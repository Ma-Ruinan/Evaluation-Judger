import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.multisample import _source_positive, review_multi_atom


class MultiSampleCheckpoint(TestCase):
    def test_only_verified_positive_source_claims_are_carried_forward(self):
        self.assertTrue(_source_positive({"source_result": "correct"}))
        self.assertTrue(_source_positive({"source_result": "supported"}))
        self.assertFalse(_source_positive({"source_result": "incorrect"}))
        self.assertFalse(_source_positive({"source_result": "unsupported"}))

    def test_review_scores_all_selected_claims_and_resumes(self):
        atom = SimpleNamespace(id="H01", metric="hallucination", weight=55, purpose="支撑性", kind="CLAIM-RATIO", rule="核验全集同 A01/A02；空集合状态=0")
        sources = {"A01": {"items": [{"claim": "第一条"}]}, "A02": {"items": [{"claim": "第二条"}]}}
        target = {"evidence": [{"file": "delivery.md", "locator": "L1", "quote": "第一条"}], "items": []}
        selection = 'BEGIN_JUDGMENT\n' + json.dumps({"selected_ids": ["A01-001", "A02-001"], "reason": "两份清单各取一条且没有重复主张"}, ensure_ascii=False) + '\nEND_JUDGMENT'
        items = 'BEGIN_JUDGMENT\n' + json.dumps({"items": [
            {"sample_id": "A01-001", "supported": True, "reason": "交付第一段给出可定位来源", "location": "L1"},
            {"sample_id": "A02-001", "supported": False, "reason": "交付第二段没有可定位来源", "location": "L2"},
        ]}, ensure_ascii=False) + '\nEND_JUDGMENT'
        with TemporaryDirectory() as directory:
            events = Path(directory) / "events"
            events.mkdir()
            for attempt in range(2):
                (events / f"multi-H01-v3-items-000-attempt-{attempt}.jsonl").write_text("", encoding="utf-8")
            args = dict(task_id="1.1", subject="demo", workspace=Path(directory), fingerprint="fixed",
                        known_files={"delivery.md"}, project=Path(directory), model="test", major=1)
            with patch("evaluation_judger.multisample.run", side_effect=[(selection, False), (items, False)]) as mocked:
                first = review_multi_atom(atom, sources, target, **args)
                second = review_multi_atom(atom, sources, target, **args)
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(mocked.call_args.args[3].name, "multi-H01-v3-items-000-attempt-2.jsonl")
            self.assertTrue((events / "multi-H01-v3-items-000-attempt-0.jsonl").exists())
            self.assertEqual((first["numerator"], first["denominator"]), (1, 2))
            self.assertEqual(second, first)
