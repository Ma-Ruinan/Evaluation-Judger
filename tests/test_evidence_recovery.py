from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock

from evaluation_judger.critical_review import check_review
from evaluation_judger.judge import _error_decisions, _digest
from evaluation_judger.opencode import JudgeError
from evaluation_judger.scoring import normalize_evidence_files, validate_atom, validate_error_decisions


class EvidenceRecovery(TestCase):
    def fixture(self):
        task = SimpleNamespace(id='demo', rubric=SimpleNamespace(errors=[
            SimpleNamespace(id='E01', trigger='specific fabrication', effects='accuracy ceiling', caps={'accuracy': Decimal(1)})], atoms=[]))
        records = [{'id': 'A01', 'state': '0', 'reason': 'The supplied claim contradicts the inspected source.'}]
        error = {'id': 'E01', 'triggered': True, 'reason': '明确的来源反证与交付中的对应主张相冲突，满足当前规则触发条件。',
                 'evidence': [{'file': 'delivery.txt', 'locator': 'L1', 'quote': 'claim'}]}
        return task, records, error

    def test_unique_extraction_alias_preserves_original_spelling(self):
        raw = [{'file': 'question__问题描述.txt', 'locator': 'L1', 'quote': '题目'}]
        result = normalize_evidence_files(raw, {'question/问题描述.txt', 'extracted/question__问题描述.txt.txt'})
        self.assertEqual(result[0]['file'], 'extracted/question__问题描述.txt.txt')
        self.assertEqual(result[0]['file_as_returned'], raw[0]['file'])
        self.assertEqual(raw[0]['file'], 'question__问题描述.txt')

    def test_ambiguous_unknown_and_traversal_references_remain_rejected(self):
        known = {'sources/report.txt', 'deliveries/report.txt'}
        for value in ('report.txt', 'invented.txt', '../report.txt', 'C:\\outside\\report.txt', '/outside/report.txt'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_evidence_files([{'file': value}], known)
        self.assertEqual(normalize_evidence_files([{'file': 'deliveries\\report.txt'}], known)[0]['file'], 'deliveries/report.txt')

    def test_atom_normalization_changes_no_score_or_reason(self):
        atom = SimpleNamespace(id='T01', kind='BIN', weight=Decimal(100), metric='completion', rule='BIN', purpose='交付')
        record = {'id': 'T01', 'state': 1, 'observation': '交付文件已完整保存且可以正常阅读。',
                  'reason': '交付文件包含完整正文并且可以正常打开，满足当前原子的完成条件。',
                  'evidence': [{'file': 'report.txt', 'locator': 'L1', 'quote': '正文'}]}
        result = validate_atom(record, atom, {'deliveries/report.txt'})
        self.assertEqual(result['state'], '1')
        self.assertEqual(result['reason'], record['reason'])
        self.assertEqual(result['evidence'][0]['file'], 'deliveries/report.txt')

    def test_error_phase_repairs_only_bad_response_then_reuses_checkpoint(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'events').mkdir()
            task, records, error = self.fixture()
            invalid = {**error, 'evidence': [{'file': 'invented.txt', 'locator': 'L1', 'quote': 'claim'}]}
            session = SimpleNamespace(ask=Mock(side_effect=[(json.dumps({'errors': [invalid]}), True), (json.dumps({'errors': [error]}), True)]))
            result = _error_decisions(task, 'subject', {}, records, {'delivery.txt'}, root, session)
            self.assertEqual(result, [error])
            self.assertEqual(session.ask.call_count, 2)
            self.assertIn('invented.txt', session.ask.call_args_list[1].args[0])
            self.assertEqual(_error_decisions(task, 'subject', {}, records, {'delivery.txt'}, root, session), result)
            self.assertEqual(session.ask.call_count, 2)
            self.assertEqual(len(records), 1)

    def test_invalid_error_phase_is_bounded_and_does_not_publish_checkpoint(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'events').mkdir()
            task, records, _ = self.fixture()
            session = SimpleNamespace(ask=Mock(return_value=(json.dumps({'errors': []}), True)))
            with self.assertRaises(JudgeError):
                _error_decisions(task, 'subject', {}, records, {'delivery.txt'}, root, session)
            self.assertEqual(session.ask.call_count, 2)
            self.assertFalse((root / 'checkpoints/errors.json').exists())

    def test_complete_error_event_is_reused_only_for_matching_atom_hash(self):
        for changed in (False, True):
            with self.subTest(changed=changed), TemporaryDirectory() as folder:
                root = Path(folder); (root / 'events').mkdir()
                task, records, error = self.fixture()
                digest = _digest(records)
                path = root / f'events/errors-{digest[:12]}-0.jsonl'
                path.write_text(json.dumps({'type': 'text', 'part': {'text': 'BEGIN_JUDGMENT '+json.dumps({'errors': [error]})+' END_JUDGMENT'}}))
                if changed:
                    records[0]['state'] = '1'
                session = SimpleNamespace(ask=Mock(return_value=(json.dumps({'errors': [error]}), True)))
                self.assertEqual(_error_decisions(task, 'subject', {}, records, {'delivery.txt'}, root, session), [error])
                self.assertEqual(session.ask.call_count, int(changed))

    def test_critical_review_uses_same_authorized_path_normalization(self):
        task, _, error = self.fixture()
        reviewed = {**error, 'evidence': [{'file': 'question__问题描述.txt', 'locator': 'L1', 'quote': '题目'}]}
        _, decisions = check_review(task, [], [error], {'errors': [reviewed], 'atom_corrections': []}, {'extracted/question__问题描述.txt.txt'})
        self.assertEqual(decisions[0]['evidence'][0]['file'], 'extracted/question__问题描述.txt.txt')
        self.assertEqual(reviewed['evidence'][0]['file'], 'question__问题描述.txt')

    def test_visual_error_evidence_keeps_existing_image_contract(self):
        task, _, error = self.fixture()
        reviewed = {**error, 'evidence': [{'file': 'chart.png', 'locator': '纵轴与图例', 'kind': 'visual',
                                          'description': '纵轴单位与图例指标口径明确相互冲突。'}]}
        initial = validate_error_decisions([reviewed], task.rubric.errors, {'deliveries/chart.png'})
        self.assertNotIn('quote', initial[0]['evidence'][0])
        _, final = check_review(task, [], [error], {'errors': initial, 'atom_corrections': []}, {'deliveries/chart.png'})
        self.assertEqual(initial, final)
        invalid = {**reviewed, 'evidence': [{**reviewed['evidence'][0], 'description': '模糊'}]}
        with self.assertRaises(ValueError):
            validate_error_decisions([invalid], task.rubric.errors, {'deliveries/chart.png'})
