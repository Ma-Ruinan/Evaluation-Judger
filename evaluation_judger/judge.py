"""Run isolated task-subject judgments with resumable atom batches."""
from __future__ import annotations

import json
import shutil
import hashlib
from datetime import datetime
from pathlib import Path

from .dataset import Task, process_records, task_fingerprint
from .materials import extract
from .multisample import review_multi_atom
from .opencode import JudgeError, TaskSession, extract_json, parse_events, probe, run
from .rubric import shared_sample_references
from .scoring import calculate, shared_items_match, validate_atom


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def needs_time_review(atom, record: dict, process_record: dict) -> bool:
    return (record.get("state") == "0" and atom.kind == "BIN"
            and "实际执行" in atom.rule and "截止" in atom.rule
            and not process_record.get("executed_at"))


def atom_batches(atoms, size: int):
    """Keep long claim audits separate, while batching smaller rubric checks."""
    if size < 1:
        raise ValueError("batch_size must be positive")
    index = 0
    while index < len(atoms):
        end = index + 1
        if atoms[index].kind != "CLAIM-RATIO":
            while end < len(atoms) and end - index < size and atoms[end].kind != "CLAIM-RATIO":
                end += 1
        yield index, atoms[index:end]
        index = end


def workspace_for(task: Task, subject: str, config: dict, project: Path, run_dir: Path) -> tuple[Path, dict]:
    digest = task_fingerprint(task, subject, config)
    workspace = run_dir / "workspaces" / task.id / subject / digest[:12]
    workspace.mkdir(parents=True, exist_ok=True)
    config_file = "opencode.v1.example.jsonc" if config.get("opencode_major", 1) == 1 else "opencode.example.jsonc"
    shutil.copy2(project / config_file, workspace / "opencode.jsonc")
    agent_dir = workspace / ".opencode" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(project / ".opencode" / "agents" / "judge.md", agent_dir / "judge.md")
    items = [("question", task.question), ("rubric", task.rubric.path)]
    items += [("sources", p) for p in task.sources]
    items += [("deliveries", p) for p in task.deliveries[subject]]
    manifest = {"task": task.id, "subject": subject, "fingerprint": digest, "files": []}
    for category, path in items:
        relative = Path(category) / path.relative_to(task.directory)
        destination = workspace / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists() or destination.stat().st_size != path.stat().st_size:
            shutil.copy2(path, destination)
        text, limitation = extract(path)
        extracted = workspace / "extracted" / (str(relative).replace("\\", "__").replace("/", "__") + ".txt")
        extracted.parent.mkdir(parents=True, exist_ok=True)
        extracted.write_text(text, encoding="utf-8")
        manifest["files"].append({"category": category, "file": relative.as_posix(), "extracted": extracted.relative_to(workspace).as_posix(), "limitation": limitation, "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds")})
    process = process_records(config)[task.id][subject]
    process_path = workspace / "process-record.json"
    write_json(process_path, process)
    manifest["process_record"] = process
    manifest["files"].append({"category": "process", "file": "process-record.json", "extracted": "process-record.json", "limitation": None})
    write_json(workspace / "manifest.json", manifest)
    return workspace, manifest


