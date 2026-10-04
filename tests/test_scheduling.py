from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import json
import threading
import time
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.cli import _run_tasks


class TaskScheduling(TestCase):
    def test_deferred_startup_retry_is_bounded_serial_and_skips_partial_judgments(self):
        tasks = [SimpleNamespace(id=str(i), dimension="A") for i in range(3)]
        config = {"startup_deferred_retry": True}
        error = lambda t: {"task": t.id, "subject": "x", "error_type": "JudgeError", "message": "OpenCode timed out after 180s"}
        with TemporaryDirectory() as folder:
            root = Path(folder)
            started = root / "workspaces/1/x/fingerprint/checkpoints/atoms-000.json"
            started.parent.mkdir(parents=True)
            started.write_text('{}')
            initial = [(tasks[0], [error(tasks[0])]), (tasks[1], [error(tasks[1])]), (tasks[2], [])]
            with patch("evaluation_judger.cli._run_tasks_once", return_value=iter(initial)), \
                 patch("evaluation_judger.cli._run_task", return_value=[error(tasks[0])]) as retry:
                completed = list(_run_tasks(tasks, config, root, root))
            self.assertEqual(completed[:3], initial)
            self.assertEqual(len(completed), 4)
            retry.assert_called_once_with(tasks[0], config, root, root)

    def test_concurrent_tasks_preserve_subject_order_dimension_barrier_and_failures(self):
        tasks = [SimpleNamespace(id=str(i), dimension="A" if i < 2 else "B") for i in range(3)]
        config = {"subjects": [{"id": "x", "name": "X"}, {"id": "y", "name": "Y"}], "task_workers": 2}
        events, lock = [], threading.Lock()
        def judge(task, subject, *_):
            with lock: events.append(("start", task.id, subject))
            time.sleep(.02)
            with lock: events.append(("end", task.id, subject))
            if task.id == "0" and subject == "x":
                raise ValueError("bounded failure")
        with TemporaryDirectory() as folder, patch("evaluation_judger.cli._results", return_value=({}, [])), \
             patch("evaluation_judger.cli.judge_task", side_effect=judge):
            root = Path(folder)
            completed = list(_run_tasks(tasks, config, root, root))
            self.assertEqual({task.id for task, _ in completed}, {"0", "1", "2"})
            failures = [failure for _, group in completed for failure in group]
            self.assertEqual([(f["task"], f["subject"]) for f in failures], [("0", "x")])
            archive = [json.loads(line) for line in (root / "operation-records/judging-attempts.jsonl").read_text().splitlines()]
            self.assertEqual(len(archive), 6)
        for task in tasks:
            self.assertLess(events.index(("end", task.id, "x")), events.index(("start", task.id, "y")))
        second_dimension_start = events.index(("start", "2", "x"))
        self.assertTrue(all(events.index(("end", task.id, "y")) < second_dimension_start for task in tasks[:2]))

    def test_default_schedule_is_serial_and_completed_results_never_call_judge(self):
        task = SimpleNamespace(id="1", dimension="A")
        config = {"subjects": [{"id": "x", "name": "X"}]}
        with patch("evaluation_judger.cli._results", return_value=({("1", "x"): {}}, [])), \
             patch("evaluation_judger.cli.judge_task") as judge:
            self.assertEqual(list(_run_tasks([task], config, Path("."), Path("."))), [(task, [])])
            judge.assert_not_called()
