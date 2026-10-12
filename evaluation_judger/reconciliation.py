"""Independent bounded resolution of evidence/critical-review disagreement."""
from pathlib import Path
import hashlib
import json
from .opencode import JudgeError, extract_json, run
from .contract_review import validate_review
from .scoring import calculate, number, result_is_consistent, validate_error_decisions

PROMPT = '''Resolve a disagreement between previous evidence and critical-error reviews for ONE participant.
Read review-reconciliation-input.json, manifest.json, material-index.json and relevant original/extracted files.
Previous judgments are hypotheses, never factual proof. Use only the FIXED rubric; do not change rules,
weights, sampling scope or cap boundaries. Determine the exact subject, period, predicate and statistical
basis for each contested finding. Different periods, projections versus realized values, ranges, or
non-equivalent numerator/denominator definitions cannot be silently substituted. Show actual source
passages that establish or exclude a contradiction. Distinguish unsupported counterevidence from an
independently supported participant defect. Respect original reuse cutoff; reuse alone is not a defect.
Preserve valid unrelated findings. Do not choose a score just to make previous reviewers agree.
Return a final evidence-review object with checks (file_coverage, item_accounting, ownership,
internal_consistency), defects ledger (id, description, evidence, owners), complete atom corrections
(id, state, observation, reason, evidence and operands/items where applicable), relation_findings when
an inventory exists, ALL errors [{id,triggered,reason,evidence}], and disagreements
[{atom_id,resolution}] explicitly resolving every contested atom. Array fields may be empty when valid.
Every scored defect needs a concrete owner/consequence under the rubric; same defect has at most one
owner per metric. A required false relation witness must be confirmed_defect with defect_id, or excluded
with governing rule_id and concrete reason. Do not cite review input files/checkpoints as evidence.
Explain all operands and inspected items, including missing/empty actual values. A grouped population
must identify every member or an exact material_inspector scope/count. Correct ratio counts and their
states together. Retain required shared samples. A cap must satisfy its exact trigger with evidence;
never introduce a severe error not present in the supplied current/contested error decisions.
Return BEGIN_JUDGMENT then the complete JSON object then END_JUDGMENT, Chinese explanations.'''


def validate_resolution(payload, task, records, known_files, *, relation_inventory, contested_atom_ids, allowed_errors):
    if not isinstance(payload, dict):
        raise ValueError('Resolution must be an object')
    payload = dict(payload)
    if 'atom_corrections' in payload:
        if 'corrections' in payload and payload['corrections'] != payload['atom_corrections']:
            raise ValueError('Conflicting correction aliases')
        payload.setdefault('corrections', payload['atom_corrections'])
    corrections = payload.get('corrections')
    notes = list(payload.get('normalization_notes', []))
    if isinstance(corrections, list):
        original = {record['id']: record for record in records}
        normalized = []
        for record in corrections:
            if not isinstance(record, dict):
                normalized.append(record); continue
            record = dict(record)
            previous = original.get(record.get('id'))
            retained = str(record.get('change', '')).strip().lower()
            if previous and (retained.startswith(('维持', '保留', '不变', 'unchanged', 'retain')) or record.get('unchanged') is True):
                try:
                    same_state = abs(number(record.get('state')) - number(previous['state'])) <= number('0.000001')
                    same_operands = all(record.get(key) == previous.get(key) for key in ('numerator', 'denominator'))
                except (ValueError, TypeError):
                    same_state = same_operands = False
                if same_state and same_operands:
                    normalized.append(previous)
                    notes.append({'atom_id': record.get('id'), 'action': 'Preserved validated full prior record for an explicitly unchanged decision with matching state/operands; raw draft retained.'})
                    continue
            reason = str(record.get('reason', '')).strip()
            observation = str(record.get('observation', '')).strip()
            if reason and observation and len(reason) + len(observation) >= 28 and record.get('evidence'):
                if len(reason) < 20:
                    record['reason'] = reason + ' 判定依据：' + observation
                    notes.append({'atom_id': record.get('id'), 'action': 'Existing observation appended to short reason; no decision/evidence changed.'})
                if len(observation) < 8:
                    record['observation'] = observation + ' 具体观察：' + reason
                    notes.append({'atom_id': record.get('id'), 'action': 'Existing reason appended to short observation; no decision/evidence changed.'})
            normalized.append(record)
        payload['corrections'] = normalized
    payload['normalization_notes'] = notes
    final, review = validate_review(payload, task, records, known_files, relation_inventory=relation_inventory)
    decisions = validate_error_decisions(payload.get('errors'), task.rubric.errors, known_files)
    if any(x['triggered'] and x['id'] not in allowed_errors for x in decisions):
        raise ValueError('Resolution cannot introduce an unreviewed severe error')
    findings = payload.get('disagreements')
    if not isinstance(findings, list):
        raise ValueError('Resolution must explicitly explain contested atoms')
    known = {a.id for a in task.rubric.atoms} | {e.id for e in task.rubric.errors}; resolved=set()
    for finding in findings:
        identifier = next((finding.get(k) for k in ('atom_id','error_id','id') if finding.get(k)), None) if isinstance(finding, dict) else None
        if (not isinstance(finding, dict) or identifier not in known
                or identifier in resolved or len(str(finding.get('resolution','')).strip()) < 20):
            raise ValueError('Disagreement resolutions need distinct known atoms and concrete explanations')
        resolved.add(identifier)
    if not set(contested_atom_ids).issubset(resolved):
        raise ValueError('Unresolved contested atoms: '+', '.join(sorted(set(contested_atom_ids)-resolved)))
    if not result_is_consistent(task, {'atoms':final,'errors':decisions,'scores':calculate(task.rubric,final,decisions)}):
        raise ValueError('Resolution must preserve complete atoms and required shared samples')
    return final, decisions, review