def _prompt(task: Task, subject: str, atoms, manifest: dict, previous_error: str = "") -> str:
    files = [f"- {item['file']} => {item['extracted']}" + (f" [modified {item['modified_at']}]" if item.get("modified_at") else "") + (f" [LIMITATION: {item['limitation']}]" if item["limitation"] else "") for item in manifest["files"]]
    rules = [dict(id=a.id, metric=a.metric, weight=str(a.weight), purpose=a.purpose, rule=a.rule, required_evidence=a.evidence) for a in atoms]
    time_notes = [line.strip() for line in task.rubric.text.splitlines() if ("评测基准日" in line or "时间／版本边界" in line)][:5]
    return f"""You evaluate task {task.id} ({task.name}) for participant {subject}. Use only this participant's delivery, task question, rubric, process record and common sources in this isolated workspace. Read the extracted files with the read tool when they have not already been read in THIS same task-participant session, including the participant delivery and relevant source anchors. Reuse already inspected evidence where applicable; inspect any additional scope required by the assigned atoms. The extracted text has line/page/paragraph locators. Original files are also present when extraction is limited.

Files:\n{chr(10).join(files)}
Process record: {json.dumps(manifest['process_record'], ensure_ascii=False, default=str)}
Rubric time context: {json.dumps(time_notes, ensure_ascii=False)}. The process execution timestamp is {manifest['process_record'].get('executed_at') or 'not recorded'}. A rubric preparation date is not automatically a participant execution date. If a delivered report states a cutoff matching its preserved file modification date and no stronger execution timestamp contradicts it, treat that as supportive context. Record the inference and its limitation. Do not deduct solely because a reused delivery is later than the rubric preparation baseline.

Assigned rubric atoms (verbatim rule text):\n{json.dumps(rules, ensure_ascii=False, indent=2)}

Return one decision for EVERY assigned atom, in this order. Use the exact rubric conditions. For BIN state is 0 or 1. For RATIO/CLAIM-RATIO/COUNT give numerator, denominator, state= numerator/denominator, and an `items` array that identifies each counted element, its result and supporting location. If the rule specifies an empty-set state, use it with numerator=denominator=0. If the denominator is an exhaustive set, inspect the entire relevant delivery; do not silently sample. For external sources, use webfetch only when needed and supply full HTTPS URL. Historical reuse of a submission is not itself a defect; judge time-sensitive claims as of its execution/cutoff date, while following the rubric's substantive requirements.

Each decision must have id, state, observation (what the delivery actually says or lacks), reason (specific explanation linking observation to rule), evidence array of {{file,locator,quote}}, and ratio fields where applicable. Evidence `file` must be one of the listed original or extracted paths, or a full HTTPS URL. For a missing item, identify which delivery files and sections you checked; use the inspected delivery as evidence. Quotes should be short and literal where text exists; do not join distant passages with ellipses in one quote. Avoid generic reasons such as 'insufficient' without a concrete explanation. Explain uncertain evidence and still reach a supported score; do not invent a negative finding from lack of access.

Write observations, reasons and item explanations in clear Chinese. Each item explanation must address that exact item's subject, predicate, date and scope; support for another statement in the same paragraph does not establish support for this statement. A related reference title alone does not establish its unseen contents. State what was actually inspected and any access limitations, then apply the rubric without inventing a negative finding. Preserve literal evidence quotes in their original language. Return JSON only between BEGIN_JUDGMENT and END_JUDGMENT, shape: {{"atoms":[...]}}. Do not omit any atom.
{('Previous attempt failed validation: ' + previous_error) if previous_error else ''}
"""


def _error_prompt(task: Task, subject: str, manifest: dict, records: list[dict]) -> str:
    rules = [dict(id=e.id, trigger=e.trigger, effects=e.effects) for e in task.rubric.errors]
    states = {r["id"]: {"state": r["state"], "reason": r["reason"]} for r in records}
    return f"""Evaluate whether any critical error cap is triggered for task {task.id}, participant {subject}. Read the delivery and relevant extracted source files with the read tool. The files are listed in manifest.json. Apply each trigger only when specific evidence supports it; lack of access to an external site is not proof of fabrication. Completed atom decisions: {json.dumps(states, ensure_ascii=False)}. Reuse the locked atom states for any trigger referring to an atom ratio; do not recalculate it differently. Return every error code with triggered true/false, concrete reason, and evidence array of {{file,locator,quote}} when triggered. Full HTTPS URLs are allowed for external evidence. Rules: {json.dumps(rules, ensure_ascii=False)}. Return BEGIN_JUDGMENT then JSON object {{"errors":[...]}} then END_JUDGMENT."""


