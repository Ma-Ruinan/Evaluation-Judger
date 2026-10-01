"""Command line entry point for one dataset per run."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from .dataset import discover, load_config, process_records, task_fingerprint
from .judge import judge_task
from .render import make_report, summaries
from .scoring import calculate, shared_items_match


def _result_is_consistent(task, result: dict) -> bool:
    try:
        records = result["atoms"]
        indexed = {r["id"]: r for r in records}
        if len(indexed) != len(task.rubric.atoms):
            return False
        if calculate(task.rubric, records, result["errors"]) != result["scores"]:
            return False
        for atom in task.rubric.atoms:
            match = re.search(r"同\s*([A-Z]\d+)", atom.rule)
            if match and match.group(1) in indexed and atom.kind in ("RATIO", "CLAIM-RATIO"):
                source, target = indexed[match.group(1)], indexed[atom.id]
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
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if loaded.get("fingerprint") == task_fingerprint(task, subject["id"], config) and _result_is_consistent(task, loaded):
                    result_map[(task.id, subject["id"])] = loaded
                else:
                    missing.append((task.id, subject["id"]))
            else:
                missing.append((task.id, subject["id"]))
    return result_map, missing


def main(argv=None):
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
        for task in tasks:
            for subject in config["subjects"]:
                print(f"Judging {task.id} / {subject['name']}", flush=True)
                judge_task(task, subject["id"], config, project, run_dir)
            result_map, _ = _results(config, run_dir, tasks)
            dimension_tasks = [item for item in tasks if item.dimension == task.dimension]
            if all((item.id, subject["id"]) in result_map for item in dimension_tasks for subject in config["subjects"]):
                summaries(dimension_tasks, result_map, config, run_dir)
    result_map, missing = _results(config, run_dir, tasks)
    print(f"Completed: {len(result_map)}/{len(tasks) * len(config['subjects'])}; missing: {missing}", flush=True)
    if args.command == "status":
        return
    if missing:
        raise SystemExit("Report requires all configured task-subject judgments")
    dimensions = summaries(tasks, result_map, config, run_dir)
    report = make_report(config, tasks, result_map, dimensions, run_dir)
    print(f"Report: {report}", flush=True)


if __name__ == "__main__":
    main()
