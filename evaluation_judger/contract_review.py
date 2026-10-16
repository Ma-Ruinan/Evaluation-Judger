"""Bounded evidence/count/defect-ownership review, without changing rubric rules."""
from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from pathlib import Path

from .opencode import JudgeError, extract_json, run, parse_events, event_session
from .scoring import calculate, result_is_consistent, validate_atom, normalize_evidence_files, evidence_has_content, capped_count_target


def count_issues(record: dict) -> list[str]:
    """Check explicit accounting only. Do not infer truth from labels or templates."""
    if record.get('kind') not in {'RATIO', 'CLAIM-RATIO'} or not record.get('denominator'):
        return []
    if record.get('kind') != 'CLAIM-RATIO' and capped_count_target(record.get('rule', '')):
        return []  # Denominator is a target, not a population of inspected items.
    items = record.get('items', [])
    # Existing one-item-per-element outputs remain accepted. Their semantic
    # findings are independently checked by the reviewer, not a regex.
    if len(items) == int(record['denominator']):
        return []
    total = 0
    for item in items:
        if not isinstance(item, dict):
            return ['Grouped accounting needs identifiable item objects with locations.']
        if isinstance(item.get('members'), list) and item['members']:
            total += len(item['members'])
        elif item.get('method') == 'material_inspector' and item.get('scope') and item.get('location'):
            try:
                value = int(item['count'])
            except (KeyError, TypeError, ValueError):
                return ['Exact aggregate must identify a numeric count, method and scope.']
            if value < 1:
                return ['Aggregate count must be positive.']
            total += value
        else:
            total += 1
    return [] if total == int(record['denominator']) else [
        f"{record['id']}: denominator {record['denominator']} has {total} explicitly accounted elements; "
        'expand grouped entries into members, or supply an exact inspector scope/count. '
        'For an actually verified aggregate, method must be exactly "material_inspector"; put the operation '
        'description in a separate field, and supply scope, location and count. Count disjoint groups once; '
        'keep overlapping examples in observation/evidence, not as additional counted items. Uniform templates are not evidence.'
    ]


def _merge_review_patch(payload, draft):
    """An explicit patch retains the same-input candidate, never bypassing validation."""
    if not isinstance(payload, dict) or 'review_patch' not in payload:
        return payload  # Existing complete-review responses keep their meaning.
    patch = payload['review_patch']
    if not isinstance(draft, dict) or not isinstance(patch, dict):
        raise ValueError('review_patch needs a saved candidate and an object of changed fields.')
    if set(patch) - {'checks', 'defects', 'corrections', 'relation_findings'}:
        raise ValueError('review_patch contains unknown fields; use the existing review fields.')
    merged = {**draft, **patch}
    if 'checks' in patch:
        if not isinstance(draft.get('checks', {}), dict) or not isinstance(patch['checks'], dict):
            raise ValueError('Patched checks must be an object.')
        merged['checks'] = {**draft.get('checks', {}), **patch['checks']}
    previous, updates = draft.get('corrections', []), patch.get('corrections', [])
    for entries in (previous, updates):
        if (not isinstance(entries, list)
                or any(not isinstance(a, dict) or not isinstance(a.get('id'), str) for a in entries)
                or len({a['id'] for a in entries}) != len(entries)):
            raise ValueError('Patch corrections need distinct atom IDs and complete atom records.')
    by_id = {a['id']: a for a in previous}
    by_id.update({a['id']: a for a in updates})
    merged['corrections'] = list(by_id.values())
    return merged


