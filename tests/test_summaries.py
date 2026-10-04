import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.dataset import sha256
from evaluation_judger.judge import write_json
from evaluation_judger.render import summaries, _summary_text, _loss_brief, task_markdown
from evaluation_judger.rubric import METRICS


class SummaryContract(TestCase):
    def test_difference_table_explains_positive_evidence_for_two_and_three_subjects(self):
        atom = SimpleNamespace(id='A01', metric='accuracy')
        task = SimpleNamespace(id='1.1', name='检验', dimension='维度一', rubric=SimpleNamespace(atoms=[atom]))
        records = []
        for sid, state, value in [('a', '0', '0.0000'), ('b', '1', '0.9166'), ('c', '1', '0.9166')]:
            scores = {m: {'final': '100' if m == 'completion' else '5'} for m in METRICS}; scores['quality_mean'] = '5'
            record = {'id': 'A01', 'metric': 'accuracy', 'state': state, 'weight': '100', 'items': [],
                      'observation': f'交付报告明确给出 p={value}，位于结果表第二行。',
                      'reason': f'p={value}，与标准值核验。', 'denominator': None, 'evidence': []}
            records.append({'subject': sid, 'scores': scores, 'atoms': [record], 'errors': []})
        for count in (2, 3):
            text = task_markdown(task, records[:count], {'a': '对象A', 'b': '对象B', 'c': '对象C'})
            table = text.split('## 逐项差异速览')[1].split('逐项依据')[0]
            self.assertIn('p=0.0000', table)
            self.assertIn('对象B：交付报告明确给出 p=0.9166', table)
            self.assertNotIn('本项无失分', table)
            if count == 3: self.assertIn('对象C：交付报告明确给出 p=0.9166', table)

    def test_binary_loss_preserves_the_finding_after_a_satisfied_condition(self):
        reason = '第二项条件满足。' + '该条件的核验说明。' * 25 + '但第一项不满足：同一指标累计9.47亿元小于单日11亿元，L171与L175矛盾。'
        brief = _loss_brief({'id': 'H02', 'state': '0', 'reason': reason})
        self.assertIn('累计9.47亿元小于单日11亿元', brief)
        self.assertIn('L171与L175矛盾', brief)

    def test_embedded_process_note_is_removed_without_changing_evidence_date(self):
        original = '报告截止2026-09-25。process-record.json 记 executed_at=null、runtime=237m21s、status=一次运行成功，无更强执行时点；自报截止与文件日期一致。'
        text = _summary_text(original)
        self.assertNotIn('runtime=', text)
        self.assertNotIn('一次运行成功', text)
        self.assertIn('2026-09-25', text)
        self.assertIn('自报截止与文件日期一致', text)

    def test_all_scores_differences_and_process_archive_without_changing_judgments(self):
        for count in (1, 2, 3):
            with self.subTest(subject_count=count), TemporaryDirectory() as directory:
                root = Path(directory)
                subjects = [{'id': str(i), 'name': f'对象{i}'} for i in range(count)]
                task = SimpleNamespace(id='1.1', name='样例', dimension='维度一', rubric=SimpleNamespace(atoms=[]))
                config = {'subjects': subjects, 'model': 'test'}
                results = {}
                before = {}
                for i, subject in enumerate(subjects):
                    scores = {m: {'final': '100' if m == 'completion' else str(i + 1)} for m in METRICS}
                    scores['quality_mean'] = str(i + 1)
                    result = {'subject': subject['id'], 'task_id': task.id, 'atoms': [], 'errors': [], 'scores': scores, 'fingerprint': 'frozen'}
                    results[(task.id, subject['id'])] = result
                    path = root/'results'/task.id/(subject['id']+'.json')
                    write_json(path, result)
                    before[path] = sha256(path)
                process = {'1.1': {s['id']: {'runtime': '3m2s', 'status': '一次运行成功'} for s in subjects}}
                with patch('evaluation_judger.render.process_records', return_value=process):
                    dimension = summaries([task], results, config, root)['维度一']
                text = (root/'results/dimension-1-summary.md').read_text(encoding='utf-8')
                self.assertIn('逐题六项分数与差异', text)
                for metric in METRICS:
                    self.assertIn(metric, json.dumps(dimension))
                self.assertNotIn('runtime', json.dumps(dimension))
                self.assertNotIn('一次运行成功', text)
                self.assertEqual(len(dimension['task_results'][0]['differences']), count-1)
                if count > 1:
                    self.assertEqual(dimension['task_results'][0]['differences']['1']['accuracy'], '1')
                archive = json.loads((root/'process-records/dimension-1-subject-tests.json').read_text(encoding='utf-8'))
                self.assertEqual(archive['subjects']['0']['records'][0]['runtime'], '3m2s')
                self.assertTrue(all(sha256(path) == digest for path, digest in before.items()))
