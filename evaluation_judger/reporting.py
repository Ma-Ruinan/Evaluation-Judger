"""Compose a formal report from locked dimension summaries and process records."""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from decimal import Decimal
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Mm, Pt, RGBColor
from docx.oxml import OxmlElement
from docx.oxml.ns import qn

from .judge import write_json
from .render import _short_reason
from .opencode import extract_json, run
from .rubric import LABELS, METRICS
from .scoring import display, number


NARRATIVE_FIELDS = ("executive_summary", "comparison", "process_analysis", "limitations", "conclusion")


def _add_prose(doc, text: str):
    """Break long model prose at sentence boundaries for readable Word pages."""
    last = None
    reader_text = str(text).replace('executed_at', '执行时点').replace('ROUND_HALF_UP', '四舍五入')
    for block in reader_text.splitlines():
        pending = ""
        for sentence in re.split(r"(?<=[。！？])", block.strip()):
            if pending and len(pending) + len(sentence) > 350:
                last = doc.add_paragraph(pending)
                pending = ""
            pending += sentence
        if pending:
            last = doc.add_paragraph(pending)
    return last


def _validate_narrative(result: dict, facts: dict) -> None:
    if any(len(str(result.get(key, "")).strip()) < 20 for key in NARRATIVE_FIELDS):
        raise ValueError("Report prose is incomplete")
    analyses = result.get("dimension_analysis")
    if not isinstance(analyses, dict) or any(len(str(analyses.get(dim, "")).strip()) < 20 for dim in facts["dimensions"]):
        raise ValueError("Dimension analysis is incomplete")
    authorized = set(re.findall(r"\d+(?:\.\d+)?", json.dumps(facts, ensure_ascii=False, default=str)))
    authorized.update(display(value) for value in tuple(authorized) if len(value.split('.')[0]) < 24)
    prose = " ".join(str(result[key]) for key in NARRATIVE_FIELDS) + " " + " ".join(str(analyses[dim]) for dim in facts["dimensions"])
    unsupported = set(re.findall(r"\d+(?:\.\d+)?", prose)) - authorized
    if unsupported:
        raise ValueError(f"Report prose introduced numbers absent from locked facts: {sorted(unsupported)}")


def _facts(dimensions: dict, config: dict) -> dict:
    rows = [row for dimension in dimensions.values() for row in dimension["task_results"]]
    subjects = {}
    for subject in config["subjects"]:
        sid = subject["id"]
        averages = {}
        for metric in METRICS:
            values = [number(row["subjects"][sid]["scores"][metric]["final"]) for row in rows]
            averages[metric] = str(sum(values, Decimal(0)) / len(values))
        averages["quality_mean"] = str(sum((number(averages[m]) for m in METRICS[1:]), Decimal(0)) / 5)
        subjects[sid] = {"name": subject["name"], "averages": averages, "display_averages": {m: display(value) for m, value in averages.items()}}
    return {
        "dataset": Path(config["dataset"]).name,
        "task_count": len(rows),
        "dimension_count": len(dimensions),
        "subjects": subjects,
        "dimensions": dimensions,
    }


def _fallback(facts: dict) -> dict:
    subject_text = "；".join(f"{item['name']}：完成率 {display(item['averages']['completion'])}%，质量均分 {display(item['averages']['quality_mean'])} 分" for item in facts["subjects"].values())
    return {
        "executive_summary": f"本次纳入 {facts['task_count']} 道题、{facts['dimension_count']} 个维度。{subject_text}。这些数值仅描述本轮纳入的任务。",
        "comparison": "对象差异应结合下方指标表和逐题失分依据阅读；不同题目的核验范围和证据条件可能不同。",
        "process_analysis": "过程记录中的用时与执行状态和交付质量评分分别统计，详见各维度过程表。",
        "limitations": "当前结论只适用于本次纳入的题目和交付。网页可访问性、非文本素材读取范围及历史交付时点会限制部分证据的解释强度。",
        "conclusion": "本报告依据已锁定的逐题判分形成；每项数字可回溯到维度总评和逐题结果。",
        "dimension_analysis": {dim: f"该维度纳入 {len(summary['tasks'])} 道题。主要失分项与过程表现见下方任务表和逐题结果。" for dim, summary in facts["dimensions"].items()},
        "source": "deterministic_fallback",
    }


