"""Render traceable Markdown, dimension summaries, and a dataset Word report."""
from __future__ import annotations

import json
import re
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt

from .dataset import process_records
from .judge import write_json
from .rubric import LABELS, METRICS
from .scoring import display, number


def _safe(value) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def duration_seconds(value) -> int | None:
    if value is None:
        return None
    raw = str(value).strip().lower()
    match = re.fullmatch(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?", raw)
    if match and any(x is not None for x in match.groups()):
        h, m, s = (int(x or 0) for x in match.groups())
        return h * 3600 + m * 60 + s
    return None


def _short_reason(value: str, limit: int = 145) -> str:
    cleaned = " ".join(str(value).split())
    cleaned = re.sub(r"^(?:BIN|RATIO|CLAIM-RATIO|COUNT)(?:规则|判据)?[：:]\s*", "", cleaned)
    cleaned = re.sub(r"(?:numerator|denominator|state)\s*=\s*[\d.]+[，,、;；]?", "", cleaned, flags=re.I)
    if len(cleaned) <= limit:
        return cleaned
    candidate = cleaned[:limit]
    end = max(candidate.rfind("。"), candidate.rfind("；"))
    return candidate[:end + 1] if end > 65 else candidate.rstrip("，、；。 ") + "……"


def _loss_brief(atom: dict) -> str:
    """Show the actual failed checks in the report, not a clipped rubric recital."""
    items = atom.get("items") or []
    failed = []
    for item in items:
        if not isinstance(item, dict):
            continue
        outcome = str(item.get("verdict", item.get("result", ""))).lower().strip()
        if outcome.startswith(("inconsistent", "unsupported", "not satisfied", "failed", "missing", "unsatisfied")):
            failed.append(item)
    ratio = ""
    if atom.get("denominator") is not None:
        ratio = f"{atom['numerator']}/{atom['denominator']}；"
    if failed:
        examples = []
        for item in failed[:2]:
            name = str(item.get("claim") or item.get("element") or item.get("source") or item.get("id") or "核验项")
            outcome = str(item.get("verdict", item.get("result", ""))).lower().strip()
            note = str(item.get("note") or item.get("reason") or "")
            if not note:
                note = ("页面无法取得，来源及内容未获证实" if outcome.startswith("not satisfied") else
                        "缺少可识别的来源支撑" if outcome.startswith("unsupported") else
                        "与已核来源不一致")
            location = str(item.get("location") or item.get("scope") or "")
            if location.startswith(("extracted/", "deliveries/")):
                location = ""
            examples.append(f"{name}（{location}）：{_short_reason(note, 85)}" if location else f"{name}：{_short_reason(note, 85)}")
        remainder = f"；另有 {len(failed) - 2} 项，详见逐题结果" if len(failed) > 2 else ""
        return f"{ratio}{'；'.join(examples)}{remainder}。"
    return f"{ratio}{_short_reason(atom['reason'])}"


def task_markdown(task, results: list[dict], names: dict[str, str]) -> str:
    lines = [f"# {task.id} {task.name} 评测结果", "", f"维度：{task.dimension}", "", "## 分数", ""]
    headers = ["指标", *[names[r["subject"]] for r in results]]
    if len(results) > 1:
        headers += [f"{names[r['subject']]}−{names[results[0]['subject']]}" for r in results[1:]]
    lines += ["| " + " | ".join(headers) + " |", "| " + " | ".join(["---"] * len(headers)) + " |"]
    for metric in METRICS:
        values = [number(r["scores"][metric]["final"]) for r in results]
        cells = [display(v) + ("%" if metric == "completion" else "") for v in values]
        cells += [display(v - values[0]) for v in values[1:]]
        lines.append("| " + LABELS[metric] + " | " + " | ".join(cells) + " |")
    values = [number(r["scores"]["quality_mean"]) for r in results]
    lines.append("| 质量均分 | " + " | ".join([*[display(v) for v in values], *[display(v - values[0]) for v in values[1:]]]) + " |")
    if len(results) > 1:
        lines += ["", "比较口径：各对象分别按同一 rubric 独立判分；差值为后列对象减首列对象，按未舍入分数计算。", ""]
    for result in results:
        lines += ["", f"## {names[result['subject']]} 逐项依据", ""]
        for metric in METRICS:
            lines += [f"### {LABELS[metric]}", ""]
            for atom in [a for a in result["atoms"] if a["metric"] == metric]:
                lines += [f"#### {atom['id']} 状态 {display(atom['state'])} 权重 {atom['weight']}", "", f"观察：{atom['observation']}", "", f"判分理由：{atom['reason']}", ""]
                if atom["denominator"] is not None:
                    lines += [f"计算：{atom['numerator']} / {atom['denominator']} = {display(atom['state'])}", ""]
                    for item in atom["items"]:
                        lines.append(f"- 核验项：{_safe(json.dumps(item, ensure_ascii=False) if isinstance(item, dict) else item)}")
                    lines.append("")
                for note in atom.get("validation_notes", []):
                    lines.append(f"- 记录检查：{note}")
                if atom.get("validation_notes"):
                    lines.append("")
                for e in atom["evidence"]:
                    lines.append(f"- 证据：`{e['file']}` · {_safe(e['locator'])} · {_safe(e['quote'])}")
                lines.append("")
        if result["errors"]:
            lines += ["### 关键错误与封顶", ""]
            for error in result["errors"]:
                lines += [f"- {error['id']}：{'触发' if error['triggered'] else '未触发'}。{error['reason']}"]
            lines.append("")
    return "\n".join(lines)


def summaries(tasks, result_map: dict, config: dict, run_dir: Path) -> dict:
    names = {s["id"]: s["name"] for s in config["subjects"]}
    process = process_records(config)
    by_dim = defaultdict(list)
    for task in tasks:
        results = [result_map[(task.id, s["id"])] for s in config["subjects"]]
        by_dim[task.dimension].append((task, results))
        path = run_dir / "results" / task.id / ("compare-result.md" if len(results) > 1 else "evaluation-result.md")
        path.write_text(task_markdown(task, results, names), encoding="utf-8")
    dimensions = {}
    for dim, entries in by_dim.items():
        summary = {"dimension": dim, "tasks": [t.id for t, _ in entries], "subjects": {}}
        md = [f"# {dim} 总评", "", f"纳入题目：{', '.join(summary['tasks'])}", ""]
        for s in config["subjects"]:
            sid = s["id"]
            rows = [next(r for r in results if r["subject"] == sid) for _, results in entries]
            metric_scores = {}
            for metric in METRICS:
                values = [number(r["scores"][metric]["final"]) for r in rows]
                metric_scores[metric] = str(sum(values, Decimal(0)) / len(values))
            metric_scores["quality_mean"] = str(sum((number(metric_scores[m]) for m in METRICS[1:]), Decimal(0)) / 5)
            records = [process.get(t.id, {}).get(sid, {}) for t, _ in entries]
            durations = [x for record in records if (x := duration_seconds(record.get("runtime"))) is not None]
            successes = sum("成功" in str(r.get("status", "")) and not any(term in str(r.get("status", "")) for term in ("不成功", "未成功", "失败")) for r in records)
            process_stats = {"duration_count": len(durations), "mean_seconds": str(Decimal(sum(durations)) / len(durations)) if durations else None, "recorded_success_count": successes}
            summary["subjects"][sid] = {"name": s["name"], "scores": metric_scores, "process": records, "process_stats": process_stats}
            md += [f"## {s['name']}", "", f"任务数：{len(rows)}。任务完成率 {display(metric_scores['completion'])}%；质量均分 {display(metric_scores['quality_mean'])} 分。", "", "| 指标 | 均分 |", "| --- | ---: |"]
            md += [f"| {LABELS[m]} | {display(metric_scores[m])}{'%' if m == 'completion' else ''} |" for m in METRICS]
            md += ["", "### 主要失分依据", ""]
            weak = sorted((a for r in rows for a in r["atoms"] if number(a["state"]) < 1), key=lambda a: (number(a["state"]), -number(a["weight"])))[:8]
            if weak:
                md += [f"- {a['id']}（{LABELS[a['metric']]}）：{a['reason']}" for a in weak]
            else:
                md.append("- 纳入题目中未发现原子项失分。")
            md += ["", "### 测试过程记录", ""]
            md += [f"- {t.id}：用时 {process.get(t.id, {}).get(sid, {}).get('runtime', '未记录')}；状态 {process.get(t.id, {}).get(sid, {}).get('status', '未记录')}" for t, _ in entries]
            md += ["", f"可解析用时 {process_stats['duration_count']}/{len(entries)} 题，平均 {display(process_stats['mean_seconds']) if process_stats['mean_seconds'] is not None else 'N/A'} 秒；过程表中标记成功 {process_stats['recorded_success_count']}/{len(entries)} 题。此处是执行记录统计，不参与质量分。"]
            md.append("")
        if len(config["subjects"]) > 1:
            base = config["subjects"][0]
            md += ["## 对象比较", "", f"差值方向：后列对象减 {base['name']}。", "", "| 指标 | " + " | ".join(s["name"] for s in config["subjects"]) + " |", "| --- | " + " | ".join("---:" for _ in config["subjects"]) + " |"]
            for metric in (*METRICS, "quality_mean"):
                md.append("| " + (LABELS.get(metric) or "质量均分") + " | " + " | ".join(display(summary["subjects"][s["id"]]["scores"][metric]) for s in config["subjects"]) + " |")
            md += ["", "相对首列的质量均分差值：" + "；".join(f"{s['name']} {display(number(summary['subjects'][s['id']]['scores']['quality_mean']) - number(summary['subjects'][base['id']]['scores']['quality_mean']))}" for s in config["subjects"][1:]) + "。", ""]
        slug = entries[0][0].id.split(".")[0]
        (run_dir / "results" / f"dimension-{slug}-summary.md").write_text("\n".join(md), encoding="utf-8")
        write_json(run_dir / "results" / f"dimension-{slug}-summary.json", summary)
        dimensions[dim] = summary
    return dimensions


def make_report(config: dict, tasks, result_map: dict, dimensions: dict, run_dir: Path) -> Path:
    doc = Document()
    section = doc.sections[0]
    section.top_margin = section.bottom_margin = Inches(.75)
    section.left_margin = section.right_margin = Inches(.8)
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)
    for style in ("Title", "Heading 1", "Heading 2"):
        doc.styles[style].font.name = "Microsoft YaHei"
        doc.styles[style].font.color.rgb = __import__("docx").shared.RGBColor(0, 0, 0)
    title_style = doc.styles["Title"]
    title_style.font.size = Pt(21)
    title_style.font.bold = True
    for border in title_style.element.xpath("./w:pPr/w:pBdr"):
        border.getparent().remove(border)
    doc.add_paragraph(config.get("report_title", "AI Agent 应用能力评测报告"), style="Title")
    doc.add_paragraph(f"数据集：{Path(config['dataset']).name}  |  纳入 {len(tasks)} 道题  |  受测对象 {len(config['subjects'])} 个")
    doc.add_heading("评测范围与方法", level=1)
    doc.add_paragraph("本报告依据逐题 Rubric-Council 评分规则，对每个受测对象的交付独立判分。完成率与五项质量指标分别统计；各题等权，数据集统计直接平均题目原始分数。具体判分理由和证据见逐题结果文件。测试过程用时与运行状态取自过程记录表。")
    doc.add_heading("总体结果", level=1)
    top_table = doc.add_table(rows=1, cols=3)
    top_table.style = "Table Grid"
    for i, label in enumerate(["受测对象", "任务完成率", "质量均分"]):
        top_table.rows[0].cells[i].text = label
    doc.add_paragraph("五项质量指标均分")
    metric_table = doc.add_table(rows=1, cols=6)
    metric_table.style = "Table Grid"
    for i, label in enumerate(["受测对象", "内容覆盖度", "准确率忠实度", "格式合规度", "结构完整度", "幻觉自洽性"]):
        metric_table.rows[0].cells[i].text = label
    for subject in config["subjects"]:
        sid = subject["id"]
        rows = [result_map[(t.id, sid)] for t in tasks]
        values = {m: sum((number(r["scores"][m]["final"]) for r in rows), Decimal(0)) / len(rows) for m in METRICS}
        mean = sum((values[m] for m in METRICS[1:]), Decimal(0)) / 5
        cells = top_table.add_row().cells
        for i, value in enumerate([subject["name"], display(values["completion"]) + "%", display(mean)]):
            cells[i].text = value
        cells = metric_table.add_row().cells
        for i, value in enumerate([subject["name"], *[display(values[m]) for m in METRICS[1:]]]):
            cells[i].text = value
    if len(config["subjects"]) > 1:
        base = config["subjects"][0]
        base_rows = [result_map[(t.id, base["id"])] for t in tasks]
        base_mean = sum((sum((number(r["scores"][m]["final"]) for r in base_rows), Decimal(0)) / len(base_rows) for m in METRICS[1:]), Decimal(0)) / 5
        for subject in config["subjects"][1:]:
            rows = [result_map[(t.id, subject["id"])] for t in tasks]
            mean = sum((sum((number(r["scores"][m]["final"]) for r in rows), Decimal(0)) / len(rows) for m in METRICS[1:]), Decimal(0)) / 5
            doc.add_paragraph(f"{subject['name']}相对{base['name']}的质量均分差值为 {display(mean - base_mean)} 分。")
    for dim, summary in dimensions.items():
        doc.add_heading(dim, level=1)
        doc.add_paragraph(f"纳入 {len(summary['tasks'])} 道题：{', '.join(summary['tasks'])}。")
        for subject in config["subjects"]:
            item = summary["subjects"][subject["id"]]
            score = item["scores"]
            doc.add_heading(subject["name"], level=2)
            doc.add_paragraph(f"任务完成率 {display(score['completion'])}%；质量均分 {display(score['quality_mean'])} 分。")
            stats = item["process_stats"]
            doc.add_paragraph(f"过程记录：{stats['recorded_success_count']}/{len(summary['tasks'])} 题标记成功；可解析用时 {stats['duration_count']} 题，平均 {display(stats['mean_seconds']) if stats['mean_seconds'] is not None else 'N/A'} 秒。过程状态与交付评分分别统计。")
            weak = sorted(((t, a) for t in tasks if t.dimension == dim for a in result_map[(t.id, subject["id"])]["atoms"] if number(a["state"]) < 1), key=lambda pair: (number(pair[1]["state"]), -number(pair[1]["weight"])))[:3]
            if weak:
                doc.add_paragraph("主要失分依据：")
                for task, atom in weak:
                    paragraph = doc.add_paragraph(f"{task.id} {atom['id']}：{_loss_brief(atom)}", style="List Bullet")
                    paragraph.paragraph_format.keep_together = True
    if len(tasks) > 1:
        doc.add_heading("核验说明", level=1)
        doc.add_paragraph("报告中的分数由保存的逐原子状态按 rubric 权重计算。逐题 JSON 包含原始状态、分子分母、证据、理由和封顶判断；逐题 Markdown 便于人工复核。对于历史复用的交付，依据其执行时点判别时效性。")
    footer = section.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.text = "Evaluation Judger 评测报告"
    target = run_dir / "reports" / "evaluation-report.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(target)
    return target