def validate_review(payload, task, records, known_files, *, relation_inventory=None):
    if not isinstance(payload, dict) or not isinstance(payload.get('defects'), list) or not isinstance(payload.get('corrections'), list):
        raise ValueError('Review needs defects and corrections arrays, which may be empty.')
    checks = payload.get('checks')
    if not isinstance(checks, dict) or any(not str(checks.get(k, '')).strip() for k in ('file_coverage','item_accounting','ownership','internal_consistency')):
        raise ValueError('Explain all four checks, including no-defect findings.')
    by_id = {a.id:a for a in task.rubric.atoms}
    replacement = {}
    atom_issues = []
    seen = set()
    for raw in payload['corrections']:
        if not isinstance(raw, dict) or not isinstance(raw.get('id'),str) or raw['id'] not in by_id or raw['id'] in seen:
            raise ValueError('Unknown or duplicate corrected atom.')
        seen.add(raw['id'])
        try:
            replacement[raw['id']] = validate_atom(raw, by_id[raw['id']], known_files)
        except ValueError as exc:
            detail = str(exc)
            if 'observation/reason is too vague' in detail:
                detail += (f" (observation has {len(str(raw.get('observation', '')).strip())} characters, minimum 8; "
                           f"reason has {len(str(raw.get('reason', '')).strip())} characters, minimum 20). "
                           'Explain the inspected finding and its rubric consequence; do not pad generic text.')
            atom_issues.append(detail)
    if atom_issues:
        raise ValueError('; '.join(atom_issues))
    final = [replacement.get(r['id'], r) for r in records]
    issues = [s for r in final for s in count_issues(r)]
    if issues:
        raise ValueError('; '.join(issues))
    if not result_is_consistent(task, {'atoms':final,'errors':[], 'scores':calculate(task.rubric, final, [])}):
        raise ValueError('Review must preserve rubric-required shared samples and all atom IDs.')
    defects = []
    identifiers = set()
    for defect in payload['defects']:
        if not isinstance(defect, dict) or not isinstance(defect.get('id'),str) or not defect['id'] or defect['id'] in identifiers:
            raise ValueError('Each defect needs a distinct ID.')
        identifiers.add(defect['id'])
        if not isinstance(defect.get('description'), str) or not defect['description'].strip():
            raise ValueError('Defect description must identify the specific conflicting/missing fact.')
        evidence = normalize_evidence_files(defect.get('evidence', []), known_files)
        if not evidence or any(not x.get('locator') or not evidence_has_content(x) for x in evidence):
            raise ValueError('Defect needs concrete original/extracted evidence.')
        owners = defect.get('owners', [])
        if not isinstance(owners, list) or not owners:
            raise ValueError('Each scored defect needs an owner.')
        metrics = set()
        for owner in owners:
            if not isinstance(owner,dict):
                raise ValueError('Owner must identify an atom, metric and consequence.')
            atom_id = owner.get('atom_id')
            if not isinstance(atom_id,str) or atom_id not in by_id or by_id[atom_id].metric != owner.get('metric'):
                raise ValueError('Defect owner must match a rubric atom and its metric.')
            if owner['metric'] in metrics:
                raise ValueError('A concrete defect may have only one primary owner per metric.')
            metrics.add(owner['metric'])
            if not str(owner.get('consequence','')).strip():
                raise ValueError('Explain the observable consequence for every metric owner.')
        defects.append({**defect, 'evidence': evidence})
    if relation_inventory is not None:
        required = {r['id']: r for r in relation_inventory['comparisons']
                    if r.get('numeric_relation_holds') is False and r.get('kind') == 'asserted'}
        known_relations = {r['id'] for r in relation_inventory['comparisons']}
        known_relations.update(r['id'] for r in relation_inventory.get('unresolved', [])
                               if isinstance(r, dict) and isinstance(r.get('id'), str))
        findings = payload.get('relation_findings')
        if not isinstance(findings, list):
            raise ValueError('Relation-enabled review needs relation_findings, possibly empty.')
        reviewed = {}
        final_by_id = {r['id']: r for r in final}
        defect_by_id = {d['id']: d for d in defects}
        for finding in findings:
            if not isinstance(finding, dict) or finding.get('id') not in known_relations or finding['id'] in reviewed:
                raise ValueError('Relation findings must identify known witnesses once; allowed IDs: '+', '.join(sorted(known_relations)))
            if not str(finding.get('reason', '')).strip():
                raise ValueError('Explain each relation witness disposition under the fixed rubric.')
            if finding.get('decision') == 'confirmed_defect':
                defect = defect_by_id.get(finding.get('defect_id'))
                if not defect or any(Decimal(str(final_by_id[owner['atom_id']]['state'])) >= 1 for owner in defect['owners']):
                    raise ValueError('Confirmed relation defect must link a scored ledger defect with non-full owner atom(s).')
            elif finding.get('decision') == 'excluded':
                if finding.get('rule_id') not in by_id:
                    raise ValueError('Excluded relation witness must cite the governing atom boundary, not ignore it.')
            else:
                raise ValueError('Relation witness must be confirmed_defect or excluded with a rubric explanation.')
            reviewed[finding['id']] = finding
        if not set(required).issubset(reviewed):
            raise ValueError('Review omitted relation witnesses: '+', '.join(sorted(set(required)-set(reviewed))))
    return final, {**payload, 'defects':defects}


