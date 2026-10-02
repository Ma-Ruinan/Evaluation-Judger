"""Resumable item-level review when one atom shares several source samples."""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from decimal import Decimal
from pathlib import Path

from .opencode import JudgeError, extract_json, run
from .scoring import validate_atom


def _save(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _label(item) -> str:
    if not isinstance(item, dict):
        return str(item)
    return str(item.get("claim") or item.get("element") or item.get("statement") or item.get("name") or item.get("id") or "")


def _pool(source_records: dict) -> list[dict]:
    seen, pool = set(), []
    for ref, record in source_records.items():
        for index, item in enumerate(record["items"], 1):
            claim = _label(item).strip()
            if not claim:
                continue
            normalized = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", claim).lower())
            if normalized in seen:
                continue
            seen.add(normalized)
            pool.append({"id": f"{ref}-{index:03d}", "source_atom": ref, "claim": claim,
                         "source_location": (item.get("location") or item.get("locator") or item.get("supporting_location") or item.get("scope") or "") if isinstance(item, dict) else ""})
            if isinstance(item, dict):
                pool[-1]["source_result"] = str(item.get("result") or item.get("verdict") or "")
                pool[-1]["source_reason"] = str(item.get("note") or item.get("reason") or "")
    return pool


def _verdict(item: dict) -> bool | None:
    value = item.get("supported")
    if isinstance(value, bool):
        return value
    word = str(item.get("verdict", item.get("result", value if value is not None else ""))).strip().lower()
    if word.startswith(("supported", "satisfied", "yes", "true", "有支撑", "满足")):
        return True
    if word.startswith(("unsupported", "not satisfied", "no", "false", "无支撑", "不满足")):
        return False
    return None


def _source_positive(entry: dict) -> bool:
    """A source atom's verified positive fact is necessarily supported in H01."""
    word = str(entry.get("source_result", "")).strip().lower()
    return word.startswith(("supported", "consistent", "correct", "satisfied", "有支撑", "一致", "正确", "满足"))


def review_multi_atom(atom, source_records: dict, target: dict, *, task_id: str, subject: str,
                      workspace: Path, fingerprint: str, known_files: set[str], project: Path,
                      model: str, major: int) -> dict:
    pool = _pool(source_records)
    if not pool:
        return target
    key = _hash({"fingerprint": fingerprint, "target": target, "sources": source_records, "review_version": 3})
    root = workspace / "checkpoints" / f"multi-{atom.id}"
    final = root / "final.json"
    if final.exists():
        cached = json.loads(final.read_text(encoding="utf-8"))
        if cached.get("input_hash") == key:
            return validate_atom(cached["atom"], atom, known_files)
    selection_file = root / "selection.json"
    selected_ids, selection_reason = [entry["id"] for entry in pool], "合并所引原子的已核验主张，重复主张只计一次。"
    if selection_file.exists():
        cached = json.loads(selection_file.read_text(encoding="utf-8"))
        valid_ids = {entry["id"] for entry in pool}
        if (cached.get("input_hash") == key or cached.get("selected_ids") and all(item in valid_ids for item in cached["selected_ids"])):
            selected_ids, selection_reason = cached["selected_ids"], cached["reason"]
    else:
        prompt = f"""Select the exact review sample for atom {atom.id} in task {task_id}. The rubric says its sample references {', '.join(source_records)}. Read rubric.md if needed. Here are the already selected source claims with stable IDs: {json.dumps(pool, ensure_ascii=False)}. Apply the atom's exact sampling and deduplication rule; do not judge support yet. Do not silently reduce the sample to the first source atom. If no further sampling is stated, use the union with duplicates counted once. Rule: {atom.rule}. Return BEGIN_JUDGMENT then JSON {{"selected_ids":["A01-001",...],"reason":"specific sampling explanation"}} then END_JUDGMENT."""
        try:
            answer, _ = run(workspace, prompt, f"select shared sample {task_id} {subject} {atom.id}", workspace / "events" / f"multi-select-{atom.id}.jsonl", project, model, major=major)
            data = extract_json(answer)
            candidate = data.get("selected_ids")
            valid_ids = {entry["id"] for entry in pool}
            if not isinstance(candidate, list) or not candidate or len(candidate) != len(set(candidate)) or any(item not in valid_ids for item in candidate):
                raise ValueError("Invalid selected claim IDs")
            selected_ids, selection_reason = candidate, str(data.get("reason") or selection_reason)
        except (JudgeError, ValueError, KeyError, TypeError):
            pass  # The full source-sample union remains a documented, reviewable fallback.
        _save(selection_file, {"input_hash": key, "selected_ids": selected_ids, "reason": selection_reason})
    by_id = {entry["id"]: entry for entry in pool}
    selected = [by_id[item] for item in selected_ids]
    findings = [{"sample_id": entry["id"], "claim": entry["claim"], "source_atom": entry["source_atom"],
                 "result": "supported", "location": entry["source_location"],
                 "reason": f"{entry['source_atom']} 已将该事实核验为正确且有来源支持。{entry.get('source_reason', '')}".strip(), "quote": ""}
                for entry in selected if _source_positive(entry)]
    previous = {}
    for old in root.glob("items-*.json"):
        try:
            for item in json.loads(old.read_text(encoding="utf-8"))["items"]:
                entry = by_id.get(item.get("sample_id"))
                if entry and item.get("claim") == entry["claim"] and item.get("result") in ("supported", "unsupported") and item.get("reason"):
                    previous[item["sample_id"]] = item
        except (OSError, ValueError, KeyError, TypeError):
            continue
    findings.extend(previous[entry["id"]] for entry in selected if not _source_positive(entry) and entry["id"] in previous)
    to_review = [entry for entry in selected if not _source_positive(entry) and entry["id"] not in previous]
    for start in range(0, len(to_review), 8):
        chunk = to_review[start:start + 8]
        expected = {entry["id"] for entry in chunk}
        checkpoint = root / f"v3-items-{start:03d}.json"
        if checkpoint.exists():
            cached = json.loads(checkpoint.read_text(encoding="utf-8"))
            if cached.get("input_hash") == key and {item["sample_id"] for item in cached["items"]} == expected:
                findings.extend(cached["items"])
                continue
        reviewed = None
        # Salvage old responses, then allow fresh calls on every resumed run.
        # Exhausted invalid logs must never permanently block this chunk.
        event_prefix = f"multi-{atom.id}-v3-items-{start:03d}-attempt-"
        old_events = sorted((workspace / "events").glob(event_prefix + "*.jsonl"))
        next_attempt = max((int(path.stem.rsplit("-", 1)[1]) for path in old_events), default=-1) + 1
        events = old_events + [workspace / "events" / f"{event_prefix}{attempt}.jsonl" for attempt in range(next_attempt, next_attempt + 2)]
        for event in events:
            prompt = f"""Judge support for ONLY these {len(chunk)} selected claims of atom {atom.id}, task {task_id}, participant {subject}. Read the participant delivery and relevant common sources. Apply rule: {atom.rule}. Claims: {json.dumps(chunk, ensure_ascii=False)}. Return exactly one item per supplied ID, in the same order. Each item needs sample_id, supported (true/false), concrete reason, and delivery/source location; cite a short literal quote when available. An inaccessible page alone is not proof of fabrication. Do not cite checkpoints or previous scores as factual evidence. Return BEGIN_JUDGMENT then JSON {{"items":[...]}} then END_JUDGMENT."""
            try:
                if event.exists():
                    from .opencode import parse_events
                    answer = parse_events(event.read_text(encoding="utf-8"))[0]
                else:
                    answer, _ = run(workspace, prompt, f"review shared claims {task_id} {subject} {atom.id} {start + 1}", event, project, model, major=major)
                data = extract_json(answer)
                raw_items = data.get("items") or data.get("checks") or data.get("atom", {}).get("items")
                if not isinstance(raw_items, list) or len(raw_items) != len(chunk) or {item.get("sample_id") for item in raw_items if isinstance(item, dict)} != expected:
                    raise ValueError("Incomplete shared-sample item list")
                reviewed = []
                for entry in chunk:
                    item = next(item for item in raw_items if item["sample_id"] == entry["id"])
                    verdict = _verdict(item)
                    reason = str(item.get("reason") or item.get("note") or "").strip()
                    if verdict is None or len(reason) < 8:
                        raise ValueError("Shared-sample item lacks verdict or explanation")
                    reviewed.append({"sample_id": entry["id"], "claim": entry["claim"],
                                     "source_atom": entry["source_atom"], "result": "supported" if verdict else "unsupported",
                                     "location": str(item.get("location") or entry["source_location"]),
                                     "reason": reason, "quote": str(item.get("quote") or "")})
                break
            except (JudgeError, ValueError, KeyError, TypeError):
                reviewed = None
        if reviewed is None:
            raise JudgeError(f"{task_id}/{subject} {atom.id}: shared-sample items {start + 1}-{start + len(chunk)} could not be verified; completed chunks remain saved")
        _save(checkpoint, {"input_hash": key, "items": reviewed})
        findings.extend(reviewed)
    order = {item["id"]: index for index, item in enumerate(selected)}
    findings.sort(key=lambda item: order[item["sample_id"]])
    numerator = sum(item["result"] == "supported" for item in findings)
    denominator = len(findings)
    raw = {"id": atom.id, "state": str(Decimal(numerator) / Decimal(denominator)),
           "numerator": numerator, "denominator": denominator, "items": findings,
           "observation": f"按 {', '.join(source_records)} 的锁定核验清单复审，共 {denominator} 条去重后的被审主张；{numerator} 条有可识别支撑。",
           "reason": f"{selection_reason} 对每条被审主张按 {atom.id} 规则判断是否有来源、报告内推导或显式判断支撑；逐项位置和理由见 items。结果为 {numerator}/{denominator}。",
           "evidence": target["evidence"]}
    reviewed_atom = validate_atom(raw, atom, known_files)
    _save(final, {"input_hash": key, "atom": reviewed_atom})
    return reviewed_atom
