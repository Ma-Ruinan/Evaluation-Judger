from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from evaluation_judger.rubric import METRICS, load_rubric
from evaluation_judger.scoring import calculate, shared_items_match, validate_atom
from evaluation_judger.render import duration_seconds
from evaluation_judger.opencode import extract_json


class ScoringContract(TestCase):
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