def review_contract(task, records, *, workspace:Path, project:Path, known_files:set[str], model:str, major:int, timeout:int=1800, relation_inventory=None):
    from .judge import write_json
    packet = {'rubric': task.rubric.text, 'atoms': records}
    if relation_inventory is not None:
        packet['relation_inventory'] = relation_inventory
    digest = hashlib.sha256(json.dumps(packet, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    path = workspace/'checkpoints'/f'contract-review-{digest[:12]}.json'
    prior_failure = ''
    if path.exists():
        try:
            cached = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(cached,dict) and cached.get('input_hash') == digest and cached.get('status') == 'passed':
                final, review = validate_review(cached['review'], task, records, known_files, relation_inventory=relation_inventory)
                return final, cached
            if isinstance(cached,dict) and cached.get('input_hash') == digest:
                prior_failure = str(cached.get('error',''))
        except (ValueError, KeyError, TypeError):
            pass
    write_json(workspace/'contract-review-input.json', packet)
    diagnostics = [s for r in records for s in count_issues(r)]
    prompt = '''Independently review the SAME participant against the fixed rubric. Read contract-review-input.json,
manifest.json and material-index.json, then original/extracted evidence for relevant findings. Never read another participant.
The input is prior judgments, not factual proof. Do not invent new criteria, alter weights, soften a requirement or change a cap.
Evidence citations in defects and corrections must name authorized original/extracted materials or actual HTTP/HTTPS sources.
Never cite contract-review-input.json, other review inputs, checkpoints or prior scores as factual evidence.
The defects ledger describes participant defects, not mistakes made by the earlier grader; explain grading repairs in checks
and corrections, and retain a participant defect only when original material supports it.
Check complete relevant file coverage (HTML href/JSON metadata, PDF URI annotations), nonempty field VALUES,
exhaustive item accounting and semantic validity of every grouped claim. Inspect all members when the rubric requires it.
Keep rubric-required shared samples identical; corrections must repair related atoms together if their shared set changes.
Check numerical AND relation/direction contradictions, dates, entities and scope. Legitimate rounding or disclosed different
periods are not contradictions. Build a shared defect ledger from concrete evidence; merge only the SAME defect, not similar topics.
One primary owner per metric per defect, as required by the rubric. Cross-metric owners need distinct observable consequences.
An atom cannot say a defect does not exist when another identifies it: explain exclusion/ownership instead. Review positive
findings as well as deductions. Do not infer all entries pass from a few examples or equal schemas.
When an atom requires factual support, inspect the exact supporting passage in rubric anchors, original material or an opened
source, matching subject, predicate and scope. A source title, URL, the submission's own assertion, or lack of refutation alone
does not establish support. Historical fairness prevents penalties for later drift; it does not itself verify an old claim.
If evidence is limited, apply the fixed rubric's uncertainty rule and explain the limitation without inventing a defect.
For each counted element, provide its own result/location. Grouped entries require explicit members, or a verified exact
material_inspector aggregate with method, scope, location and count. Preserve every failed finding and map outcomes to operands.
Use this existing aggregate schema ONLY for an actually verified exact calculation:
{"claim":"the inspected group and outcome","method":"material_inspector","operation":"actual query/calculation",
"scope":"exact authorized document/paragraph or cell range and criterion","location":"original material locator",
"count":2,"result":"supported"}. The 2 is an illustrative count, not a task operand. The method value must be exactly
material_inspector; put query details in operation. A tool name, rubric population count or XML preview alone is not a verified
aggregate. Otherwise enumerate explicit members with supported outcomes/locations. Count disjoint groups only once. Do not
put an all-items aggregate AND overlapping examples in items: retain those examples in observation/evidence, including every
concrete failed finding. Keep numerator, denominator and outcomes supported by the inspected scope.
Each corrected atom remains a COMPLETE original-schema record, including observation (at least 8 characters), reason
(at least 20 characters), evidence, state and applicable ratio fields. Explain facts and the rubric consequence in Chinese.
If changes are necessary, return COMPLETE corrected atom records using the original grading schema. No manual metric scores.
Return BEGIN_JUDGMENT JSON with:
{"checks":{"file_coverage":"Chinese explanation of inspected scope and limitations",
"item_accounting":"Chinese explanation","ownership":"Chinese explanation","internal_consistency":"Chinese explanation"},
"defects":[{"id":"D001","description":"specific fact/scope, not generic label",
"evidence":[{"file":"authorized path","locator":"concrete location","quote":"literal text"}],
"owners":[{"metric":"metric key","atom_id":"rubric ID","consequence":"observable consequence"}],
"related_atoms":["other ID"],"disposition":"explain what is deducted and what is not"}],
"corrections":[complete atom records]} END_JUDGMENT. Empty defects/corrections allowed if actually supported.
URLs are locator evidence, not automatic proof of reliability. Unavailable webpages do not establish fabrication.
'''
    if relation_inventory is not None:
        prompt += '''\nThe input also contains an independently extracted relation_inventory. It is not a gold judgment.
Read original passages to verify subject/period/unit binding and materiality. Reconcile opposite direction statements across sections.
Inspect unresolved bindings where relevant; partial evidence does not prove correctness or fabrication. Numerical witnesses use
declared scalars and carry rounding notes; a failed numeric relation is a candidate, not an automatic atom deduction or cap.
For EVERY asserted comparison with numeric_relation_holds=false, add relation_findings:
[{"id":"inventory ID","decision":"confirmed_defect","defect_id":"scored ledger defect ID","reason":"specific rubric consequence"}]
or {"id":"inventory ID","decision":"excluded","rule_id":"governing rubric atom ID","reason":"concrete rounding/scope/materiality exclusion"}.
Confirmed defects must appear in defects and affect the appropriate owner atom; return complete corrected records if needed.
Return relation_findings:[] when no required witness exists. Do not silently omit any failed asserted relation.
Also repair grading omissions such as required-but-missing samples even if current state is unchanged; expand the required set
and recompute operands by the rule. Not inventing a participant defect does not justify leaving a known grading-method gap.
'''
    failure = '; '.join([x for x in [prior_failure,*diagnostics] if x])
    events = workspace/'events'
    events.mkdir(exist_ok=True)
    candidates = list(workspace.glob(f'contract-review-candidate-{digest[:12]}-*.json'))
    offset = max(len(list(events.glob(f'contract-{digest[:12]}-*.jsonl'))),
        max((int(p.stem.rsplit('-',1)[1])+1 for p in candidates),default=0))
    candidate = max(candidates,key=lambda p:p.stat().st_mtime_ns) if candidates else None
    matching_events = sorted(events.glob(f'contract-{digest[:12]}-*.jsonl'),
                             key=lambda entry: entry.stat().st_mtime_ns, reverse=True)
    session_id = next((event_session(entry) for entry in matching_events if event_session(entry)), None) if major == 1 else None
    prior_read = False
    replay_failure = ''
    for previous_event in matching_events:
        try:
            previous_answer, _, read = parse_events(previous_event.read_text(encoding='utf-8'))
            prior_read = prior_read or (read and event_session(previous_event) == session_id)
            if read and 'END_JUDGMENT' in previous_answer:
                final, review = validate_review(extract_json(previous_answer), task, records, known_files,
                                               relation_inventory=relation_inventory)
                saved = {'version':1,'input_hash':digest,'status':'passed','review':review,
                         'recovered_from_events':previous_event.name}
                write_json(path, saved)
                return final, saved
        except (OSError, JudgeError, ValueError, KeyError, TypeError) as exc:
            if not replay_failure:
                replay_failure = str(exc)
    # Historical scans seek a complete valid result, but must not replace the
    # newest saved diagnostic with an older rejected response's error.
    failure = prior_failure or replay_failure or failure
    for attempt in range(2):
        try:
            repair_context = (f'\nRead {candidate.name} as a rejected candidate, not factual proof. Preserve independently verified findings, repair the stated validation problem, and check relevant original evidence; return the complete review.' if candidate else '')
            if candidate:
                repair_context += ('\nFor a local repair, prefer {"review_patch":{"corrections":[]}}, putting ONLY changed complete atom records in that list. '
                    'The program merges this explicit patch into that saved candidate by atom ID, keeping other records. '
                    'You may also patch checks by key; defects and relation_findings, when supplied, replace their entire lists. '
                    'Reconcile those lists if the corrected findings affect them. To withdraw an earlier atom correction, '
                    'return its complete original decision. A complete review remains supported and replaces the candidate. '
                    'Unchanged records are not assumed true: inspect their evidence as needed. The merged review must pass '
                    'all original evidence, accounting, ownership, relation and shared-sample checks before completion.')
            continuation = ('\nContinue the interrupted review in THIS SAME INPUT session. Reuse already inspected evidence and return the requested complete review or explicit local patch; do not repeat completed research without a specific need.' if session_id else '')
            active_session = session_id
            answer, used_read = run(workspace, prompt + repair_context + continuation + ('\nSpecific validation feedback: '+failure if failure else ''),
                f'contract review {task.id}', events/f'contract-{digest[:12]}-{offset+attempt}.jsonl',
                project, model, timeout, major, session_id=session_id)
            session_id = (event_session(events/f'contract-{digest[:12]}-{offset+attempt}.jsonl') or session_id) if major == 1 else None
            if not used_read and not (active_session and prior_read):
                raise ValueError('Independent review did not read its input or material evidence; use the read tool.')
            payload = extract_json(answer)
            if isinstance(payload, dict) and 'review_patch' in payload:
                write_json(events/f'contract-{digest[:12]}-{offset+attempt}-patch.json', payload)
                draft = json.loads(candidate.read_text(encoding='utf-8')) if candidate else None
                payload = _merge_review_patch(payload, draft)
            candidate = workspace/f'contract-review-candidate-{digest[:12]}-{offset+attempt}.json'
            write_json(candidate, payload)
            final, review = validate_review(payload, task, records, known_files, relation_inventory=relation_inventory)
            saved = {'version':1,'input_hash':digest,'status':'passed','review':review}
            write_json(path, saved)
            return final, saved
        except (JudgeError, ValueError, KeyError, TypeError) as exc:
            failure = str(exc)
            partial_event = events/f'contract-{digest[:12]}-{offset+attempt}.jsonl'
            if partial_event.exists():
                previous_session = session_id
                session_id = (event_session(partial_event) or session_id) if major == 1 else None
                read = parse_events(partial_event.read_text(encoding='utf-8'))[2]
                prior_read = (prior_read or read) if previous_session == session_id else read
            write_json(workspace/'checkpoints'/f'contract-review-attempt-{digest[:12]}-{offset+attempt}.json',
                {'status':'rejected','input_hash':digest,'error':failure,'candidate':candidate.name if candidate else None})
    # Preserve valid grading and continue independent tasks; expose unresolved
    # review honestly and allow a later resume instead of pretending success.
    saved = {'version':1,'input_hash':digest,'status':'incomplete','error':failure,
             'original_records_preserved':True}
    write_json(path, saved)
    return records, saved