def compose_narrative(facts: dict, config: dict, project: Path, run_dir: Path) -> dict:
    """Use OpenCode for prose; failures leave scores and a usable report intact."""
    report_dir = run_dir / "reports"
    report_dir.mkdir(parents=True, exist_ok=True)
    write_json(report_dir / "report-facts.json", facts)
    digest = hashlib.sha256(json.dumps({"facts": facts, "writer_revision": 4}, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    checkpoint = report_dir / "narrative.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("facts_hash") == digest and saved.get("narrative", {}).get("source") == "opencode":
            return saved["narrative"]
    workspace = report_dir / "writer-workspace"
    workspace.mkdir(exist_ok=True)
    major = int(config.get("opencode_major", 1))
    template = "opencode.v1.example.jsonc" if major == 1 else "opencode.example.jsonc"
    shutil.copy2(project / template, workspace / "opencode.jsonc")
    agent_dir = workspace / ".opencode" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / ".opencode" / "agents" / "judge.md", agent_dir / "judge.md")
    prompt = f"""Write the ANALYTIC PROSE for a Chinese AI Agent evaluation report from the locked facts below. Do not score deliveries again. Follow the Report-Generator principles: executive summary, scope-aware dimension analysis, process performance separated from quality, object comparison, limitations and conclusion. Every concrete difference must cite a dimension/task/atom from the facts. Do not invent task results, numerical values, causes, significance or source claims. For error caps, describe ONLY the concrete finding that triggered the rule: a rule listing several possible error categories does not mean all categories occurred. This may be a subset of the dataset, so state the exact scope. Keep the executive summary around 150-300 Chinese characters; each dimension analysis around 250-500, other sections around 100-350 as evidence permits. Do not repeat full score vectors in every section: the report tables already show all metrics. Use connected paragraphs in formal Chinese plain prose without Markdown syntax. Return BEGIN_JUDGMENT then a JSON object with keys executive_summary, comparison, process_analysis, limitations, conclusion, dimension_analysis (object keyed by each exact dimension name), then END_JUDGMENT. Facts: {json.dumps(facts, ensure_ascii=False, default=str)}"""
    try:
        draft_checkpoint = report_dir / "writer-draft.json"
        saved_draft = json.loads(draft_checkpoint.read_text(encoding="utf-8")) if draft_checkpoint.exists() else {}
        if saved_draft.get("facts_hash") == digest:
            result = saved_draft["draft"]
        else:
            answer, _ = run(workspace, prompt, "write locked evaluation report", workspace / "writer-events.jsonl", project, config.get("model", "aiaaa/deepseek-v4.1-flash#high"), major=major)
            result = extract_json(answer)
        _validate_narrative(result, facts)
        write_json(draft_checkpoint, {"facts_hash": digest, "draft": result})
        for review_attempt in range(2):
            review_prompt = f"""Act as an independent reviewer of report prose. Compare the draft ONLY against the locked fact table. Check every number, subject mapping, dimension/task/atom attribution, scope limitation and whether any causal or broad superiority claim exceeds the evidence. Do not rescore deliveries. Return BEGIN_JUDGMENT then JSON {{"ok":true/false,"issues":[...]}} then END_JUDGMENT. Facts: {json.dumps(facts, ensure_ascii=False, default=str)}. Draft: {json.dumps(result, ensure_ascii=False)}"""
            review_answer, _ = run(workspace, review_prompt, "audit locked evaluation report", workspace / f"review-{review_attempt}-events.jsonl", project, config.get("model", "aiaaa/deepseek-v4.1-flash#high"), major=major)
            review = extract_json(review_answer)
            write_json(report_dir / f"narrative-review-{review_attempt}.json", review)
            write_json(report_dir / "narrative-review.json", review)
            if review.get("ok") is True:
                break
            if review_attempt == 1:
                raise ValueError(f"Independent report review found issues: {review.get('issues')}")
            repair_prompt = f"""Revise this Chinese report prose to resolve ONLY the independent review issues. Keep the same JSON keys and all locked scores. Do not infer new errors from a rubric's possible error categories: describe only findings actually recorded. Return BEGIN_JUDGMENT then the complete revised JSON then END_JUDGMENT. Facts: {json.dumps(facts, ensure_ascii=False, default=str)}. Draft: {json.dumps(result, ensure_ascii=False)}. Issues: {json.dumps(review.get('issues'), ensure_ascii=False)}"""
            repaired, _ = run(workspace, repair_prompt, "repair reviewed report prose", workspace / "writer-repair-events.jsonl", project, config.get("model", "aiaaa/deepseek-v4.1-flash#high"), major=major)
            result = extract_json(repaired)
            _validate_narrative(result, facts)
            write_json(draft_checkpoint, {"facts_hash": digest, "draft": result})
        result["source"] = "opencode"
    except Exception as exc:
        result = _fallback(facts)
        write_json(report_dir / "narrative-error.json", {"type": type(exc).__name__, "message": str(exc)})
    write_json(checkpoint, {"facts_hash": digest, "narrative": result})
    return result


def make_formal_report(config: dict, dimensions: dict, run_dir: Path, project: Path) -> Path:
    facts = _facts(dimensions, config)
    narrative = compose_narrative(facts, config, project, run_dir)
    doc = Document()
    sec = doc.sections[0]
    sec.page_width, sec.page_height = Mm(210), Mm(297)
    sec.top_margin = sec.bottom_margin = Inches(.72)
    sec.left_margin = sec.right_margin = Inches(.82)
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
    normal.font.size = Pt(10.5)
    normal.paragraph_format.space_after = Pt(7)
    blue = RGBColor(26, 59, 96)
    for name in ("Title", "Heading 1", "Heading 2"):
        doc.styles[name].font.name = "Microsoft YaHei"
        doc.styles[name].element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
        doc.styles[name].font.color.rgb = blue
    doc.styles["Title"].font.size = Pt(22)
    for border in doc.styles["Title"].element.xpath("./w:pPr/w:pBdr"):
        border.getparent().remove(border)
    doc.add_paragraph(config.get("report_title", "AI Agent 应用能力评测报告"), style="Title")
    doc.add_paragraph(f"数据集：{facts['dataset']}  |  纳入 {facts['task_count']} 道题、{facts['dimension_count']} 个维度  |  受测对象 {len(config['subjects'])} 个")
    doc.add_heading("执行摘要", 1)
    _add_prose(doc, narrative["executive_summary"])
    doc.add_heading("测评范围与方法", 1)
    doc.add_paragraph("依据各题 Rubric-Council 规则独立判分，再从已锁定的逐题结果生成维度总评。质量分由原子权重程序复算；任务等权汇总。过程 Excel 只用于执行状态和用时分析，不参与交付质量评分。下列结论只适用于本报告纳入的题目。")
    doc.add_heading("整体量化结果", 1)
    table = doc.add_table(rows=1, cols=4)
    table.style = "Table Grid"
    for cell, title in zip(table.rows[0].cells, ("受测对象", "任务完成率", "质量均分", "有效题数")):
        cell.text = title
    for item in facts["subjects"].values():
        cells = table.add_row().cells
        for cell, value in zip(cells, (item["name"], display(item["averages"]["completion"]) + "%", display(item["averages"]["quality_mean"]), str(facts["task_count"]))):
            cell.text = value
    doc.add_paragraph("五项质量指标（按本次纳入题目等权平均）")
    metric_table = doc.add_table(rows=1, cols=6)
    metric_table.style = "Table Grid"
    for cell, title in zip(metric_table.rows[0].cells, ("受测对象", *[LABELS[m] for m in METRICS[1:]])):
        cell.text = title
    for item in facts["subjects"].values():
        cells = metric_table.add_row().cells
        for cell, value in zip(cells, (item["name"], *[display(item["averages"][m]) for m in METRICS[1:]])):
            cell.text = value
    doc.add_heading("各维度分析", 1)
    for dim, summary in dimensions.items():
        doc.add_heading(dim, 2)
        scope = doc.add_paragraph(f"纳入题目：{', '.join(summary['tasks'])}。")
        scope.paragraph_format.keep_with_next = True
        analysis = _add_prose(doc, narrative["dimension_analysis"][dim])
        analysis.paragraph_format.keep_with_next = True
        table = doc.add_table(rows=1, cols=1 + len(config["subjects"]))
        table.style = "Table Grid"
        for cell, value in zip(table.rows[0].cells, ("题目", *[s["name"] + "质量均分" for s in config["subjects"]])):
            cell.text = value
        for row in summary["task_results"]:
            cells = table.add_row().cells
            cells[0].text = f"{row['task_id']} {row['task_name']}"
            for cell, subject in zip(cells[1:], config["subjects"]):
                cell.text = display(row["subjects"][subject["id"]]["scores"]["quality_mean"])
        doc.add_paragraph("代表性失分依据").paragraph_format.keep_with_next = True
        evidence_rows = []
        for row in summary["task_results"]:
            for subject in config["subjects"]:
                item = row["subjects"][subject["id"]]
                for loss in item.get("main_losses", [])[:1]:
                    evidence_rows.append((number(item["scores"]["quality_mean"]),
                                          f"{row['task_id']}/{loss['atom_id']} · {subject['name']}：{_short_reason(loss['brief'], 180)}"))
        for _, text in sorted(evidence_rows, key=lambda entry: entry[0])[:4]:
            doc.add_paragraph(text, style="List Bullet")
        cap_rows = [(row, subject, error) for row in summary["task_results"] for subject in config["subjects"]
                    for error in row["subjects"][subject["id"]].get("critical_errors", []) if error.get("triggered")]
        for row, subject, error in cap_rows:
            doc.add_paragraph(f"{row['task_id']}/{error['id']} · {subject['name']}：关键错误触发封顶。{_short_reason(error['reason'], 180)}", style="List Bullet")
    doc.add_heading("执行过程表现", 1)
    _add_prose(doc, narrative["process_analysis"])
    for dim, summary in dimensions.items():
        for subject in config["subjects"]:
            stats = summary["subjects"][subject["id"]]["process_stats"]
            doc.add_paragraph(f"{dim} · {subject['name']}：过程表标记成功 {stats['recorded_success_count']}/{len(summary['tasks'])} 题；可解析用时 {stats['duration_count']} 题，平均 {display(stats['mean_seconds']) if stats['mean_seconds'] is not None else 'N/A'} 秒。")
    if len(config["subjects"]) > 1:
        doc.add_heading("对象比较与主要差异", 1)
        _add_prose(doc, narrative["comparison"])
    doc.add_heading("限制与适用范围", 1)
    _add_prose(doc, narrative["limitations"])
    doc.add_heading("结论", 1)
    _add_prose(doc, narrative["conclusion"])
    doc.add_paragraph("逐题比较文件保留全部原子判分、定位证据及比例核验清单；本报告只摘取支撑主要结论的结果。")
    footer = sec.footer.paragraphs[0]
    footer.alignment = WD_ALIGN_PARAGRAPH.CENTER
    footer.text = "能力测评报告 · 第 "
    page_number = OxmlElement("w:fldSimple")
    page_number.set(qn("w:instr"), "PAGE")
    footer._p.append(page_number)
    footer.add_run(" 页")
    for table in doc.tables:
        header = table.rows[0]
        repeat = OxmlElement("w:tblHeader")
        header._tr.get_or_add_trPr().append(repeat)
        for cell in header.cells:
            shade = OxmlElement("w:shd")
            shade.set(qn("w:fill"), "EAF0F6")
            cell._tc.get_or_add_tcPr().append(shade)
            for paragraph in cell.paragraphs:
                paragraph.paragraph_format.keep_with_next = True
                for text_run in paragraph.runs:
                    text_run.bold = True
        for row in table.rows:
            row._tr.get_or_add_trPr().append(OxmlElement("w:cantSplit"))
    target = run_dir / "reports" / "evaluation-report.docx"
    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(target)
    return target
