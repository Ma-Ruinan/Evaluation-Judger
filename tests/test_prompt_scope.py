"""Regression coverage for large inventories and stale atom responses."""
import json
import subprocess
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.judge import _prompt, judge_task
from evaluation_judger.opencode import TaskSession, run
from evaluation_judger.rubric import METRICS


class PromptScope(TestCase):
    def test_large_inventory_keeps_current_rules_and_feedback_visible(self):
        atom = SimpleNamespace(id="C02", metric="coverage", weight=Decimal(30),
                               purpose="核验影响", rule="逐项核验攻击向量与影响", evidence="交付报告")
        task = SimpleNamespace(id="demo", name="大型代码交付", rubric=SimpleNamespace(text="固定标准"))
        entries = [{"category": "deliveries", "file": f"deliveries/project/deep/library/file{i}.py",
                    "extracted": f"extracted/file{i}.txt", "limitation": None,
                    "logical_name": f"file{i}.py"} for i in range(3000)]
        entries += [{"category": "deliveries", "file": "deliveries/project/审计报告.md",
                     "extracted": "extracted/report.txt", "limitation": None, "logical_name": "审计报告.md"}]
        manifest = {"files": entries, "process_record": {}}
        original = json.dumps(manifest, ensure_ascii=False)
        prompt = _prompt(task, "subject", [atom], manifest, "Requested ONLY C02; received T01.")
        self.assertLess(len(prompt), 16_000)
        self.assertIn('"id": "C02"', prompt[:2000])
        self.assertIn("Requested ONLY C02; received T01", prompt[:2000])
        self.assertIn('shape: {"atoms":[...]}', prompt[:2000])
        self.assertIn("审计报告.md (material-files.jsonl line 3001)", prompt)
        self.assertIn("offset=1, limit=20", prompt)
        self.assertIn("logical_name", prompt)
        self.assertIn("PDF URI annotations", prompt)
        self.assertIn("A clickable link counts", prompt)
        self.assertIn("Do not deduct solely because", prompt)
        self.assertNotIn("library/file2999.py", prompt)
        self.assertEqual(json.dumps(manifest, ensure_ascii=False), original)

    def test_long_attachment_requires_reading_beyond_preview(self):
        with TemporaryDirectory() as folder:
            root = Path(folder)
            prompt = "CURRENT REQUEST C02\n" + "材料内容\n" * 20000 + "FINAL REQUIREMENT\n"
            event = json.dumps({"type": "text", "part": {"text": 'BEGIN_JUDGMENT {} END_JUDGMENT'}})
            with patch("evaluation_judger.opencode.environment", return_value={}), \
                 patch("evaluation_judger.opencode._executable", return_value="opencode"), \
                 patch("evaluation_judger.opencode.subprocess.run", return_value=subprocess.CompletedProcess([], 0, event, "")) as call:
                run(root, prompt, "test", root / "events.jsonl", root, "provider/model")
            command = call.call_args.args[0]
            instruction = command[command.index("--file") - 1]
            self.assertIn("20002 lines", instruction)
            self.assertIn("preview may be truncated", instruction)
            self.assertIn("offset/limit", instruction)
            self.assertIn("final line", instruction)
            self.assertEqual((root / "prompt.txt").read_text(encoding="utf-8"), prompt)

    def test_session_restart_survives_interruption_and_keeps_checkpoints(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); workspace = root / ("a" * 12)
            (workspace / "checkpoints").mkdir(parents=True)
            (workspace / "events").mkdir()
            checkpoint = workspace / "checkpoints/judge-session.json"
            checkpoint.write_text(json.dumps({"fingerprint": "a" * 64, "session_id": "stale"}))
            (workspace / "events/atoms-000.jsonl").write_text(json.dumps({"sessionID": "stale"}))
            locked = workspace / "checkpoints/atoms-000.json"; locked.write_text("locked atoms")
            session = TaskSession(workspace, root, "model", 1, "a" * 64)
            session.restart("Wrong IDs twice")
            resumed = TaskSession(workspace, root, "model", 1, "a" * 64)
            self.assertIsNone(resumed.session_id)
            self.assertEqual(resumed.resets[0]["previous_session_id"], "stale")
            self.assertEqual(locked.read_text(), "locked atoms")
            def reply(workspace, prompt, title, output, *args, **kwargs):
                self.assertIsNone(kwargs["session_id"])
                output.write_text(json.dumps({"sessionID": "fresh"}))
                return "{}", True
            with patch("evaluation_judger.opencode.run", side_effect=reply):
                resumed.ask("C02", "test", workspace / "events/new.jsonl")
            self.assertEqual(TaskSession(workspace, root, "model", 1, "a" * 64).session_id, "fresh")
            self.assertEqual(len(json.loads(checkpoint.read_text())["resets"]), 1)

    def test_targeted_retry_recovers_wrong_ids_without_rescoring_locked_atoms(self):
        atoms = [SimpleNamespace(id=f"X{i}", metric=metric, weight=Decimal(100), kind="BIN",
                                 rule="核验交付", purpose="核验", evidence="正文") for i, metric in enumerate(METRICS)]
        task = SimpleNamespace(id="demo", name="断点恢复", dimension="维度",
                               rubric=SimpleNamespace(atoms=atoms, errors=[], text="固定标准",
                                                      by_metric=lambda m: [a for a in atoms if a.metric == m]))
        records = [{"id": a.id, "state": 1, "observation": "交付包含明确的完整内容。",
                    "reason": "交付具体内容与评分要求一致，并有以下逐字证据支持判定。",
                    "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "具体证据"}]} for a in atoms]
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / "events").mkdir(); (root / "checkpoints").mkdir()
            (root / "probe-ok.json").write_text("{}")
            (root / "checkpoints/judge-session.json").write_text(json.dumps({"fingerprint": "fixed", "session_id": "stale"}))
            locked = root / "checkpoints/atoms-000.json"
            locked.write_text(json.dumps({"fingerprint": "fixed", "atoms": records[:4]}))
            original = locked.read_bytes()
            def event(payload, session="stale"):
                return json.dumps({"type": "text", "sessionID": session,
                                   "part": {"text": "BEGIN_JUDGMENT " + json.dumps({"atoms": payload}) + " END_JUDGMENT"}})
            for attempt in range(2):
                (root / f"events/atoms-004-attempt-{attempt}.jsonl").write_text(event(records[:4]))
            calls = []
            def reply(workspace, prompt, title, output, *args, **kwargs):
                calls.append((prompt, kwargs["session_id"]))
                if len(calls) < 3:
                    payload = records[:4]
                else:
                    payload = [records[4] if "X4" in title else records[5]]
                answer = event(payload, kwargs["session_id"] or "fresh")
                output.write_text(answer, encoding="utf-8")
                return json.loads(answer)["part"]["text"], True
            manifest = {"fingerprint": "fixed", "process_record": {},
                        "files": [{"file": "delivery.md", "extracted": "delivery.md", "limitation": None}]}
            with patch("evaluation_judger.judge.workspace_for", return_value=(root, manifest)), \
                 patch("evaluation_judger.opencode.run", side_effect=reply):
                result = judge_task(task, "subject", {"batch_size": 4}, root, root)
            self.assertEqual([s for _, s in calls], ["stale", "stale", None, "fresh"])
            self.assertIn("received atom IDs ['X0', 'X1', 'X2', 'X3']", calls[1][0][:2000])
            self.assertEqual(locked.read_bytes(), original)
            self.assertEqual(len(result["atoms"]), 6)
            self.assertEqual(result["scores"]["quality_mean"], "5")
