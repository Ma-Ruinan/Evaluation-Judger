"""Inventory numerical comparison assertions before reviewing their rubric impact."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
import hashlib
import html
import json
import re

from .opencode import JudgeError, extract_json, run

VERSION = 1

PROMPT = '''Read manifest.json and ALL original/extracted participant delivery files, including conclusions,
detail sections, tables and chart descriptions. Do not read prior atom decisions, scores or another participant.
Inventory asserted numerical comparisons (greater/less/higher/lower/more/less/above/below,
高于/低于/多于/少于/大于/小于/超过/不及/领先/落后). Bind locally unstated operands to exact declarations
of the SAME subject, measure, period and unit elsewhere in this delivery. Keep both contradictory directions,
not just repeated numerical tables. Separate conditional predictions, different scope and rounding.
This is not scoring. A quote proves that an assertion was made, not that its external facts are true.
Use only exact observed scalar values when available. Never strip >, < or a range to invent an operand.
Unknown/range/threshold operands belong in unresolved. Do not infer that a threshold equals the actual value.
Every operand needs its own literal source quote containing the stated number. Quote separate passages in
separate evidence entries, not a fabricated concatenation. Copy authorized paths exactly from manifest.json.
Return BEGIN_JUDGMENT JSON:
{"coverage":"Chinese inspected scope and limitations","comparisons":[{"id":"R001",
"subject":"specific entities/measure","scope":"period and unit",
"left":{"value":"numeric scalar","unit":"unit","meaning":"entity/measure",
"evidence":[{"file":"authorized original/extracted path","locator":"location","quote":"literal passage"}]},
"right":{"value":"numeric scalar","unit":"same unit","meaning":"entity/measure","evidence":[same schema]},
"operator":"gt|lt|ge|le|eq","assertion":{"file":"authorized path","locator":"location",
"quote":"literal comparison assertion"},"kind":"asserted|conditional","context":"binding/scope explanation"}],
"unresolved":[{"assertion":"comparison whose operands cannot be bound","reason":"specific limitation"}]}
END_JUDGMENT. Empty comparisons allowed when none are present; explain inspected scope.
'''


def _normalized(text):
    return re.sub(r'\s+', '', html.unescape(text))


def _scalar(value):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value).replace(',', '').strip())
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _quoted_numbers(text):
    # Commas in arrays separate values; only groups of three digits are thousands separators.
    return [Decimal(value.replace(',', '')) for value in re.findall(
        r'(?<![\d.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![\d.])', text)]


def _quoted(evidence, workspace, known_files, manifest):
    if not isinstance(evidence, dict) or evidence.get('file') not in known_files:
        raise ValueError('Comparison evidence needs an authorized original/extracted path.')
    if not str(evidence.get('locator', '')).strip() or not str(evidence.get('quote', '')).strip():
        raise ValueError('Comparison evidence needs concrete location and literal quote.')
    relative = evidence['file']
    paths = [workspace/relative]
    paths += [workspace/item['extracted'] for item in manifest['files']
              if item.get('file') == relative and item.get('extracted') != relative]
    source = []
    for path in paths:
        if not path.resolve().is_relative_to(workspace.resolve()):
            raise ValueError('Comparison evidence escapes its task-participant workspace.')
        if path.suffix.lower() not in {'.pdf', '.docx', '.xlsx', '.pptx', '.png', '.jpg', '.jpeg'}:
            source.append(_normalized(path.read_text(encoding='utf-8', errors='replace')))
    quote = _normalized(evidence['quote'])
    if not any(quote in text for text in source):
        raise ValueError(f"Quote not found in {relative}: provide one actual short passage per evidence entry.")
    return evidence


def validate_inventory(payload, workspace, known_files, manifest, *, tolerate_partial=False):
    if not isinstance(payload, dict) or not str(payload.get('coverage', '')).strip():
        raise ValueError('Explain inventory coverage and limitations.')
    if not isinstance(payload.get('comparisons'), list) or not isinstance(payload.get('unresolved'), list):
        raise ValueError('Inventory requires comparisons and unresolved arrays.')
    identifiers, checked = set(), []
    unresolved = list(payload['unresolved'])
    warnings = []
    for item in payload['comparisons']:
        if tolerate_partial:
            try:
                one = validate_inventory({'coverage': payload['coverage'], 'comparisons': [item], 'unresolved': []},
                                         workspace, known_files, manifest)
                if item['id'] in identifiers:
                    raise ValueError('Duplicate comparison identifier.')
                identifiers.add(item['id'])
                checked.extend(one['comparisons'])
                unresolved.extend(one['unresolved'])
            except (ValueError, TypeError, KeyError, OSError) as exc:
                warning = {'id': item.get('id') if isinstance(item, dict) else None,
                           'assertion': item.get('assertion') if isinstance(item, dict) else item,
                           'reason': str(exc), 'disposition': 'Unverified binding, no arithmetic verdict or automatic deduction.'}
                warnings.append(warning)
                unresolved.append(warning)
            continue
        if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not item['id'] or item['id'] in identifiers:
            raise ValueError('Comparison identifiers must be distinct.')
        identifiers.add(item['id'])
        if item.get('operator') not in {'gt', 'lt', 'ge', 'le', 'eq'} or item.get('kind') not in {'asserted', 'conditional'}:
            raise ValueError('Use a supported operator and distinguish asserted from conditional.')
        if not str(item.get('subject', '')).strip() or not str(item.get('scope', '')).strip():
            raise ValueError('Bind comparisons to a specific subject and scope.')
        _quoted(item.get('assertion'), workspace, known_files, manifest)
        numbers, binding_notes = [], []
        for side in ('left', 'right'):
            operand = item.get(side)
            if not isinstance(operand, dict) or not operand.get('unit') or not operand.get('meaning'):
                raise ValueError('Each operand needs meaning and unit.')
            if not isinstance(operand.get('evidence'), list) or not operand['evidence']:
                raise ValueError('Every operand needs source evidence.')
            for evidence in operand['evidence']:
                _quoted(evidence, workspace, known_files, manifest)
            number = _scalar(operand.get('value'))
            numbers.append(number)
            if number is not None:
                quoted_numbers = [match for evidence in operand['evidence'] for match in _quoted_numbers(evidence['quote'])]
                matches = [value for value in quoted_numbers if value == number or
                           (number.as_tuple().exponent > value.as_tuple().exponent and
                            value.quantize(Decimal(1).scaleb(number.as_tuple().exponent), rounding=ROUND_HALF_UP) == number)]
                if not matches:
                    raise ValueError(f"{item['id']}/{side}: scalar {number} must occur in its own source quote; quoted numbers are {quoted_numbers}. Do not invent derived values.")
                if number not in matches:
                    binding_notes.append(f'{side}: {number} is a rounded representation of quoted {matches[0]}; review precision at the comparison boundary.')
        if None in numbers or item['left']['unit'] != item['right']['unit']:
            unresolved.append({'id': item['id'], 'assertion': item['assertion'],
                               'reason': 'Range/threshold/non-scalar or units differ; no automatic arithmetic verdict.'})
            checked.append({**item, 'numeric_relation_holds': None})
            continue
        left, right = numbers
        truth = {'gt': left > right, 'lt': left < right, 'ge': left >= right,
                 'le': left <= right, 'eq': left == right}[item['operator']]
        checked.append({**item, 'numeric_relation_holds': truth, 'binding_notes': binding_notes})
    if payload['comparisons'] and not checked:
        raise ValueError('No comparison has valid evidence binding; repair candidates from original material.')
    return {'coverage': payload['coverage'], 'comparisons': checked, 'unresolved': unresolved,
            'validation_warnings': warnings,
            'note': 'Arithmetic witnesses only. Rubric decides scope, materiality, ownership and caps. Not automatic deductions.'}


def inventory_relations(task, *, workspace: Path, project: Path, known_files: set[str],
                        model: str, major: int, timeout: int = 1800):
    from .judge import write_json
    manifest = json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))
    bindings = [(item['file'], hashlib.sha256((workspace/item['file']).read_bytes()).hexdigest(),
                 hashlib.sha256((workspace/item['extracted']).read_bytes()).hexdigest())
                for item in manifest['files'] if item['category'] == 'deliveries']
    digest = hashlib.sha256(json.dumps([VERSION, PROMPT, bindings], ensure_ascii=False).encode()).hexdigest()
    checkpoint = workspace/'checkpoints'/f'relation-inventory-{digest[:12]}.json'
    previous = ''
    if checkpoint.exists():
        try:
            cached = json.loads(checkpoint.read_text(encoding='utf-8'))
            if cached.get('input_hash') == digest and cached.get('status') == 'passed':
                checked = validate_inventory(cached['candidate'], workspace, known_files, manifest, tolerate_partial=True)
                refreshed = {**cached, **checked}
                write_json(checkpoint, refreshed)
                write_json(workspace/'relation-inventory.json', refreshed)
                return refreshed
            previous = str(cached.get('error', ''))
        except (ValueError, TypeError, KeyError, OSError) as exc:
            previous = str(exc)
    events = workspace/'events'
    events.mkdir(exist_ok=True)
    candidates = sorted(workspace.glob(f'relation-candidate-{digest[:12]}-*.json'))
    offset = max((int(p.stem.rsplit('-', 1)[1])+1 for p in candidates), default=0)
    candidate_path = max(candidates, key=lambda p: p.stat().st_mtime_ns) if candidates else None
    if candidate_path:
        try:
            candidate = json.loads(candidate_path.read_text(encoding='utf-8'))
            checked = validate_inventory(candidate, workspace, known_files, manifest, tolerate_partial=True)
            result = {'version': VERSION, 'status': 'passed', 'input_hash': digest,
                      'candidate': candidate, 'recovered_from_candidate': candidate_path.name, **checked}
            write_json(checkpoint, result)
            write_json(workspace/'relation-inventory.json', result)
            return result
        except (ValueError, TypeError, KeyError, OSError) as exc:
            previous = str(exc)
    for attempt in range(2):
        try:
            prompt = PROMPT + ('\nValidation feedback: '+previous if previous else '')
            if candidate_path:
                prompt += f'\nRead {candidate_path.name} as a rejected candidate; repair evidence binding from original files, not by assuming prior output is true.'
            response, read = run(workspace, prompt, f'relation inventory {task.id}',
                events/f'relation-{digest[:12]}-{offset+attempt}.jsonl', project, model, timeout, major)
            if not read:
                raise ValueError('Relation inventory must read delivery evidence.')
            candidate = extract_json(response)
            candidate_path = workspace/f'relation-candidate-{digest[:12]}-{offset+attempt}.json'
            write_json(candidate_path, candidate)
            checked = validate_inventory(candidate, workspace, known_files, manifest, tolerate_partial=True)
            result = {'version': VERSION, 'status': 'passed', 'input_hash': digest,
                      'candidate': candidate, **checked}
            write_json(checkpoint, result)
            write_json(workspace/'relation-inventory.json', result)
            return result
        except (JudgeError, ValueError, TypeError, KeyError, OSError) as exc:
            previous = str(exc)
            write_json(workspace/'checkpoints'/f'relation-attempt-{digest[:12]}-{offset+attempt}.json',
                       {'status': 'rejected', 'error': previous, 'candidate': candidate_path.name if candidate_path else None})
    write_json(checkpoint, {'version': VERSION, 'status': 'incomplete', 'input_hash': digest, 'error': previous})
    raise JudgeError('Relation inventory incomplete; saved candidates/checkpoints permit resume: '+previous)
