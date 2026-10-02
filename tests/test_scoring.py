from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from evaluation_judger.rubric import METRICS, load_rubric, referenced_atoms, shared_sample_references
from evaluation_judger.scoring import calculate, shared_items_match, validate_atom
from evaluation_judger.render import _atom_difference, _safe, _visible_items, duration_seconds, task_markdown
from evaluation_judger.opencode import extract_json
from evaluation_judger.judge import atom_batches, needs_time_review
from evaluation_judger.multisample import _pool, _verdict
from types import SimpleNamespace


class ScoringContract(TestCase):
    def test_display_rounding_does_not_block_authoritative_ratio(self):
        atom = SimpleNamespace(id="A01", kind="CLAIM-RATIO", metric="accuracy", weight=Decimal(100), rule="核验事实", purpose="准确性")
        raw = {"id": "A01", "state": "0.67", "numerator": 2, "denominator": 3,
               "observation": "三条事实中两条符合所提供的来源",
               "reason": "三条事实逐条核验后，第一条和第二条有支持，第三条与来源冲突。",
               "items": [{"claim": str(i)} for i in range(3)],
               "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "事实"}]}
        self.assertEqual(validate_atom(raw, atom, {"delivery.md"})["state"], str(Decimal(2) / Decimal(3)))
        raw["state"] = 1
        with self.assertRaises(ValueError):
            validate_atom(raw, atom, {"delivery.md"})

    def test_sample_population_mismatch_is_reconciled_only_when_proven(self):
        atom = SimpleNamespace(id="H03", kind="CLAIM-RATIO", metric="hallucination", weight=Decimal(100),
                               rule="对全部来源抽样核验", purpose="来源核验")
        raw = {"id": "H03", "state": 1, "numerator": 61, "denominator": 61,
               "observation": "来源全集61条，实际核验26条，均未发现虚构来源。",
               "reason": "核验的26条来源均有可识别的出版方和标题，未发现虚构条目，符合规则。",
               "items": [{"entry": str(i), "result": "非虚构（平台和标题真实）"} for i in range(26)],
               "evidence": [{"file": "delivery.md", "locator": "参考文献", "quote": "来源条目"}]}
        checked = validate_atom(raw, atom, {"delivery.md"})
        self.assertEqual((checked["numerator"], checked["denominator"], checked["state"]), (26, 26, "1"))
        self.assertEqual(checked["record_corrections"][0]["original_denominator"], "61")
        self.assertEqual(raw["denominator"], 61)
        raw["items"][0]["result"] = "无法核验"
        self.assertEqual(validate_atom(raw, atom, {"delivery.md"})["denominator"], 61)

    def test_long_claim_audits_are_isolated_without_losing_atom_order(self):
        atoms = [SimpleNamespace(id=str(i), kind=kind) for i, kind in enumerate(["BIN", "RATIO", "CLAIM-RATIO", "CLAIM-RATIO", "BIN", "BIN"])]
        batches = list(atom_batches(atoms, 4))
        self.assertEqual([[a.id for a in group] for _, group in batches], [["0", "1"], ["2"], ["3"], ["4", "5"]])
        self.assertEqual([a.id for _, group in batches for a in group], [a.id for a in atoms])

    def test_multi_sample_pool_deduplicates_exact_claims(self):
        pool = _pool({"A01": {"items": [{"claim": "甲 10 亿元"}]}, "A02": {"items": [{"claim": "甲10亿元"}, {"claim": "乙 20 亿元"}]}})
        self.assertEqual([item["id"] for item in pool], ["A01-001", "A02-002"])
        self.assertFalse(_verdict({"verdict": "unsupported"}))

    def test_time_review_trigger_does_not_depend_on_model_wording(self):
        atom = SimpleNamespace(kind="BIN", rule="截止日期须与实际执行日期一致")
        self.assertTrue(needs_time_review(atom, {"state": "0", "reason": "日期不符"}, {"executed_at": None}))
        self.assertFalse(needs_time_review(atom, {"state": "0"}, {"executed_at": "2026-09-25"}))

    def test_multi_atom_sample_reference_is_not_a_single_reference(self):
        self.assertEqual(referenced_atoms("核验全集同 A01/A02/A03"), ("A01", "A02", "A03"))
        self.assertEqual(referenced_atoms("主张切分与核验全集同A01"), ("A01",))
        self.assertEqual(shared_sample_references("主张切分与核验全集同 A01/A02/A03"), ("A01", "A02", "A03"))
        self.assertEqual(shared_sample_references("核验集合与抽样清单同A01"), ("A01",))
        self.assertEqual(shared_sample_references("抽样规则同 A01"), ())
        self.assertEqual(shared_sample_references("主张切分同A01。核验全集：全部结论性主张"), ())

    def test_markdown_escapes_html_and_comparison_only_uses_shared_checks(self):
        self.assertIn("&lt;script", _safe('<script id="references-data">'))
        left = {"items": [{"element": "指定主题", "result": "satisfied"}, {"element": "只在左侧抽样", "result": "satisfied"}]}
        right = {"items": [{"element": "指定主题", "result": "not satisfied"}]}
        summary = _atom_difference([left, right], ["甲", "乙"])
        self.assertIn("甲满足，乙未满足", summary)
        self.assertNotIn("只在左侧抽样", summary)

    def test_long_human_view_keeps_all_failed_items(self):
        items = [{"element": str(i), "result": "satisfied"} for i in range(12)]
        items[-1]["result"] = "not satisfied"
        visible, omitted = _visible_items(items)
        self.assertIn(items[-1], visible)
        self.assertEqual(len(visible) + omitted, len(items))

    def test_compact_view_preserves_chinese_losses_and_unknown_verdicts(self):
        items = [{"element": str(i), "result": "支持"} for i in range(12)]
        items += [{"element": "loss", "result": "不支持"}]
        items += [{"element": str(i), "result": "新的判定标签"} for i in range(5)]
        visible, omitted = _visible_items(items)
        self.assertTrue(all(item in visible for item in items[12:]))
        self.assertEqual(omitted, 10)

    def test_human_record_keeps_item_explanations_and_flexible_corrections(self):
        task = SimpleNamespace(id="1.1", name="样例", dimension="维度一")
        scores = {m: {"final": "1"} for m in METRICS}
        scores['quality_mean'] = '1'
        atom = {"id": "A01", "metric": "accuracy", "state": "0", "weight": "100",
                "observation": "观察", "reason": "理由", "numerator": 0, "denominator": 1,
                "items": [{"element": "政策定位", "result": "不支持", "explanation": "法律施行证据不能支持政策定位"}],
                "record_corrections": [{"reason": "证据归因修正", "from": "支持", "to": "不支持"}, "补充说明"], "evidence": []}
        text = task_markdown(task, [{"subject": "a", "scores": scores, "errors": [], "atoms": [atom]}], {"a": "甲"})
        self.assertIn("法律施行证据不能支持政策定位", text)
        self.assertIn("证据归因修正", text)
        self.assertIn("补充说明", text)

    def test_shared_sample_checks_claim_identity_as_well_as_count(self):
        source = [{"claim": "故宫 IP 年销售额超 15 亿元"}, {"claim": "河南卫视中国节日 5 季 35 期"}]
        self.assertTrue(shared_items_match(source, [{"claim": "故宫 IP 年销售额超15亿元"}, {"claim": "河南卫视中国节日5季35期"}]))
        self.assertFalse(shared_items_match(source, list(reversed(source))))

    def test_process_duration_is_not_a_quality_score(self):
        self.assertEqual(duration_seconds("266m13s"), 15973)
        self.assertEqual(duration_seconds("1h2m3s"), 3723)
        self.assertIsNone(duration_seconds("一次运行成功"))

    def test_minor_json_punctuation_can_be_repaired_before_validation(self):
        self.assertEqual(extract_json('BEGIN_JUDGMENT\n{"atoms":[{"id":"T01" "state":1}]}\nEND_JUDGMENT')["atoms"][0]["state"], 1)

    def rubric(self):
        lines = ["## 6. 任务完成率", "| ID | 判据层 | 权重 | 唯一目的 | 状态函数与可观察规则 | 必需证据 | 溯源 |"]
        lines.append("| --- | --- | ---: | --- | --- | --- | --- |")
        lines.append("| T01 | test | 100 | completion | **BIN**：exists | file | REQ |")
        lines.append("## 7. 五项质量评分")
        for index, prefix in enumerate("CAFSH", 1):
            lines.append(f"### 7.{index} metric")
            lines.append("| ID | 判据层 | 权重 | 唯一目的 | 状态函数与可观察规则 | 必需证据 | 溯源 |")
            lines.append("| --- | --- | ---: | --- | --- | --- | --- |")
            rule = "**CLAIM-RATIO**：空集合状态=1" if prefix == "A" else "**BIN**：valid"
            lines.append(f"| {prefix}01 | test | 100 | purpose | {rule} | file | REQ |")
        lines += ["## 8. 关键错误与分数上限", "| 错误代码 | 可观察触发条件 | 影响 |", "| --- | --- | --- |", "| E01 | severe problem | 内容覆盖度：上限 1；原子 C01；准确率·忠实度：上限 1.5；原子 A01 |", "## 9. 证据记录与计算规则"]
        return "\n".join(lines)

    def test_empty_set_and_multiple_caps(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(self.rubric(), encoding="utf-8")
            rubric = load_rubric(path)
            self.assertEqual(rubric.errors[0].caps, {"coverage": Decimal("1"), "accuracy": Decimal("1.5")})
            records = []
            for atom in rubric.atoms:
                raw = {"id": atom.id, "state": 1, "observation": "A concrete observation", "reason": "The observable delivery satisfies the assigned rule.", "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "observed content"}]}
                if atom.id == "A01":
                    raw.update({"numerator": 0, "denominator": 0, "items": []})
                records.append(validate_atom(raw, atom, {"delivery.md"}))
            scores = calculate(rubric, records, [{"id": "E01", "triggered": True}])
            self.assertEqual(scores["completion"]["final"], "100")
            self.assertEqual(scores["coverage"]["final"], "1")
            self.assertEqual(scores["accuracy"]["final"], "1.5")
            self.assertEqual(scores["quality_mean"], "3.5")

    def test_missing_ratio_breakdown_is_rejected(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "rubric.md"
            path.write_text(self.rubric(), encoding="utf-8")
            atom = next(a for a in load_rubric(path).atoms if a.id == "A01")
            with self.assertRaisesRegex(ValueError, "itemized"):
                validate_atom({"id": "A01", "state": 0.5, "numerator": 1, "denominator": 2, "items": [], "observation": "One of two supported", "reason": "One claim agrees with the source, one does not.", "evidence": [{"file": "delivery.md", "locator": "L2", "quote": "claim"}]}, atom, {"delivery.md"})
