import subprocess
import sys
import json
import threading
from types import SimpleNamespace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.opencode import JudgeError, TaskSession, run, probe, extract_json, _run_with_first_event_timeout


class InterruptedOutput(TestCase):
    def test_events_are_persisted_before_an_interrupted_request_finishes(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); logged = threading.Event(); failures = []
            def callback(line):
                (root / 'live.jsonl').write_text(line)
                logged.set()
            def request():
                try:
                    _run_with_first_event_timeout([sys.executable, '-c', 'import time;print("event",flush=True);time.sleep(10)'], first_event_timeout=.5, timeout=1, text=True, encoding='utf-8', on_output=callback)
                except subprocess.TimeoutExpired as exc:
                    failures.append(exc)
            worker = threading.Thread(target=request); worker.start()
            self.assertTrue(logged.wait(.8))
            self.assertTrue(worker.is_alive())
            self.assertEqual((root / 'live.jsonl').read_text(), 'event\n')
            worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(failures), 1)

    def test_live_event_writes_redact_keys(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); output = root / 'events.jsonl'
            def invoke(*args, **kwargs):
                kwargs['on_output']('sample-secret\n')
                self.assertEqual(output.read_text(), '[REDACTED]\n')
                return SimpleNamespace(stdout=json.dumps({'type':'text','part':{'text':'BEGIN_JUDGMENT {} END_JUDGMENT'}}), stderr='', returncode=0)
            with patch('evaluation_judger.opencode.environment', return_value={'AIAAA_API_KEY':'sample-secret', 'OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS':'180'}), patch('evaluation_judger.opencode._executable',return_value='opencode'), patch('evaluation_judger.opencode._run_with_first_event_timeout',side_effect=invoke):
                run(root,'probe','title',output,root,'model')

    def test_array_envelope_is_opt_in_for_grading_only(self):
        answer = 'BEGIN_JUDGMENT [{"id":"A01","state":1}] END_JUDGMENT'
        with self.assertRaisesRegex(JudgeError, 'Expected JSON object'):
            extract_json(answer)
        self.assertEqual(extract_json(answer, allow_array=True), [{'id': 'A01', 'state': 1}])

    def test_total_deadline_shorter_than_first_event_deadline(self):
        command = [sys.executable, '-c', 'import time;time.sleep(10)']
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            _run_with_first_event_timeout(command, first_event_timeout=3, timeout=.5, text=True, encoding='utf-8')
        self.assertEqual(caught.exception.timeout, .5)
        self.assertEqual(caught.exception.first_event_timeout, .5)

    def test_health_agent_keeps_read_isolation_and_does_not_change_grading_profile(self):
        with TemporaryDirectory() as directory:
            root=Path(directory)
            judge={'variant':'high','permission':{'read':{'*':'deny','own/*':'allow'},'webfetch':'allow'}}
            (root/'opencode.jsonc').write_text(json.dumps({'agent':{'judge':judge}}))
            def reply(workspace,prompt,title,output,project,model,*args,**kwargs):
                self.assertEqual(model,'provider/model')
                self.assertEqual(kwargs['agent'],'read-probe')
                return 'BEGIN_JUDGMENT '+json.dumps({'nonce':(root/'probe.txt').read_text()})+' END_JUDGMENT',True
            with patch('evaluation_judger.opencode.run',side_effect=reply):probe(root,root,'provider/model#high')
            config=json.loads((root/'opencode.jsonc').read_text())
            self.assertEqual(config['agent']['judge'],judge)
            self.assertEqual(config['agent']['read-probe']['permission']['read'],judge['permission']['read'])
            self.assertEqual(config['agent']['read-probe']['permission']['webfetch'],'deny')
            self.assertNotIn('variant',config['agent']['read-probe'])

    def test_silent_child_is_bounded_and_stderr_is_preserved(self):
        command = [sys.executable, "-c", "import time,sys;sys.stderr.write('diagnostic');sys.stderr.flush();time.sleep(10)"]
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            _run_with_first_event_timeout(command, first_event_timeout=.5, timeout=3, text=True, encoding='utf-8')
        self.assertEqual(caught.exception.first_event_timeout, .5)
        self.assertEqual(caught.exception.stderr, 'diagnostic')

    def test_active_stream_retains_full_timeout_and_partial_events(self):
        command = [sys.executable, "-c", "import time;print('event',flush=True);time.sleep(.7);print('done',flush=True)"]
        complete = _run_with_first_event_timeout(command, first_event_timeout=.5, timeout=3, text=True, encoding='utf-8')
        self.assertEqual(complete.stdout.splitlines(), ['event','done'])
        command = [sys.executable, "-c", "import time;print('event',flush=True);time.sleep(10)"]
        with self.assertRaises(subprocess.TimeoutExpired) as caught:
            _run_with_first_event_timeout(command, first_event_timeout=.5, timeout=1, text=True, encoding='utf-8')
        self.assertEqual(caught.exception.output.strip(),'event')
        self.assertFalse(hasattr(caught.exception,'first_event_timeout'))

    def test_startup_retry_requires_successful_exact_read_roundtrip(self):
        with TemporaryDirectory() as directory:
            root=Path(directory);calls=[]
            def reply(workspace,prompt,title,output,*args):
                calls.append(output.name)
                if len(calls)==1:raise JudgeError("OpenCode timed out before any output")
                return "BEGIN_JUDGMENT "+json.dumps({"nonce":(root/"probe.txt").read_text()})+" END_JUDGMENT",True
            with patch("evaluation_judger.opencode.run",side_effect=reply):probe(root,root,"model")
            self.assertEqual(calls,["probe-events.jsonl","probe-events-retry.jsonl"])
            with patch("evaluation_judger.opencode.run",return_value=('BEGIN_JUDGMENT {"nonce":"invented"} END_JUDGMENT',False)) as model:
                with self.assertRaises(JudgeError):probe(root,root,"model")
                self.assertEqual(model.call_count,2)

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
            failure = subprocess.TimeoutExpired("opencode", 10, output=b'completed atom; sample-secret', stderr=b'diagnostic sample-secret')
            with patch("evaluation_judger.opencode.environment", return_value={"AIAAA_API_KEY": "sample-secret"}), \
                 patch("evaluation_judger.opencode._executable", return_value="opencode"), \
                 patch("evaluation_judger.opencode.subprocess.run", side_effect=failure):
                with self.assertRaises(JudgeError):
                    run(root, "judge", "test", output, root, "test", timeout=10)
            self.assertEqual(output.read_text(encoding="utf-8"), "completed atom; [REDACTED]")
            self.assertEqual(output.with_suffix(".stderr.txt").read_text(encoding="utf-8"), "diagnostic [REDACTED]")
