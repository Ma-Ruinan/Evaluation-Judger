from decimal import Decimal
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory
from pathlib import Path
import json

from evaluation_judger.critical_review import check_review, review_critical, review_context


class CriticalEvidenceReview(TestCase):
    def test_scoped_context_keeps_samples_and_clause_references(self):
        task, records, errors = self.fixture()
        task.rubric.atoms[0].rule = '与A01采用同一批主张核验'
        task.rubric.atoms += [SimpleNamespace(id='A01', metric='accuracy', rule='按原主张核验'), SimpleNamespace(id='T01', metric='completion', rule='完成交付'), SimpleNamespace(id='C01', metric='coverage', rule='内容覆盖')]
        task.rubric.errors[0].trigger = 'T01相关证据'; task.rubric.errors[0].effects = '幻觉上限'
        full = records + [{'id': name, 'state': 1} for name in ['A01', 'T01', 'C01']]
        selected, eligible = review_context(task, full, errors)
        self.assertEqual(eligible, ['H01'])
        self.assertEqual([item['id'] for item in selected], ['H01', 'A01', 'T01'])
        self.assertIs(selected[0], records[0])
        self.assertEqual(len(full), 4)

    def test_scoped_review_archives_full_input_and_preserves_other_records(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'events').mkdir()
            task, records, errors = self.fixture(); task.id = 'demo'
            task.rubric.errors[0].trigger = '具体虚构'; task.rubric.errors[0].effects = '幻觉封顶'
            task.rubric.atoms.append(SimpleNamespace(id='T01', metric='completion', rule='完成交付'))
            records.append({'id': 'T01', 'state': 1})
            response = {'errors': [{'id': 'E01', 'triggered': False, 'reason': '先前反证没有针对同一个时间和命题，无法支持这一重大错误判定。'}], 'atom_corrections': []}
            def reply(*args, **kwargs):
                limited = json.loads((root / 'critical-review-input.json').read_text())
                full = json.loads((root / 'critical-review-full-input.json').read_text())
                self.assertEqual([r['id'] for r in limited['atoms']], ['H01'])
                self.assertEqual(len(full['atoms']), 2)
                return json.dumps(response), True
            with patch('evaluation_judger.critical_review.run', side_effect=reply):
                result = review_critical(task, records, errors, known_files={'delivery.md'}, workspace=root, project=root, model='model', major=1, scoped_input=True)
            self.assertEqual(result[0], records)
            self.assertFalse(result[1][0]['triggered'])

    def test_complete_logged_review_is_salvaged_without_another_model_call(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'events').mkdir()
            task, records, errors = self.fixture(); task.id = 'demo'
            rule = task.rubric.errors[0]; rule.trigger = '具体虚构'; rule.effects = '质量封顶'
            payload = {'task': task.id, 'atoms': records, 'errors': errors,
                       'rules': [{'id': rule.id, 'trigger': rule.trigger, 'effects': rule.effects}]}
            (root / 'critical-review-input.json').write_text(json.dumps(payload))
            response = {'errors': [{'id': 'E01', 'triggered': False, 'reason': '先前反证没有针对同一个时间和命题，无法支持这一重大错误判定。'}], 'atom_corrections': []}
            (root / 'events/critical-evidence-review-0.jsonl').write_text(json.dumps({'sessionID': 'saved', 'type': 'text', 'part': {'text': 'BEGIN_JUDGMENT '+json.dumps(response)+' END_JUDGMENT'}}))
            with patch('evaluation_judger.critical_review.run') as model:
                result = review_critical(task, records, errors, known_files={'delivery.md'}, workspace=root, project=root, model='model', major=1)
            model.assert_not_called()
            self.assertFalse(result[1][0]['triggered'])

    def test_interrupted_review_resumes_only_matching_inputs(self):
        for changed in (False, True):
            with self.subTest(changed=changed), TemporaryDirectory() as folder:
                root = Path(folder); (root / 'events').mkdir()
                task, records, errors = self.fixture(); task.id = 'demo'
                rule = task.rubric.errors[0]; rule.trigger = '具体虚构'; rule.effects = '质量封顶'
                payload = {'task': task.id, 'atoms': records, 'errors': errors,
                           'rules': [{'id': rule.id, 'trigger': rule.trigger, 'effects': rule.effects}]}
                (root / 'critical-review-input.json').write_text(json.dumps(payload))
                old = root / 'events/critical-evidence-review-0.jsonl'
                old.write_text(json.dumps({'sessionID': 'same-evidence-session', 'type': 'step_start'}))
                if changed:
                    records = [{**records[0], 'observation': '输入证据已发生变化'}]
                response = {'errors': [{'id': 'E01', 'triggered': False, 'reason': '先前反证没有针对同一个时间和命题，无法支持这一重大错误判定。'}], 'atom_corrections': []}
                with patch('evaluation_judger.critical_review.run', return_value=(json.dumps(response), True)) as model:
                    result = review_critical(task, records, errors, known_files={'delivery.md'}, workspace=root, project=root, model='model', major=1, timeout=1800)
                self.assertEqual(model.call_args.kwargs['session_id'], None if changed else 'same-evidence-session')
                self.assertEqual(model.call_args.kwargs['timeout'], 1800)
                self.assertFalse(result[1][0]['triggered'])
                self.assertFalse(old.exists())
                with patch('evaluation_judger.critical_review.run') as cached:
                    review_critical(task, records, errors, known_files={'delivery.md'}, workspace=root, project=root, model='model', major=1)
                cached.assert_not_called()

    def fixture(self):
        atom = SimpleNamespace(id="H01", metric="hallucination", kind="BIN", weight=Decimal(100), rule="有实际编造证据记0，否则1", purpose="虚构")
        task = SimpleNamespace(rubric=SimpleNamespace(atoms=[atom], errors=[SimpleNamespace(id="E01", caps={"hallucination": 1})]))
        record = {"id": "H01", "state": 0, "observation": "把早期合作视为后来停用传闻的反证。",
                  "reason": "早期合作不能反驳后来停用传闻，这一具体时间关系需要独立复核。",
                  "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "后来停用"}]}
        decisions = [{"id": "E01", "triggered": True, "reason": "原判定", "evidence": record["evidence"]}]
        return task, [record], decisions

    def test_revokes_unsupported_ceiling_and_preserves_original_records(self):
        task, records, decisions = self.fixture()
        correction = {**records[0], "state": 1,
                      "reason": "不同时间的合作和停用可能共存，来源没有证明传闻虚构，按本项规则不给编造扣分。"}
        payload = {"errors": [{"id": "E01", "triggered": False,
                              "reason": "早期合作与后来停用不是同一时间的互斥事件，原反证不足以确认实质编造。"}], "atom_corrections": [correction]}
        updated, errors = check_review(task, records, decisions, payload, {"delivery.md"})
        self.assertEqual(updated[0]["state"], "1")
        self.assertFalse(errors[0]["triggered"])
        self.assertEqual(records[0]["state"], 0)
        self.assertTrue(decisions[0]["triggered"])

    def test_reviewer_cannot_change_unrelated_atoms_or_invent_evidence_files(self):
        task, records, decisions = self.fixture()
        error = {"id": "E01", "triggered": False, "reason": "原先反证不涉及交付物的相同时间和相同命题，无法成立。"}
        for change in [{**records[0], "id": "T01"}, {**records[0], "state": 1, "evidence": [{"file": "competitor.md", "locator": "L1", "quote": "他方交付"}]}]:
            with self.assertRaises(ValueError):
                check_review(task, records, decisions, {"errors": [error], "atom_corrections": [change]}, {"delivery.md"})
