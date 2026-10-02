"""Validate atom decisions and calculate scores independently of the model."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
import re
import unicodedata

from .rubric import METRICS, Rubric


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
        if not isinstance(entry, dict) or not entry.get("file") or not entry.get("locator") or not entry.get("quote"):
            raise ValueError(f"{atom.id}: evidence needs file, locator, quote")
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
        if d < 0 or n < 0 or n > d:
            raise ValueError(f"{atom.id}: invalid ratio {n}/{d}")
        if d == 0:
            if "空集合状态=1" in atom.rule or "空集" in atom.rule and "状态=1" in atom.rule:
                state = Decimal(1)
            else:
                state = Decimal(0)
        else:
            state = n / d
        given = number(record.get("state"))
        if abs(state - given) > Decimal("0.005"):
            raise ValueError(f"{atom.id}: stated state {given} differs from {n}/{d}")
        if not isinstance(record.get("items"), list) or (d > 0 and not record["items"]):
            raise ValueError(f"{atom.id}: ratio requires itemized checks")
        notes = []
        if d == d.to_integral_value() and d <= 100 and len(record["items"]) != int(d):
            notes.append(f"逐项清单共 {len(record['items'])} 条，分母为 {int(d)}；存在合并记录，建议复核明细覆盖范围")
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
        caps = [number(cap) for e in errors if e.get("triggered") for cap in [next((rule.caps[metric] for rule in rubric.errors if rule.id == e["id"] and metric in rule.caps), None)] if cap is not None]
        scores[metric] = {"raw": str(raw), "final": str(min([raw, *caps])), "caps": [str(c) for c in caps]}
    scores["quality_mean"] = str(sum((number(scores[m]["final"]) for m in METRICS[1:]), Decimal(0)) / 5)
    return scores
