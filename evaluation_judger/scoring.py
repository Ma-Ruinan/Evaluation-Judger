"""Validate atom decisions and calculate scores independently of the model."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
import re
import unicodedata

from .rubric import METRICS, Rubric, shared_sample_references


def shared_items_match(source_items: list, target_items: list) -> bool:
    """Check that a rubric's shared claim sample is actually the same sample."""
    if len(source_items) != len(target_items):
        return False
    for source, target in zip(source_items, target_items):
        if not isinstance(source, dict) or not isinstance(target, dict):
            return False
        left = source.get("claim") or source.get("element") or source.get("statement")
        right = target.get("claim") or target.get("element") or target.get("statement")
        if not left or not right:
            return False
        left = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(left)).lower())
        right = re.sub(r"[\W_]+", "", unicodedata.normalize("NFKC", str(right)).lower())
        if SequenceMatcher(None, left, right).ratio() < .85:
            return False
    return True


def number(value) -> Decimal:
    try:
        if isinstance(value, str) and re.fullmatch(r"\s*\d+(?:\.\d+)?\s*/\s*\d+(?:\.\d+)?\s*", value):
            numerator, denominator = (Decimal(part.strip()) for part in value.split("/", 1))
            d = numerator / denominator
        else:
            d = Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"Invalid numeric value: {value!r}") from exc
    if not d.is_finite():
        raise ValueError("Non-finite value")
    return d


def display(value) -> str:
    return str(number(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def capped_count_target(rule: str) -> str | None:
    """Recognize only the two explicit saturation formulas, never generic min."""
    target = r"(?:\d+(?:\.\d+)?|[A-Za-z_]\w*)"
    divided = re.search(rf"\bmin\s*\([^\n]*?/\s*({target})\s*[,，]\s*1(?:\.0+)?\s*\)", rule, re.I)
    if divided:
        return divided.group(1)
    clipped = re.search(rf"\bmin\s*\([^\n]*[,，]\s*({target})\s*\)\s*/\s*({target})", rule, re.I)
    if clipped and clipped.group(1) == clipped.group(2):
        return clipped.group(1)
    return None


def counting_notes(items: list, denominator) -> list[str]:
    """Explain grouped/excluded records without treating them as a schema failure."""
    d = number(denominator)
    if d <= 0 or d > 100 or d != d.to_integral_value() or len(items) == int(d):
        return []
    def excluded(item):
        if not isinstance(item, dict):
            return False
        if item.get("included") is False or item.get("applicable") is False:
            return True
        label = str(item.get("result") or item.get("verdict") or "").strip().lower()
        return bool(re.match(r"^(?:excluded\b|not_applicable\b|not applicable\b|n/a\b|排除(?:[（(:：]|$)|剔除(?:[（(:：]|$)|不计入(?:[（(:：]|$))", label))
    if sum(not excluded(item) for item in items) == int(d):
        return []
    return [f"展示清单 {len(items)} 条，计分对象 {int(d)} 项；分组、排除等对应关系见本项计数依据和核验范围。"]


def validate_atom(record: dict, atom, known_files: set[str]) -> dict:
    record = dict(record)
    if record.get("id") != atom.id:
        raise ValueError(f"Expected atom {atom.id}, got {record.get('id')}")
    rationale = str(record.get("reason", "")).strip()
    observation = str(record.get("observation", "")).strip()
    if len(rationale) < 20 or len(observation) < 8:
        raise ValueError(f"{atom.id}: observation/reason is too vague")
    evidence = record.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        raise ValueError(f"{atom.id}: missing evidence list")
    for entry in evidence:
        if not isinstance(entry, dict) or not entry.get("file") or not entry.get("locator"):
            raise ValueError(f"{atom.id}: evidence needs file, locator, quote or explicit visual description")
        visual = (entry.get("kind") == "visual" and isinstance(entry.get("description"), str)
                  and len(entry["description"].strip()) >= 8
                  and str(entry["file"]).lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".html", ".htm", ".pptx", ".docx", ".pdf", ".xlsx")))
        if not entry.get("quote") and not visual:
            raise ValueError(f"{atom.id}: evidence needs a text quote or a located visual description")
        if entry["file"] not in known_files and not str(entry["file"]).startswith("https://"):
            raise ValueError(f"{atom.id}: unknown evidence file {entry['file']}")
    if atom.kind == "BIN":
        state = number(record.get("state"))
        if state not in (0, 1):
            raise ValueError(f"{atom.id}: BIN must be 0 or 1")
    else:
        numerator, denominator = record.get("numerator"), record.get("denominator")
        if numerator is None or denominator is None:
            raise ValueError(f"{atom.id}: ratio needs numerator and denominator")
        n, d = number(numerator), number(denominator)
        items = record.get("items")
        audited = re.findall(r"(?:实际核验|核验的)\s*(\d+)\s*条", observation + " " + rationale)
        positive = ("非虚构", "无问题", "satisfied", "supported", "consistent", "correct", "有效", "通过")
        if ("抽样" in atom.rule and n == d and d > 0 and number(record.get("state")) == 1
                and isinstance(items, list) and items and len(items) < d
                and str(len(items)) in audited
                and all(isinstance(item, dict) and str(item.get("result") or item.get("verdict") or "").lower().startswith(positive) for item in items)):
            # The model sometimes writes the source population count as the
            # denominator despite explicitly documenting a smaller audited sample.
            # Only reconcile the provable all-positive case; preserve the original.
            correction = {"type": "audited_sample_count", "original_numerator": str(n),
                          "original_denominator": str(d), "audited_count": len(items),
                          "reason": "文字明确实际核验数量，明细逐条为通过；将全集数量与计分样本数量分开，状态与分数不变。"}
            record["record_corrections"] = [*record.get("record_corrections", []), correction]
            record["numerator"] = record["denominator"] = len(items)
            n = d = Decimal(len(items))
        # Generated rubrics may explicitly cap counts at a fixed target:
        # min(n / target, 1) or min(n, target) / target. This is not a
        # success/total ratio; the observed count can legitimately exceed target.
        count_target = capped_count_target(atom.rule) if atom.kind != "CLAIM-RATIO" else None
        capped_count = count_target is not None
        if count_target and re.fullmatch(r"\d+(?:\.\d+)?", count_target) and d != number(count_target):
            raise ValueError(f"{atom.id}: count denominator must equal rubric target {count_target}")
        if d < 0 or n < 0 or (n > d and not capped_count):
            raise ValueError(f"{atom.id}: invalid ratio {n}/{d}")
        if d == 0:
            if "空集合状态=1" in atom.rule or "空集" in atom.rule and "状态=1" in atom.rule:
                state = Decimal(1)
            else:
                state = Decimal(0)
        else:
            state = min(n / d, Decimal(1)) if capped_count else n / d
        given = number(record.get("state"))
        if abs(state - given) > Decimal("0.005"):
            raise ValueError(f"{atom.id}: stated state {given} differs from computed {state} ({n}/{d}, capped={capped_count})")
        if not isinstance(record.get("items"), list) or (d > 0 and not record["items"]):
            raise ValueError(f"{atom.id}: ratio requires itemized checks")
        notes = []
        if not capped_count:
            notes = counting_notes(record["items"], d)
    if atom.kind == "BIN":
        notes = []
    return {
        "id": atom.id, "metric": atom.metric, "kind": atom.kind,
        "weight": str(atom.weight), "state": str(state),
        "observation": observation, "reason": rationale,
        "evidence": evidence, "numerator": record.get("numerator"),
        "denominator": record.get("denominator"), "items": record.get("items", []),
        "validation_notes": notes,
        "rule": atom.rule, "purpose": atom.purpose,
        "record_corrections": record.get("record_corrections", []),
    }


