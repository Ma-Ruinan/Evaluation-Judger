import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from evaluation_judger.judge import _review_progress
from evaluation_judger.opencode import JudgeError
from test_contract_review import fixture

class ReviewProgressTests(TestCase):
    def test_restores_complete_latest_review_without_editing_original_records(self):
        task, records = fixture()
        updated = copy.deepcopy(records); updated[0]['state'] = '0'
        with TemporaryDirectory() as folder:
            root = Path(folder)
            _review_progress(task, updated, workspace=root, fingerprint='fixed', known_files={'delivery.md'}, save=True)
            restored = _review_progress(task, records, workspace=root, fingerprint='fixed', known_files={'delivery.md'})
            self.assertEqual(restored[0]['state'], '0')
            self.assertEqual(records[0]['state'], '1')
            self.assertEqual(json.loads((root/'checkpoints/review-progress.json').read_text())['stage'], 'needs_contract_review')
            self.assertFalse((root/'results').exists())
            self.assertEqual(len(list((root/'checkpoints').glob('review-progress-*.json'))), 1)

    def test_changed_inputs_or_corrupted_checkpoint_are_not_resumed(self):
        task, records = fixture(); updated = copy.deepcopy(records); updated[0]['state'] = '0'
        with TemporaryDirectory() as folder:
            root = Path(folder)
            _review_progress(task, updated, workspace=root, fingerprint='fixed', known_files={'delivery.md'}, save=True)
            self.assertIs(_review_progress(task, records, workspace=root, fingerprint='changed', known_files={'delivery.md'}), records)
            path=root/'checkpoints/review-progress.json'; saved=json.loads(path.read_text()); saved['atoms'][0]['state']='1'; path.write_text(json.dumps(saved))
            self.assertIs(_review_progress(task, records, workspace=root, fingerprint='fixed', known_files={'delivery.md'}), records)

    def test_incomplete_duplicate_or_unauthorized_records_cannot_be_saved(self):
        task, records = fixture(); unauthorized=copy.deepcopy(records)
        unauthorized[0]['evidence'][0]['file']='outside.txt'
        with TemporaryDirectory() as folder:
            root = Path(folder)
            for invalid in (records[:-1], records+[records[0]], unauthorized):
                with self.subTest(size=len(invalid)), self.assertRaises(JudgeError):
                    _review_progress(task, invalid, workspace=root, fingerprint='fixed', known_files={'delivery.md'}, save=True)
            self.assertFalse((root/'checkpoints/review-progress.json').exists())
