"""Command line entry point for one dataset per run."""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .dataset import discover, load_config, process_records, task_fingerprint
from .judge import judge_task
from .judge import write_json, write_text
from .render import make_report, summaries, task_markdown
from .reporting import make_formal_report
from .rubric import shared_sample_references
from .scoring import calculate, shared_items_match, result_is_consistent as _result_is_consistent




def _context(config_path: Path):
    config = load_config(config_path)
    project = Path(__file__).resolve().parent.parent
    run_dir = Path(config.get("run_dir", project / "runs" / config_path.stem)).resolve()
    tasks = discover(config)
    process = process_records(config)
    missing = [t.id for t in tasks if t.id not in process or any(s["id"] not in process[t.id] for s in config["subjects"])]
    if missing:
        raise ValueError(f"Process Excel lacks task/subject rows: {missing}")
    return config, project, run_dir, tasks


def _results(config, run_dir, tasks):
    result_map = {}
    missing = []
    for task in tasks:
        for subject in config["subjects"]:
            path = run_dir / "results" / task.id / f"{subject['id']}.json"
            if path.exists():
                try:
                    loaded = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    missing.append((task.id, subject["id"]))
                    continue
                needs_critical_review = (config.get("critical_error_review", False) and isinstance(loaded, dict)
                    and any(entry.get("triggered") for entry in loaded.get("errors", []))
                    and loaded.get("critical_error_review_version") != 1)
                if isinstance(loaded, dict) and not needs_critical_review and loaded.get("fingerprint") == task_fingerprint(task, subject["id"], config) and _result_is_consistent(task, loaded):
                    result_map[(task.id, subject["id"])] = loaded
                else:
                    missing.append((task.id, subject["id"]))
            else:
                missing.append((task.id, subject["id"]))
    return result_map, missing


_attempts_lock = threading.Lock()


def _run_task(task, config, project, run_dir):
    failures = []
    for subject in config["subjects"]:
        existing, _ = _results(config, run_dir, [task])
        if (task.id, subject["id"]) in existing:
            print(f"Reusing {task.id} / {subject['name']}", flush=True)
            continue
        print(f"Judging {task.id} / {subject['name']}", flush=True)
        started_at = datetime.now(timezone.utc).isoformat()
        started = time.perf_counter()
        outcome = 'incomplete'
        try:
            judge_task(task, subject["id"], config, project, run_dir)
            outcome = 'completed'
        except Exception as exc:
            # Retain all prior checkpoints and continue independent subjects/tasks.
            failure = {"task": task.id, "subject": subject["id"], "error_type": type(exc).__name__, "message": str(exc)}
            failures.append(failure)
            print(f"Incomplete {task.id} / {subject['name']}: {type(exc).__name__}: {exc}", flush=True)
        finally:
            archive = run_dir / 'operation-records' / 'judging-attempts.jsonl'
            archive.parent.mkdir(parents=True, exist_ok=True)
            record = {'record_type': 'evaluator_attempt', 'task': task.id, 'subject': subject['id'],
                      'started_at': started_at, 'finished_at': datetime.now(timezone.utc).isoformat(),
                      'elapsed_seconds': round(time.perf_counter()-started, 3), 'outcome': outcome}
            with _attempts_lock:
                with archive.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps(record, ensure_ascii=False)+'\n')
    return failures


def _run_tasks_once(tasks, config, project, run_dir):
    workers = config.get("task_workers", 1)
    if isinstance(workers, bool) or not isinstance(workers, int) or not 1 <= workers <= 4:
        raise ValueError("task_workers must be an integer in 1-4")
    if workers == 1:
        for task in tasks:
            yield task, _run_task(task, config, project, run_dir)
        return
    # Keep dimensions in their configured order. Each task has one owner;
    # subjects are judged sequentially inside that task's isolated workspaces.
    for dimension in dict.fromkeys(t.dimension for t in tasks):
        selected = [task for task in tasks if task.dimension == dimension]
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_run_task, task, config, project, run_dir): task for task in selected}
            for future in as_completed(futures):
                yield futures[future], future.result()


def _run_tasks(tasks, config, project, run_dir):
    deferred = {}
    for task, failures in _run_tasks_once(tasks, config, project, run_dir):
        if config.get("startup_deferred_retry", False):
            for failure in failures:
                root = run_dir / "workspaces" / task.id / failure["subject"]
                if (failure["error_type"] == "JudgeError"
                        and failure["message"] in {"OpenCode timed out after 180s", "OpenCode timed out before first event after 180s"}
                        and not any(root.glob("*/checkpoints/atoms-*.json"))):
                    deferred[task.id] = task
        yield task, failures
    # A bounded serial pass after other requests finish. Only unresolved
    # startup failures qualify; _run_task still reuses valid locked results.
    for task in deferred.values():
        print(f"Retrying deferred startup failure for {task.id}", flush=True)
        yield task, _run_task(task, config, project, run_dir)


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Evaluate one rubric dataset with OpenCode")
    parser.add_argument("command", choices=["prepare", "run", "status", "report"])
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    config, project, run_dir, tasks = _context(args.config)
    print(f"Dataset: {config['dataset']} | tasks: {len(tasks)} | subjects: {len(config['subjects'])} | run: {run_dir}", flush=True)
    if args.command == "prepare":
        for task in tasks:
            print(f"{task.id} {task.dimension}/{task.name}: {len(task.rubric.atoms)} atoms, {len(task.rubric.errors)} error rules; " + ", ".join(f"{s['id']}={len(task.deliveries[s['id']])} files" for s in config["subjects"]))
        return
    if args.command == "run":
        failures = []
        summarized_dimensions = set()
        for task, task_failures in _run_tasks(tasks, config, project, run_dir):
            failures.extend(task_failures)
            result_map, _ = _results(config, run_dir, tasks)
            failures = [failure for failure in failures
                        if (failure["task"], failure["subject"]) not in result_map]
            write_json(run_dir / "failures.json", failures)
            if all((task.id, subject["id"]) in result_map for subject in config["subjects"]):
                locked = [result_map[(task.id, subject["id"])] for subject in config["subjects"]]
                names = {subject["id"]: subject["name"] for subject in config["subjects"]}
                name = "compare-result.md" if len(locked) > 1 else "evaluation-result.md"
                write_text(run_dir / "results" / task.id / name, task_markdown(task, locked, names))
            dimension_tasks = [item for item in tasks if item.dimension == task.dimension]
            if task.dimension not in summarized_dimensions and all((item.id, subject["id"]) in result_map for item in dimension_tasks for subject in config["subjects"]):
                summaries(dimension_tasks, result_map, config, run_dir)
                summarized_dimensions.add(task.dimension)
        write_json(run_dir / "failures.json", failures)
    result_map, missing = _results(config, run_dir, tasks)
    print(f"Completed: {len(result_map)}/{len(tasks) * len(config['subjects'])}; missing: {missing}", flush=True)
    if args.command == "status":
        return
    if missing:
        raise SystemExit("Some judgments remain incomplete; rerun to resume. Report requires all configured task-subject judgments")
    dimensions = summaries(tasks, result_map, config, run_dir)
    mode = config.get("report_mode", "auto")
    if mode not in ("auto", "pilot", "formal"):
        raise ValueError("report_mode must be auto, pilot or formal")
    formal = mode == "formal" or mode == "auto" and (not config.get("tasks") or len(tasks) >= 4)
    report = make_formal_report(config, dimensions, run_dir, project) if formal else make_report(config, tasks, result_map, dimensions, run_dir)
    print(f"Report: {report}", flush=True)


if __name__ == "__main__":
    main()
