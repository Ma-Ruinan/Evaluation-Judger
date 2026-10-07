import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from evaluation_judger.dataset import task_fingerprint
from evaluation_judger.judge import workspace_for
from evaluation_judger.rubric import load_rubric
from test_dataset import fixture_rubric
from evaluation_judger.scoring import shared_items_match, is_source_url, normalize_evidence_files
from evaluation_judger.multisample import _label


class TemperatureTests(unittest.TestCase):
    def test_http_source_is_preserved_without_allowing_unknown_local_paths(self):
        for url in ('http://m.epaper.zqrb.cn/html/2026-04/18/content_1232845.htm', 'https://example.org/source'):
            evidence = [{'file': url, 'locator': '原文第2段', 'quote': '逐字引文'}]
            self.assertTrue(is_source_url(url))
            self.assertEqual(normalize_evidence_files(evidence, set()), evidence)
        for invalid in ('http:///missing-host', 'ftp://example.org/page', 'file:///C:/secret.txt', 'C:/secret.txt', '../outside.txt'):
            self.assertFalse(is_source_url(invalid))
            with self.assertRaises(ValueError):
                normalize_evidence_files([{'file': invalid}], set())

    def test_subject_claim_alias_preserves_identity_without_accepting_different_samples(self):
        source = [{'subject': '2023年12月中央经济工作会议将低空经济列为战略性新兴产业之一', 'result': '支持'}]
        same = [{'subject': source[0]['subject'], 'result': '不支持'}]
        self.assertTrue(shared_items_match(source, same))
        self.assertTrue(shared_items_match(source, [{'claim': source[0]['subject']}]))
        self.assertEqual(_label(source[0]), source[0]['subject'])
        self.assertFalse(shared_items_match(source, [{'subject': '另一个产品的定价和发布日期'}]))
        self.assertFalse(shared_items_match(source, source + source))
        self.assertFalse(shared_items_match(source, [{'location': 'same page'}]))
        self.assertFalse(shared_items_match(source, [{'id': source[0]['subject']}]))
        for alias in ('claim', 'element', 'statement', 'subject', 'item', 'name'):
            with self.subTest(alias=alias):
                alternative = {alias: source[0]['subject']}
                self.assertTrue(shared_items_match(source, [alternative]))
                self.assertEqual(_label(alternative), source[0]['subject'])

    def test_workspace_override_isolated_and_resume_fingerprint_changes(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            question, rubric, delivery = [root / name for name in ('question.txt', 'rubric.md', 'actor-answer.md')]
            question.write_text('question', encoding='utf-8')
            rubric.write_text(fixture_rubric(), encoding='utf-8')
            delivery.write_text('answer', encoding='utf-8')
            task = SimpleNamespace(id='1.1', name='test', directory=root, question=question,
                                   rubric=load_rubric(rubric), sources=(), deliveries={'x': (delivery,)})
            project = Path(__file__).resolve().parents[1]
            original = (project / '.opencode/agents/judge.md').read_bytes()
            config = {'subjects': [{'id': 'x', 'prefix': 'actor-'}]}
            baseline = task_fingerprint(task, 'x', config)
            with patch('evaluation_judger.judge.process_records', return_value={'1.1': {'x': {}}}):
                default, _ = workspace_for(task, 'x', config, project, root / 'run')
                zero, _ = workspace_for(task, 'x', {**config, 'temperature': 0}, project, root / 'run')
            self.assertNotEqual(default, zero)
            self.assertIn('temperature: 0\n', (zero / '.opencode/agents/judge.md').read_text(encoding='utf-8'))
            self.assertEqual((default / '.opencode/agents/judge.md').read_bytes(), original)
            self.assertEqual((project / '.opencode/agents/judge.md').read_bytes(), original)
            self.assertEqual(baseline, task_fingerprint(task, 'x', config))
            for invalid in (True, '0', -1, 3, float('nan'), float('inf')):
                with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                    task_fingerprint(task, 'x', {**config, 'temperature': invalid})