def judge_task(task: Task, subject: str, config: dict, project: Path, run_dir: Path) -> dict:
    workspace, manifest = workspace_for(task, subject, config, project, run_dir)
    model = config.get("model", "aiaaa/deepseek-v4.1-flash#high")
    major = int(config.get("opencode_major", 1))
    if not (workspace / "probe-ok.json").exists():
        probe(workspace, project, model, major)
        write_json(workspace / "probe-ok.json", {"ok": True})
    known_files = {item[key] for item in manifest["files"] for key in ("file", "extracted")} | {"manifest.json"}
    session = TaskSession(workspace, project, model, major, manifest["fingerprint"])
    atoms = task.rubric.atoms
    batch_size = int(config.get("batch_size", 4))
    saved_atoms = {}
    by_id = {atom.id: atom for atom in atoms}
    for old_checkpoint in sorted((workspace / "checkpoints").glob("atoms-*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            saved = json.loads(old_checkpoint.read_text(encoding="utf-8"))
            if saved.get("fingerprint") != manifest["fingerprint"]:
                continue
            for raw in saved["atoms"]:
                if raw.get("id") in by_id and raw["id"] not in saved_atoms:
                    saved_atoms[raw["id"]] = validate_atom(raw, by_id[raw["id"]], known_files)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    records = []
    for index, batch in atom_batches(atoms, batch_size):
        checkpoint = workspace / "checkpoints" / f"atoms-{index:03d}.json"
        if all(a.id in saved_atoms for a in batch):
            records += [saved_atoms[a.id] for a in batch]
            continue
        event_dir = workspace / "events"
        event_dir.mkdir(parents=True, exist_ok=True)
        event_glob = "atoms-*.jsonl"
        found: dict[str, dict] = {a.id: saved_atoms[a.id] for a in batch if a.id in saved_atoms}

        def harvest() -> None:
            for old_event in sorted(event_dir.glob(event_glob), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    old_answer = parse_events(old_event.read_text(encoding="utf-8"))[0]
                    old_raw = extract_json(old_answer).get("atoms", [])
                except (JudgeError, ValueError, KeyError, TypeError):
                    continue
                for raw in old_raw:
                    atom = next((a for a in batch if a.id == raw.get("id")), None) if isinstance(raw, dict) else None
                    if atom is None or atom.id in found:
                        continue
                    try:
                        found[atom.id] = validate_atom(raw, atom, known_files)
                    except (ValueError, KeyError, TypeError):
                        continue

        harvest()
        if not found:
            for attempt in range(2):
                event = event_dir / f"atoms-{index:03d}-attempt-{attempt}.jsonl"
                if event.exists():
                    continue
                try:
                    session.ask(_prompt(task, subject, batch, manifest), f"judge {task.id} {subject} atoms {index + 1}-{index + len(batch)}", event)
                except JudgeError as exc:
                    if "Response length limit" in str(exc):
                        break  # Repeating the same large group is unlikely to help.
                    pass  # The raw event remains available for salvage on this or a later run.
                harvest()
                if found:
                    break
        for atom in batch:
            if atom.id in found:
                continue
            for attempt in range(3):
                prompt = _prompt(task, subject, [atom], manifest, "Earlier output for this atom was incomplete. Supply a numeric state and an itemized check for every counted element.")
                event = event_dir / f"atoms-{index:03d}-{atom.id}-attempt-{attempt}.jsonl"
                try:
                    session.ask(prompt, f"repair {task.id} {subject} {atom.id}", event)
                except JudgeError:
                    pass
                harvest()
                if atom.id in found:
                    break
                if attempt == 2:
                    raise JudgeError(f"{task.id}/{subject}: atom {atom.id} could not be validated after targeted retries")
        checked = [found[a.id] for a in batch]
        write_json(checkpoint, {"fingerprint": manifest["fingerprint"], "atoms": checked})
        records += checked
        print(f"  {task.id}/{subject}: {len(records)}/{len(atoms)} atoms", flush=True)
    for position, atom in enumerate(atoms):
        record = records[position]
        if not needs_time_review(atom, record, manifest["process_record"]):
            continue
        dates = sorted({item["modified_at"][:10] for item in manifest["files"] if item["category"] == "deliveries" and item.get("modified_at")})
        if not dates:
            continue
        checkpoint = workspace / "checkpoints" / f"time-review-{atom.id}.json"
        if checkpoint.exists():
            reviewed = json.loads(checkpoint.read_text(encoding="utf-8"))["atom"]
        else:
            prompt = f"""Review ONLY time-sensitive rubric atom {atom.id} for task {task.id}, subject {subject}. The original decision was {json.dumps(record, ensure_ascii=False)}. Preserved delivery file modification dates are {dates}; see manifest.json for each file. The process execution timestamp is {manifest['process_record'].get('executed_at') or 'not recorded'}. The rubric preparation baseline date is not conclusive evidence of this reused delivery's actual execution date. User policy: historical reuse itself must not cause a time penalty. Read the delivered report's stated cutoff and manifest.json using the read tool. Decide whether the stated cutoff matches the best available execution-date evidence within the rubric tolerance. Explain evidence and limitations. Return one full atom record with id, state, observation, reason, evidence array of {{file,locator,quote}} between BEGIN_JUDGMENT and END_JUDGMENT as JSON object {{"atom":{{...}}}}."""
            answer, _ = run(workspace, prompt, f"review {task.id} {subject} {atom.id} time", workspace / "events" / f"time-review-{atom.id}.jsonl", project, model, major=major)
            reviewed = validate_atom(extract_json(answer)["atom"], atom, known_files)
            write_json(checkpoint, {"atom": reviewed})
        records[position] = reviewed
    indexed = {record["id"]: i for i, record in enumerate(records)}
    for atom in atoms:
        references = shared_sample_references(atom.rule)
        if len(references) != 1 or references[0] not in indexed or atom.kind not in ("RATIO", "CLAIM-RATIO"):
            continue
        source_id = references[0]
        source_atom = next(a for a in atoms if a.id == source_id)
        source_pos, target_pos = indexed[source_id], indexed[atom.id]

        def repair(current_atom, current_record, instruction: str, expected_count: int | None, label: str, expected_items: list | None = None) -> dict:
            key = _digest({"fingerprint": manifest["fingerprint"], "record": current_record, "instruction": instruction})
            path = workspace / "checkpoints" / f"shared-review-{label}.json"
            if path.exists():
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("input_hash") == key:
                    cached = validate_atom(saved["atom"], current_atom, known_files)
                    if expected_items is None or shared_items_match(expected_items, cached["items"]):
                        return cached
            def check(raw: dict) -> dict:
                # Checkpoints are previous judgments, never primary factual evidence.
                raw = dict(raw)
                raw["evidence"] = [ev for ev in raw.get("evidence", []) if not str(ev.get("file", "")).startswith(("checkpoints/", "events/"))]
                reviewed = validate_atom(raw, current_atom, known_files)
                if expected_count is not None and (int(reviewed["denominator"]) != expected_count or len(reviewed["items"]) != expected_count):
                    raise ValueError(f"Expected exactly {expected_count} itemized claims")
                if expected_count is None and int(reviewed["denominator"]) != len(reviewed["items"]):
                    raise ValueError("Claim list must match denominator")
                if expected_items is not None and not shared_items_match(expected_items, reviewed["items"]):
                    raise ValueError("Reviewed claim list differs from the locked sample")
                return reviewed
            for old_event in sorted((workspace / "events").glob(f"shared-review-{label}-attempt-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
                try:
                    old_answer = parse_events(old_event.read_text(encoding="utf-8"))[0]
                    reviewed = check(extract_json(old_answer)["atom"])
                    write_json(path, {"input_hash": key, "atom": reviewed})
                    return reviewed
                except (JudgeError, ValueError, KeyError, TypeError):
                    continue
            for attempt in range(2):
                prompt = f"""Review ONLY atom {current_atom.id} for task {task.id}, subject {subject}. Read relevant delivery, rubric and sources with the read tool. Existing decision: {json.dumps(current_record, ensure_ascii=False)}. {instruction} Cite delivery files, original sources or external URLs as factual evidence. Do not cite this project's checkpoints, events or earlier scores as factual evidence. Return a complete atom record with numeric state, observation, concrete reason, evidence with file/locator/quote, numerator, denominator and every itemized check. Return BEGIN_JUDGMENT then JSON {{"atom":{{...}}}} then END_JUDGMENT."""
                answer, _ = run(workspace, prompt, f"review shared sample {task.id} {subject} {current_atom.id}", workspace / "events" / f"shared-review-{label}-attempt-{attempt}.jsonl", project, model, major=major)
                try:
                    reviewed = check(extract_json(answer)["atom"])
                    write_json(path, {"input_hash": key, "atom": reviewed})
                    return reviewed
                except (ValueError, KeyError, TypeError, JudgeError):
                    if attempt == 1:
                        raise JudgeError(f"{task.id}/{subject}: shared sample {label} remains inconsistent")
            raise AssertionError("unreachable")

        source = records[source_pos]
        source_repaired = False
        if source["denominator"] is not None and len(source["items"]) != int(source["denominator"]):
            records[source_pos] = repair(source_atom, source, f"Expand the existing {source_id} sampled claim list so each counted claim has its own item. Recheck numerator and denominator if the prior count was wrong. The associated {atom.id} rule explicitly requires the same sample.", None, source_id)
            source = records[source_pos]
            source_repaired = True
        target = records[target_pos]
        required = int(source["denominator"])
        if source_repaired or int(target["denominator"]) != required or len(target["items"]) != required or not shared_items_match(source["items"], target["items"]):
            instruction = f"The rubric requires exactly the SAME sampled claims as {source_id}. Use this locked {source_id} list, one corresponding {atom.id} item per claim, in the same order: {json.dumps(source['items'], ensure_ascii=False)}. Do not introduce or omit claims. The denominator must be {required}. Judge each item by {atom.rule}. Each explanation and supporting source must address the exact locked claim's subject, predicate and scope. A paragraph or line can contain several different claims: support for another claim on the same line does not support this claim. Copying a label while judging a different statement is invalid. Show which concrete source passage supports the locked statement; a related source title alone does not establish support."
            records[target_pos] = repair(atom, target, instruction, required, atom.id, source["items"])
    for atom in atoms:
        references = shared_sample_references(atom.rule)
        if len(references) < 2 or atom.kind not in ("RATIO", "CLAIM-RATIO") or any(ref not in indexed for ref in references):
            continue
        position = indexed[atom.id]
        records[position] = review_multi_atom(atom, {ref: records[indexed[ref]] for ref in references}, records[position],
                                              task_id=task.id, subject=subject, workspace=workspace,
                                              fingerprint=manifest["fingerprint"], known_files=known_files,
                                              project=project, model=model, major=major)
    error_checkpoint = workspace / "checkpoints" / "errors.json"
    error_hash = _digest(records)
    cached_errors = json.loads(error_checkpoint.read_text(encoding="utf-8")) if error_checkpoint.exists() else {}
    if cached_errors.get("input_hash") == error_hash:
        decisions = cached_errors["errors"]
    elif task.rubric.errors:
        answer, _ = session.ask(_error_prompt(task, subject, manifest, records), f"judge {task.id} {subject} critical errors", workspace / "events" / "errors.jsonl")
        decisions = extract_json(answer)["errors"]
        expected = {e.id for e in task.rubric.errors}
        if {e.get("id") for e in decisions} != expected or len(decisions) != len(expected):
            raise JudgeError(f"{task.id}/{subject}: incomplete error decisions")
        for entry in decisions:
            if not isinstance(entry.get("triggered"), bool) or not str(entry.get("reason", "")).strip():
                raise JudgeError(f"{task.id}/{subject}: invalid error decision {entry.get('id')}")
            if entry["triggered"] and not entry.get("evidence"):
                raise JudgeError(f"{task.id}/{subject}: triggered error needs evidence")
            for ev in entry.get("evidence", []):
                if ev.get("file") not in known_files and not str(ev.get("file", "")).startswith("https://"):
                    raise JudgeError(f"{task.id}/{subject}: unknown critical-error evidence file {ev.get('file')}")
        write_json(error_checkpoint, {"input_hash": error_hash, "errors": decisions})
    else:
        decisions = []
    result = {
        "task_id": task.id, "task_name": task.name, "dimension": task.dimension,
        "subject": subject, "fingerprint": manifest["fingerprint"],
        "multi_sample_review_version": 3,
        "input_files": manifest["files"], "atoms": records, "errors": decisions,
        "scores": calculate(task.rubric, records, decisions),
    }
    write_json(run_dir / "results" / task.id / f"{subject}.json", result)
    return result
