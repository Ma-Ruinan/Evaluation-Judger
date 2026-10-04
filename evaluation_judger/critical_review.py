"""Independent, resumable audit of evidence used for severe score ceilings."""
from __future__ import annotations

import hashlib
import json
import re

from .opencode import JudgeError, extract_json, run, parse_events, event_session
from .scoring import number, shared_items_match, validate_atom


def review_context(task, records, decisions):
    rules = {rule.id: rule for rule in task.rubric.errors}
    atoms = {atom.id: atom for atom in task.rubric.atoms}
    affected = {metric for entry in decisions if entry['triggered'] for metric in rules[entry['id']].caps}
    eligible = {record['id'] for record in records if atoms[record['id']].metric in affected and number(record['state']) < 1}
    selected = set(eligible)
    references = lambda text: set(re.findall(r'(?<![A-Za-z0-9])([A-Z]+\d+)(?![A-Za-z0-9])', str(text))) & set(atoms)
    for entry in decisions:
        if entry['triggered']:
            rule = rules[entry['id']]
            selected.update(references(rule.trigger + ' ' + rule.effects + ' ' + entry['reason'] + ' ' + json.dumps(entry.get('evidence', []), ensure_ascii=False)))
    while True:
        expanded = selected | {ref for identifier in selected for ref in references(atoms[identifier].rule)}
        if expanded == selected:
            break
        selected = expanded
    return [record for record in records if record['id'] in selected], sorted(eligible)


def check_review(task, records, decisions, payload, known_files):
    rules = {rule.id: rule for rule in task.rubric.errors}
    original_errors = {entry["id"]: entry for entry in decisions}
    affected = {metric for entry in decisions if entry["triggered"] for metric in rules[entry["id"]].caps}
    indexed = {record["id"]: record for record in records}
    atoms = {atom.id: atom for atom in task.rubric.atoms}
    corrections = payload.get("atom_corrections", [])
    if not isinstance(corrections, list) or len({entry.get("id") for entry in corrections}) != len(corrections):
        raise ValueError("Critical review atom corrections must be unique")
    for raw in corrections:
        identifier = raw.get("id")
        if identifier not in indexed or atoms[identifier].metric not in affected or number(indexed[identifier]["state"]) >= 1:
            raise ValueError("Review correction leaves the affected negative findings")
        reviewed = validate_atom(raw, atoms[identifier], known_files)
        previous = indexed[identifier]
        if number(reviewed["state"]) < number(previous["state"]):
            raise ValueError("Invalid-counterevidence review cannot introduce an additional penalty")
        if atoms[identifier].kind == "CLAIM-RATIO" and (
                number(reviewed["denominator"]) != number(previous["denominator"]) or
                not shared_items_match(previous["items"], reviewed["items"])):
            raise ValueError("Critical review must preserve the locked factual claim sample")
        indexed[identifier] = reviewed
    errors = payload.get("errors")
    if not isinstance(errors, list) or {entry.get("id") for entry in errors} != set(rules) or len(errors) != len(rules):
        raise ValueError("Critical review must return every error decision")
    for entry in errors:
        if not isinstance(entry.get("triggered"), bool) or len(str(entry.get("reason", "")).strip()) < (20 if original_errors[entry.get("id", "")]["triggered"] else 1):
            raise ValueError("Critical review requires explicit decisions and concrete reasons")
        if entry["triggered"] and not original_errors[entry["id"]]["triggered"]:
            raise ValueError("Critical review cannot introduce a new severe error")
        if entry["triggered"] and not entry.get("evidence"):
            raise ValueError("A confirmed severe error requires locatable evidence")
        for evidence in entry.get("evidence", []):
            if not all(evidence.get(key) for key in ("file", "locator", "quote")):
                raise ValueError("Critical review evidence requires file, locator and quote")
            if evidence["file"] not in known_files and not str(evidence["file"]).startswith("https://"):
                raise ValueError("Critical review evidence leaves authorized materials")
    return [indexed[record["id"]] for record in records], errors


