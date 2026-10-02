"""Command line entry point for one dataset per run."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .dataset import discover, load_config, process_records, task_fingerprint
from .judge import judge_task
from .judge import write_json
from .render import make_report, summaries, task_markdown
from .reporting import make_formal_report
from .rubric import shared_sample_references
from .scoring import calculate, shared_items_match


def _result_is_consistent(task, result: dict) -> bool:
    try:
        records = result["atoms"]
        indexed = {r["id"]: r for r in records}
        if len(indexed) != len(task.rubric.atoms):
            return False
        if calculate(task.rubric, records, result["errors"]) != result["scores"]:
            return False
        if any(len(shared_sample_references(atom.rule)) > 1 for atom in task.rubric.atoms) and result.get("multi_sample_review_version") != 3:
            return False
        for atom in task.rubric.atoms:
            references = shared_sample_references(atom.rule)
            if len(references) == 1 and references[0] in indexed and atom.kind in ("RATIO", "CLAIM-RATIO"):
                source, target = indexed[references[0]], indexed[atom.id]
                required = int(source["denominator"])
                if int(target["denominator"]) != required or len(source["items"]) != required or len(target["items"]) != required or not shared_items_match(source["items"], target["items"]):
                    return False
        return True
    except (KeyError, ValueError, TypeError):
        return False


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
                if isinstance(loaded, dict) and loaded.get("fingerprint") == task_fingerprint(task, subject["id"], config) and _result_is_consistent(task, loaded):
                    result_map[(task.id, subject["id"])] = loaded
                else:
                    missing.append((task.id, subject["id"]))
            else:
                missing.append((task.id, subject["id"]))
    return result_map, missing


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
        for task in tasks:
            for subject in config["subjects"]:
                existing, _ = _results(config, run_dir, [task])
                if (task.id, subject["id"]) in existing:
                    print(f"Reusing {task.id} / {subject['name']}", flush=True)
                    continue
                print(f"Judging {task.id} / {subject['name']}", flush=True)
                try:
                    judge_task(task, subject["id"], config, project, run_dir)
                except Exception as exc:
                    # Retain all prior checkpoints and continue independent subjects/tasks.
                    failure = {"task": task.id, "subject": subject["id"], "error_type": type(exc).__name__, "message": str(exc)}
                    failures.append(failure)
                    write_json(run_dir / "failures.json", failures)
                    print(f"Incomplete {task.id} / {subject['name']}: {type(exc).__name__}: {exc}", flush=True)
            result_map, _ = _results(config, run_dir, tasks)
            if all((task.id, subject["id"]) in result_map for subject in config["subjects"]):
                locked = [result_map[(task.id, subject["id"])] for subject in config["subjects"]]
                names = {subject["id"]: subject["name"] for subject in config["subjects"]}
                name = "compare-result.md" if len(locked) > 1 else "evaluation-result.md"
                (run_dir / "results" / task.id / name).write_text(task_markdown(task, locked, names), encoding="utf-8")
            dimension_tasks = [item for item in tasks if item.dimension == task.dimension]
            if all((item.id, subject["id"]) in result_map for item in dimension_tasks for subject in config["subjects"]):
                summaries(dimension_tasks, result_map, config, run_dir)
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