def calculate(rubric: Rubric, records: list[dict], errors: list[dict]) -> dict:
    indexed = {r["id"]: r for r in records}
    if set(indexed) != {a.id for a in rubric.atoms} or len(indexed) != len(records):
        raise ValueError("Atom records are incomplete or duplicated")
    scores = {}
    for metric in METRICS:
        raw = sum((a.weight * number(indexed[a.id]["state"]) for a in rubric.by_metric(metric)), Decimal(0))
        raw = raw if metric == "completion" else raw / Decimal(20)
        rules = [rule for e in errors if e.get("triggered") for rule in rubric.errors if rule.id == e["id"] and metric in rule.caps]
        caps = [number(rule.caps[metric]) for rule in rules if metric not in rule.atom_scopes]
        # Scoped zero caps remove only the explicitly named atoms. Preserve
        # other deliverables' contributions in the same metric.
        scoped = [rule for rule in rules if metric in rule.atom_scopes]
        contributions = {a.id: a.weight * number(indexed[a.id]["state"]) / (1 if metric == "completion" else Decimal(20)) for a in rubric.by_metric(metric)}
        if any(number(rule.caps[metric]) != 0 for rule in scoped):
            raise ValueError("Nonzero scoped cap requires an explicit atom-level score formula")
        for rule in scoped:
            for identifier in rule.atom_scopes[metric]:
                contributions[identifier] = Decimal(0)
        final = min([sum(contributions.values(), Decimal(0)), *caps]) if scoped else min([raw, *caps])
        scores[metric] = {"raw": str(raw), "final": str(final), "caps": [str(c) for c in caps]}
        if scoped:
            scores[metric]["scoped_caps"] = [{"error": rule.id, "ceiling": str(rule.caps[metric]), "atoms": list(rule.atom_scopes[metric])} for rule in scoped]
    scores["quality_mean"] = str(sum((number(scores[m]["final"]) for m in METRICS[1:]), Decimal(0)) / 5)
    return scores


def result_is_consistent(task, result: dict) -> bool:
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
