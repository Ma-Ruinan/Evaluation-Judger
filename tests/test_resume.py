import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.judge import judge_task
from evaluation_judger.rubric import METRICS


class RegroupedCheckpoints(TestCase):
    def test_old_mixed_batches_resume_without_model_calls(self):
        atoms = [SimpleNamespace(id=f"X{i}", metric=metric, weight=Decimal(100),
                                 kind="CLAIM-RATIO" if metric == "accuracy" else "BIN",
                                 rule="核验该事实是否有来源支持", purpose="测试核验")
                 for i, metric in enumerate(METRICS)]
        rubric = SimpleNamespace(atoms=atoms, errors=[], by_metric=lambda metric: [a for a in atoms if a.metric == metric])
        task = SimpleNamespace(id="demo", name="跨批次恢复", dimension="维度一", rubric=rubric)
        records = []
        for atom in atoms:
            record = {"id": atom.id, "state": 1, "observation": "已检查交付中的具体事实及来源",
                      "reason": "交付明确给出了该事实和可定位的来源，两者一致并满足规则要求。",
                      "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "已核验事实"}]}
            if atom.kind == "CLAIM-RATIO":
                record.update(numerator=1, denominator=1, items=[{"claim": "已核验事实", "result": "supported"}])
            records.append(record)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checkpoints").mkdir()
            (root / "probe-ok.json").write_text("{}", encoding="utf-8")
            for start in (0, 4):
                (root / "checkpoints" / f"atoms-{start:03d}.json").write_text(
                    json.dumps({"fingerprint": "fixed", "atoms": records[start:start + 4]}, ensure_ascii=False), encoding="utf-8")
            manifest = {"fingerprint": "fixed", "files": [{"file": "delivery.md", "extracted": "delivery.md"}], "process_record": {}}
            with patch("evaluation_judger.judge.workspace_for", return_value=(root, manifest)), \
                 patch("evaluation_judger.judge.run") as model:
                result = judge_task(task, "a", {"batch_size": 2}, root, root / "run")
            model.assert_not_called()
            self.assertEqual([record["id"] for record in result["atoms"]], [atom.id for atom in atoms])
            self.assertEqual(result["scores"]["quality_mean"], "5")
