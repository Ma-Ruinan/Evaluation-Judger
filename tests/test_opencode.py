import subprocess
import json
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.opencode import JudgeError, TaskSession, run


class InterruptedOutput(TestCase):
    def test_task_context_is_reused_without_crossing_participants_or_inputs(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            a, b = root / ("a" * 12), root / ("b" * 12)
            a.mkdir()
            b.mkdir()
            calls = []
            def reply(workspace, prompt, title, output, project, model, *, major, session_id):
                calls.append((workspace.name, session_id))
                output.write_text(json.dumps({"sessionID": session_id or "ses_" + workspace.name}), encoding="utf-8")
                return "{}", True
            with patch("evaluation_judger.opencode.run", side_effect=reply):
                first = TaskSession(a, root, "test", 1, "a" * 64)
                first.ask("one", "test", a / "one.jsonl")
                first.ask("two", "test", a / "two.jsonl")
                TaskSession(a, root, "test", 1, "a" * 64).ask("resume", "test", a / "resume.jsonl")
                TaskSession(b, root, "test", 1, "b" * 64).ask("other subject", "test", b / "one.jsonl")
                TaskSession(a, root, "test", 1, "changed inputs").ask("new", "test", a / "new.jsonl")
            self.assertEqual([session for _, session in calls], [None, "ses_" + a.name, "ses_" + a.name, None, None])

    def test_partial_length_limited_answer_continues_only_its_own_session(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            partial = '\n'.join(json.dumps(event) for event in [
                {"type": "text", "sessionID": "ses_test", "part": {"text": 'BEGIN_JUDGMENT {"atoms":['}},
                {"type": "step_finish", "sessionID": "ses_test", "part": {"reason": "length"}},
            ])
            complete = json.dumps({"type": "text", "sessionID": "ses_test", "part": {"text": 'BEGIN_JUDGMENT {"atoms":[]} END_JUDGMENT'}})
            responses = [SimpleNamespace(stdout=partial, stderr="", returncode=0), SimpleNamespace(stdout=complete, stderr="", returncode=0)]
            with patch("evaluation_judger.opencode.environment", return_value={"AIAAA_API_KEY": "sample-secret"}), \
                 patch("evaluation_judger.opencode._executable", return_value="opencode"), \
                 patch("evaluation_judger.opencode.subprocess.run", side_effect=responses) as process:
                answer, _ = run(root, "judge", "test", root / "events.jsonl", root, "test")
            self.assertIn("END_JUDGMENT", answer)
            command = process.call_args.args[0]
            self.assertEqual(command[command.index("--session") + 1], "ses_test")
            self.assertEqual((root / "events.jsonl").read_text(encoding="utf-8"), partial)
            self.assertTrue((root / "events-continuation.jsonl").exists())

    def test_empty_length_limited_response_identifies_smaller_group_recovery(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            response = SimpleNamespace(stdout='{"type":"step_finish","part":{"reason":"length"}}', stderr="", returncode=0)
            with patch("evaluation_judger.opencode.environment", return_value={"AIAAA_API_KEY": "sample-secret"}), \
                 patch("evaluation_judger.opencode._executable", return_value="opencode"), \
                 patch("evaluation_judger.opencode.subprocess.run", return_value=response):
                with self.assertRaisesRegex(JudgeError, "Response length limit"):
                    run(root, "judge", "test", root / "events.jsonl", root, "test")

    def test_timeout_preserves_partial_events_and_redacts_key(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "events" / "attempt.jsonl"
            failure = subprocess.TimeoutExpired("opencode", 10, output=b'completed atom; sample-secret')
            with patch("evaluation_judger.opencode.environment", return_value={"AIAAA_API_KEY": "sample-secret"}), \
                 patch("evaluation_judger.opencode._executable", return_value="opencode"), \
                 patch("evaluation_judger.opencode.subprocess.run", side_effect=failure):
                with self.assertRaises(JudgeError):
                    run(root, "judge", "test", output, root, "test", timeout=10)
            self.assertEqual(output.read_text(encoding="utf-8"), "completed atom; [REDACTED]")
