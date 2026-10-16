from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import json
import os

from evaluation_judger.supervisor import supervise, _report_ready, _progress


class AutomaticResume(TestCase):
    def setup_run(self, root):
        config_path = root / 'config.json'
        config_path.write_text('{}')
        question = root / 'question.txt'; question.write_text('fixed question')
        rubric = root / 'rubric.md'; rubric.write_text('fixed rubric')
        excel = root / 'process.xlsx'; excel.write_text('fixed record')
        task = SimpleNamespace(question=question, rubric=SimpleNamespace(path=rubric), sources=(), deliveries={})
        return config_path, {'subjects': [{}, {}], 'process_excel': {'path': str(excel)}, 'report_mode': 'pilot'}, [task]

    def test_formal_fallback_is_retried_with_existing_scores(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root)
            config['report_mode'] = 'formal'
            run = root / 'run'; reports = run / 'reports'; reports.mkdir(parents=True)
            (reports/'evaluation-report.docx').write_text('report')
            (reports/'narrative.json').write_text(json.dumps({'facts_hash':'fixed','narrative':{'source':'deterministic_fallback'}}))
            self.assertFalse(_report_ready(config,run,tasks))
            before = _progress(run)
            def worker(*_):
                (reports/'writer-draft.json').write_text('{"draft":"repaired"}')
                self.assertNotEqual(_progress(run),before)
                (reports/'narrative.json').write_text(json.dumps({'facts_hash':'fixed','narrative':{'source':'opencode'}}))
                (reports/'narrative-review.json').write_text(json.dumps({'ok':True,'facts_hash':'other'}))
                self.assertFalse(_report_ready(config,run,tasks))
                (reports/'narrative-review.json').write_text(json.dumps({'ok':True,'facts_hash':'fixed'}))
                return 0
            with patch('evaluation_judger.cli._results',return_value=({0:{},1:{}},[])),patch('evaluation_judger.supervisor._worker',side_effect=worker) as call:
                self.assertEqual(supervise(path,config,root,run,tasks,retry_delay=0),0)
                self.assertEqual(call.call_count,1)

    def test_retries_native_command_and_accepts_only_complete_report(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root)
            run = root / 'run'; count = [0]
            def results(*_): return ({i: {} for i in range(count[0])}, list(range(count[0], 2)))
            def worker(command, *_):
                self.assertNotIn('--auto-resume', command)
                count[0] += 1
                if count[0] == 2:
                    report = run / 'reports/evaluation-report.docx'
                    report.parent.mkdir(); report.write_text('report')
                    return 0
                return 1
            with patch('evaluation_judger.cli._results', side_effect=results), patch('evaluation_judger.supervisor._worker', side_effect=worker):
                self.assertEqual(supervise(path, config, root, run, tasks, retry_delay=0), 0)
                self.assertEqual(count[0], 2)
                self.assertEqual(supervise(path, config, root, run, tasks, max_attempts=2, retry_delay=0), 0)
            state = json.loads((run / 'operation-records/auto-resume-state.json').read_text())
            self.assertEqual(state['status'], 'completed')

    def test_no_progress_stops_without_synthesizing_results(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root); run = root / 'run'
            with patch('evaluation_judger.cli._results', return_value=({}, [1, 2])), patch('evaluation_judger.supervisor._worker', return_value=1) as worker:
                self.assertEqual(supervise(path, config, root, run, tasks, max_attempts=8, retry_delay=0), 1)
                self.assertEqual(worker.call_count, 3)
            self.assertFalse((run / 'results').exists())
            self.assertFalse((run / 'reports').exists())

    def test_success_exit_without_report_does_not_complete(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root)
            with patch('evaluation_judger.cli._results', return_value=({0: {}, 1: {}}, [])), patch('evaluation_judger.supervisor._worker', return_value=0):
                self.assertEqual(supervise(path, config, root, root / 'run', tasks, max_attempts=1, retry_delay=0), 1)

    def test_budget_persists_before_interrupted_worker(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root); run = root / 'run'
            with patch('evaluation_judger.cli._results', return_value=({}, [1, 2])):
                with patch('evaluation_judger.supervisor._worker', side_effect=RuntimeError('interrupted')):
                    with self.assertRaisesRegex(RuntimeError, 'interrupted'):
                        supervise(path, config, root, run, tasks, max_attempts=2, retry_delay=0)
                with patch('evaluation_judger.supervisor._worker', return_value=1) as worker:
                    self.assertEqual(supervise(path, config, root, run, tasks, max_attempts=2, retry_delay=0), 1)
                    self.assertEqual(worker.call_count, 1)

    def test_changed_inputs_block_resume(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root); run = root / 'run'
            with patch('evaluation_judger.cli._results', return_value=({}, [1, 2])), patch('evaluation_judger.supervisor._worker', return_value=1):
                supervise(path, config, root, run, tasks, max_attempts=1, retry_delay=0)
                tasks[0].question.write_text('changed question')
                with self.assertRaisesRegex(ValueError, 'changed'):
                    supervise(path, config, root, run, tasks, max_attempts=2, retry_delay=0)

    def test_duplicate_running_supervisor_is_rejected(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root); run = root / 'run'
            lock = run / 'operation-records/auto-resume.lock'; lock.parent.mkdir(parents=True)
            lock.write_text(json.dumps({'pid': os.getpid()}))
            with self.assertRaisesRegex(RuntimeError, 'still active'):
                supervise(path, config, root, run, tasks, retry_delay=0)
            self.assertTrue(lock.exists())

    def test_authentication_failure_is_not_repeated(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); path, config, tasks = self.setup_run(root); run = root / 'run'
            def worker(*_):
                (run / 'failures.json').write_text(json.dumps([{'message': 'Unauthorized'}]))
                return 1
            with patch('evaluation_judger.cli._results', return_value=({}, [1, 2])), patch('evaluation_judger.supervisor._worker', side_effect=worker) as call:
                self.assertEqual(supervise(path, config, root, run, tasks, retry_delay=0), 1)
                self.assertEqual(call.call_count, 1)
