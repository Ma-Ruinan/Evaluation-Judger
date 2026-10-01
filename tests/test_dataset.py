from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from evaluation_judger.dataset import discover


def fixture_rubric() -> str:
    lines = ["## 6. 任务完成率"]
    for index, prefix in enumerate("TCAFSH"):
        if index:
            if index == 1:
                lines.append("## 7. 五项质量评分")
            lines.append(f"### 7.{index} test")
        lines += ["| ID | 判据层 | 权重 | 唯一目的 | 状态函数与可观察规则 | 必需证据 | 溯源 |", "| --- | --- | ---: | --- | --- | --- | --- |", f"| {prefix}01 | test | 100 | purpose | **BIN**：rule | file | REQ |"]
    return "\n".join(lines)


class DatasetIsolation(TestCase):
    def test_one_to_three_subjects_and_excluded_delivery(self):
        with TemporaryDirectory() as root:
            base = Path(root)
            folder = base / "维度一-演示" / "1_示例题"
            folder.mkdir(parents=True)
            (folder / "问题描述.txt").write_text("question", encoding="utf-8")
            (folder / "rubric.md").write_text(fixture_rubric(), encoding="utf-8")
            (folder / "common.txt").write_text("source", encoding="utf-8")
            for prefix in ("alpha-", "beta-", "gamma-"):
                (folder / f"{prefix}1.1-answer.md").write_text(prefix, encoding="utf-8")
            all_subjects = [{"id": name, "name": name, "prefix": name + "-"} for name in ("alpha", "beta", "gamma")]
            three = discover({"dataset": str(base), "subjects": all_subjects})
            self.assertEqual(len(three), 1)
            self.assertEqual(len(three[0].sources), 1)
            self.assertEqual({k: len(v) for k, v in three[0].deliveries.items()}, {"alpha": 1, "beta": 1, "gamma": 1})
            one = discover({"dataset": str(base), "subjects": all_subjects[:1], "excluded_subject_prefixes": ["beta-", "gamma-"]})
            self.assertEqual([p.name for p in one[0].sources], ["common.txt"])