def review_critical(task, records, decisions, *, known_files, workspace, project, model, major, timeout=900, scoped_input=False):
    from .judge import write_json
    if not any(entry["triggered"] for entry in decisions):
        return records, decisions, None
    full_payload = {"task": task.id, "atoms": records, "errors": decisions,
               "rules": [dict(id=rule.id, trigger=rule.trigger, effects=rule.effects) for rule in task.rubric.errors]}
    payload = full_payload
    if scoped_input:
        selected, eligible = review_context(task, records, decisions)
        payload = {**full_payload, 'atoms': selected, 'review_scope': {'eligible_atom_ids': eligible,
                   'context_atom_ids': [record['id'] for record in selected],
                   'full_previous_judgments': 'critical-review-full-input.json'}}
        write_json(workspace / 'critical-review-full-input.json', full_payload)
    digest = hashlib.sha256(json.dumps({"input": full_payload, "version": 2 if scoped_input else 1}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    checkpoint = workspace / "checkpoints/critical-evidence-review.json"
    if checkpoint.exists():
        saved = json.loads(checkpoint.read_text(encoding="utf-8"))
        if saved.get("input_hash") == digest:
            updated, errors = check_review(task, records, decisions, saved["review"], known_files)
            return updated, errors, saved
    if isinstance(timeout, bool) or not isinstance(timeout, int) or timeout < 1:
        raise ValueError("critical_review_timeout_seconds must be a positive integer")
    input_path = workspace / "critical-review-input.json"
    # Bind legacy logs to their actual input before replacing the input file.
    if input_path.exists():
        previous = json.loads(input_path.read_text(encoding="utf-8"))
        previous_hash = hashlib.sha256(json.dumps({"input": previous, "version": 1}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        for old in (workspace / "events").glob("critical-evidence-review-?.jsonl"):
            archive = old.with_name(f"critical-evidence-review-{previous_hash[:12]}-{old.stem.rsplit('-', 1)[1]}.jsonl")
            if archive.exists():
                if archive.read_bytes() != old.read_bytes():
                    raise ValueError("Legacy critical-review archive differs; preserve both logs")
                old.unlink()
            else:
                old.rename(archive)
    write_json(input_path, payload)
    prompt = """Independently audit the precise evidential/logical validity of the TRIGGERED severe errors in critical-review-input.json for THIS task and participant. Read that formatted file and the relevant original/extracted materials listed in manifest.json; use webfetch only as needed. Do not read any other participant. Treat the earlier decisions as hypotheses, not factual sources. A refutation must match the exact subject, predicate, event and cutoff: an earlier partnership does not prove no later suspension; 'not used for training' does not refute another data-use allegation; a later price/valuation does not disprove an earlier value; absence from one newsroom/index or an inaccessible link alone does not establish fabrication. Historical reuse itself is not an error. Unknown evidence must not be converted into proven falsehood or severe fabrication. Apply the rubric's actual truth/empty-set/sampling requirements and reach supported decisions automatically.
Return every error code in errors [{id,triggered,reason,evidence:[{file,locator,quote}]}], preserving originally untriggered codes as false. Confirm a severe error only with specific evidence actually implying its trigger; otherwise revoke it and explain the exact logical insufficiency. If a negative atom in a metric affected by these ceilings depends on the SAME logically invalid counterevidence, include its complete corrected record in atom_corrections, with id,state,observation,reason,evidence and ratio fields/items where applicable. Keep the locked factual claim sample, ordering and denominator for CLAIM-RATIO; explain each changed item's evidence. Change only these unsupported negative findings, retain unrelated findings, and never add a new penalty. Do not infer satisfaction solely from uncertainty: follow the exact rubric and explain the best supported decision. Keep corrections empty when no atom decision needs correction. Evidence must cite original/extracted materials or actual HTTPS sources; not review inputs/checkpoints as factual sources. Write explanations in Chinese. Return BEGIN_JUDGMENT then JSON {"errors":[...],"atom_corrections":[...]} then END_JUDGMENT."""
    if scoped_input:
        prompt += '\nThe input retains all eligible negative atoms, their linked claim samples, and atoms explicitly named by the triggered clauses/decisions. Other previous judgments remain in critical-review-full-input.json if genuinely needed as context. They are hypotheses, never factual sources. Do not redo unrelated scoring. Return all error decisions and inspect the relevant primary evidence independently.'
    failure = ""
    matching_events = sorted((workspace / "events").glob(f"critical-evidence-review-{digest[:12]}-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    session_id = next((event_session(path) for path in matching_events if event_session(path)), None) if major == 1 else None
    for old in matching_events:
        try:
            previous_answer = parse_events(old.read_text(encoding="utf-8"))[0]
            if "END_JUDGMENT" not in previous_answer:
                continue  # A repaired fragment could omit still-pending corrections.
            review = extract_json(previous_answer)
            updated, errors = check_review(task, records, decisions, review, known_files)
            saved = {"input_hash": digest, "review": review, "original_errors": decisions,
                     "original_atoms": [record for record in records if record["id"] in {entry["id"] for entry in review.get("atom_corrections", [])}]}
            write_json(checkpoint, saved)
            return updated, errors, saved
        except (ValueError, KeyError, TypeError, JudgeError):
            continue
    for attempt in range(2):
        event_index = len(matching_events) + attempt
        event = workspace / f"events/critical-evidence-review-{digest[:12]}-{event_index}.jsonl"
        while event.exists():
            event_index += 1
            event = workspace / f"events/critical-evidence-review-{digest[:12]}-{event_index}.jsonl"
        continuation = "\nContinue the interrupted audit in this same session. Reuse the evidence already inspected and return the complete required JSON; do not repeat completed research without a specific need." if session_id else ""
        answer, _ = run(workspace, prompt + continuation + ("\nCorrect this validation issue: " + failure if failure else ""),
                        f"audit critical evidence {task.id}", event, project, model, major=major, timeout=timeout, session_id=session_id)
        session_id = (event_session(event) or session_id) if major == 1 else None
        try:
            review = extract_json(answer)
            updated, errors = check_review(task, records, decisions, review, known_files)
            saved = {"input_hash": digest, "review": review, "original_errors": decisions,
                     "original_atoms": [record for record in records if record["id"] in {entry["id"] for entry in review.get("atom_corrections", [])}]}
            write_json(checkpoint, saved)
            return updated, errors, saved
        except (ValueError, KeyError, TypeError, JudgeError) as exc:
            failure = str(exc)
    raise JudgeError("Critical error evidence review did not validate: " + failure)
