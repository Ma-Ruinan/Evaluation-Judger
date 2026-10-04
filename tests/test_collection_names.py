from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.dataset import logical_delivery_name, task_fingerprint
from evaluation_judger.judge import workspace_for, _prompt
from evaluation_judger.rubric import load_rubric
from test_dataset import fixture_rubric


class CollectionNames(TestCase):
    def test_only_confirmed_subject_and_current_task_labels_are_removed(self):
        enabled = {"collection_prefixes_are_metadata": True, "process_excel": {"task_id_map": {"W1": "1.1"}}}
        self.assertEqual(logical_delivery_name("actor-1.1-result.csv", "1.1", "actor-", enabled), "result.csv")
        self.assertEqual(logical_delivery_name("actor-W1-result.csv", "1.1", "actor-", enabled), "result.csv")
        self.assertEqual(logical_delivery_name("actor-1.2-result.csv", "1.1", "actor-", enabled), "1.2-result.csv")
        self.assertEqual(logical_delivery_name("other-1.1-result.csv", "1.1", "actor-", enabled), "other-1.1-result.csv")
        self.assertEqual(logical_delivery_name("1.1-result.csv", "1.1", "actor-", enabled), "1.1-result.csv")
        self.assertEqual(logical_delivery_name("actor-1.1-result.csv", "1.1", "actor-", {}), "actor-1.1-result.csv")

    def test_mapping_keeps_material_bytes_names_and_evidence_paths(self):
        with TemporaryDirectory() as directory:
            root = Path(directory); task_dir = root / "task"; task_dir.mkdir()
            question = task_dir / "问题描述.txt"; question.write_text("Save result.csv", encoding="utf-8")
            rubric = task_dir / "rubric.md"; rubric.write_text(fixture_rubric(), encoding="utf-8")
            delivery = task_dir / "actor-1.1-result.csv"; delivery.write_text("value\n42\n", encoding="utf-8")
            task = SimpleNamespace(id="1.1", name="collection", directory=task_dir, question=question,
                                   rubric=load_rubric(rubric), sources=(), deliveries={"x": (delivery,)})
            config = {"subjects": [{"id": "x", "prefix": "actor-"}], "collection_prefixes_are_metadata": True}
            project = Path(__file__).resolve().parents[1]
            with patch("evaluation_judger.judge.process_records", return_value={"1.1": {"x": {}}}):
                workspace, manifest = workspace_for(task, "x", config, project, root / "run")
            record = next(item for item in manifest["files"] if item["category"] == "deliveries")
            self.assertEqual(record["logical_name"], "result.csv")
            self.assertEqual(record["file"], "deliveries/actor-1.1-result.csv")
            self.assertEqual((workspace / record["file"]).read_bytes(), delivery.read_bytes())
            self.assertFalse((workspace / "deliveries/result.csv").exists())
            prompt = _prompt(task, "x", [], manifest)
            self.assertIn("Cite the unchanged collected path", prompt)
            self.assertIn('"deliveries/actor-1.1-result.csv": "result.csv"', prompt)
            baseline = task_fingerprint(task, "x", {})
            self.assertEqual(baseline, task_fingerprint(task, "x", {"collection_prefixes_are_metadata": False}))
            self.assertNotEqual(baseline, task_fingerprint(task, "x", config))