def reconcile_reviews(task, records, decisions, *, contested_decisions, contract_review, critical_review,
                      relation_inventory, workspace:Path, project:Path, known_files, model, major, timeout=1800):
    from .judge import write_json
    contested = [r['id'] for r in (critical_review or {}).get('review',{}).get('atom_corrections',[])]
    allowed={e['id'] for e in [*decisions,*contested_decisions] if e['triggered']}
    packet={'rubric':task.rubric.text,'atoms':records,'errors':decisions,'contested_errors':contested_decisions,
            'evidence_review':contract_review,'critical_review':critical_review,'contested_atom_ids':contested,
            'relation_inventory':relation_inventory}
    digest=hashlib.sha256(json.dumps([1,PROMPT,packet],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    checkpoint=workspace/'checkpoints'/f'review-reconciliation-{digest[:12]}.json'
    def checked(payload):
        return validate_resolution(payload,task,records,known_files,relation_inventory=relation_inventory,
                                   contested_atom_ids=contested,allowed_errors=allowed)
    if checkpoint.exists():
        saved=json.loads(checkpoint.read_text(encoding='utf-8'))
        if saved.get('status')=='passed' and saved.get('input_hash')==digest:
            final,errors,review=checked(saved['review']);return final,errors,saved
    write_json(workspace/'review-reconciliation-input.json',packet)
    events=workspace/'events';events.mkdir(exist_ok=True)
    candidates=sorted(workspace.glob(f'review-reconciliation-candidate-{digest[:12]}-*.json'))
    offset=max((int(f.stem.rsplit('-',1)[1])+1 for f in candidates),default=0)
    previous='';candidate=candidates[-1] if candidates else None
    for previous_candidate in reversed(candidates):
        try:
            payload=json.loads(previous_candidate.read_text(encoding='utf-8'));final,errors,review=checked(payload)
            saved={'version':1,'status':'passed','input_hash':digest,'review':review,
                   'recovered_from_candidate':previous_candidate.name};write_json(checkpoint,saved)
            return final,errors,saved
        except (ValueError,TypeError,KeyError,JudgeError) as exc: previous=str(exc)
    for attempt in range(2):
        try:
            prompt=PROMPT+('\nValidation feedback: '+previous if previous else '')
            if candidate: prompt+=f'\nRead {candidate.name} as an unaccepted draft; repair against original evidence.'
            answer,read=run(workspace,prompt,f'reconcile reviews {task.id}',
                            events/f'reconciliation-{digest[:12]}-{offset+attempt}.jsonl',project,model,timeout,major)
            if not read: raise ValueError('Resolution must independently read input/material evidence')
            payload=extract_json(answer);candidate=workspace/f'review-reconciliation-candidate-{digest[:12]}-{offset+attempt}.json'
            write_json(candidate,payload);final,errors,review=checked(payload)
            saved={'version':1,'status':'passed','input_hash':digest,'review':review};write_json(checkpoint,saved)
            return final,errors,saved
        except (ValueError,TypeError,KeyError,JudgeError) as exc:
            previous=str(exc);write_json(workspace/'checkpoints'/f'reconciliation-attempt-{digest[:12]}-{offset+attempt}.json',
                                        {'status':'rejected','error':previous,'candidate':candidate.name if candidate else None})
    write_json(checkpoint,{'version':1,'status':'incomplete','input_hash':digest,'error':previous})
    raise JudgeError('Independent review resolution incomplete; candidates/checkpoints retained: '+previous)
