import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from evaluation_judger.judge import judge_task, write_json, write_text, atom_records
from evaluation_judger.rubric import METRICS
from evaluation_judger.scoring import calculate


class RegroupedCheckpoints(TestCase):
    def test_fresh_judgment_with_no_atom_checkpoints(self):
        atoms = [SimpleNamespace(id=f'X{i}', metric=metric, weight=Decimal(100), kind='BIN', rule='核验交付', purpose='核验', evidence='可定位正文') for i, metric in enumerate(METRICS)]
        task = SimpleNamespace(id='demo', name='全新评分', dimension='维度', rubric=SimpleNamespace(atoms=atoms, errors=[], text='固定标准', by_metric=lambda m: [a for a in atoms if a.metric == m]))
        records = [{'id': a.id, 'state': 1, 'observation': '交付包含明确的完整内容。', 'reason': '交付具体内容与评分要求一致，并有以下逐字证据支持判定。', 'evidence': [{'file': 'delivery.md', 'locator': 'L1', 'quote': '具体证据'}]} for a in atoms]
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'probe-ok.json').write_text('{}')
            manifest = {'fingerprint': 'fixed', 'process_record': {}, 'files': [{'file': 'delivery.md', 'extracted': 'delivery.md'}]}
            def answer(prompt, title, output):
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps({'type': 'text', 'part': {'text': 'BEGIN_JUDGMENT '+json.dumps({'atoms': records})+' END_JUDGMENT'}}), encoding='utf-8')
                return '', True
            manifest['files'][0]['limitation'] = None
            with patch('evaluation_judger.judge.workspace_for', return_value=(root, manifest)), patch('evaluation_judger.judge.TaskSession.ask', side_effect=answer) as model:
                result = judge_task(task, 'subject', {'batch_size': 6}, root, root)
            self.assertEqual(model.call_count, 1)
            self.assertEqual(len(result['atoms']), 6)
            self.assertEqual(result['scores']['quality_mean'], '5')

    def test_shared_sample_repair_reports_error_and_preserves_old_events(self):
        atoms = [SimpleNamespace(id=f'X{i}', metric=metric, weight=Decimal(100), kind='BIN', rule='核验交付', purpose='核验') for i, metric in enumerate(METRICS)]
        source = next(a for a in atoms if a.metric == 'accuracy'); source.id='A01'; source.kind='CLAIM-RATIO'
        target = next(a for a in atoms if a.metric == 'hallucination'); target.id='H01'; target.kind='CLAIM-RATIO'; target.rule='主张切分与核验全集同A01'
        task = SimpleNamespace(id='demo', name='样本恢复', dimension='维度', rubric=SimpleNamespace(atoms=atoms, errors=[], by_metric=lambda m: [a for a in atoms if a.metric == m]))
        base = {'state': 1, 'observation': '交付内容有明确支撑且已逐项检查。', 'reason': '已核对当前主张及对应来源，证据明确满足当前评分要求。', 'evidence': [{'file': 'delivery.md', 'locator': 'L1', 'quote': '具体证据'}]}
        records = [{**base, 'id': a.id} for a in atoms]
        items = [{'claim': '主张一', 'result': 'supported'}, {'claim': '主张二', 'result': 'supported'}]
        next(r for r in records if r['id']=='A01').update(numerator=2, denominator=2, items=items)
        initial = next(r for r in records if r['id']=='H01'); initial.update(numerator=1, denominator=1, items=items[:1])
        valid = {**initial, 'numerator': 2, 'denominator': 2, 'items': items}
        with TemporaryDirectory() as folder:
            root=Path(folder); (root/'events').mkdir(); (root/'probe-ok.json').write_text('{}')
            for i, record in enumerate(records):
                (root/f'events/atoms-{i:03d}-attempt-0.jsonl').write_text(json.dumps({'type':'text','part':{'text':'BEGIN_JUDGMENT '+json.dumps(record)+' END_JUDGMENT'}}),encoding='utf-8')
            old=root/'events/shared-review-H01-attempt-0.jsonl'
            old.write_text(json.dumps({'type':'text','part':{'text':'BEGIN_JUDGMENT '+json.dumps({'atom':initial})+' END_JUDGMENT'}}),encoding='utf-8')
            original=old.read_bytes()
            manifest={'fingerprint':'fixed','process_record':{},'files':[{'file':'delivery.md','extracted':'delivery.md'}]}
            with patch('evaluation_judger.judge.workspace_for',return_value=(root,manifest)), patch('evaluation_judger.judge.run',side_effect=[(json.dumps({'atom':initial}),True),(json.dumps({'atom':valid}),True)]) as model:
                result=judge_task(task,'subject',{'batch_size':1},root,root)
            self.assertEqual(model.call_count,2)
            self.assertIn('Expected exactly 2',model.call_args_list[1].args[1])
            self.assertEqual([call.args[3].name for call in model.call_args_list],['shared-review-H01-attempt-1.jsonl','shared-review-H01-attempt-2.jsonl'])
            self.assertEqual(old.read_bytes(),original)
            self.assertEqual(next(r for r in result['atoms'] if r['id']=='H01')['denominator'],2)

    def test_direct_atom_logs_resume_without_model_calls(self):
        atoms = [SimpleNamespace(id=f'X{i}', metric=metric, weight=Decimal(100), kind='BIN', rule='检查具体交付', purpose='核验') for i, metric in enumerate(METRICS)]
        task = SimpleNamespace(id='demo', name='日志恢复', dimension='维度', rubric=SimpleNamespace(atoms=atoms, errors=[], by_metric=lambda m: [a for a in atoms if a.metric == m]))
        with TemporaryDirectory() as folder:
            root = Path(folder); (root / 'events').mkdir(); (root / 'probe-ok.json').write_text('{}')
            for i, atom in enumerate(atoms):
                record = {'id': atom.id, 'state': 1, 'observation': '交付包含可定位的完整证据。', 'reason': '已核对具体交付内容和对应要求，记录的证据确实满足该评分项。', 'evidence': [{'file': 'delivery.md', 'locator': 'L1', 'quote': '具体证据'}]}
                envelope = record if i % 2 == 0 else [record]
                (root / f'events/atoms-{i:03d}-attempt-0.jsonl').write_text(json.dumps({'type': 'text', 'part': {'text': 'BEGIN_JUDGMENT '+json.dumps(envelope)+' END_JUDGMENT'}}))
            manifest = {'fingerprint': 'fixed', 'process_record': {}, 'files': [{'file': 'delivery.md', 'extracted': 'delivery.md'}]}
            with patch('evaluation_judger.judge.workspace_for', return_value=(root, manifest)), patch('evaluation_judger.opencode.run') as model:
                result = judge_task(task, 'subject', {}, root, root)
            model.assert_not_called()
            self.assertEqual(len(result['atoms']), len(atoms))
            self.assertEqual(result['scores']['quality_mean'], '5')

    def test_single_atom_envelopes_preserve_original_record(self):
        record = {'id': 'A02', 'state': '11/17', 'items': [{'claim': '原主张'}]}
        for payload in ({'atoms': [record]}, {'atom': record}, record, [record]):
            self.assertEqual(atom_records(payload), [record])
            self.assertIs(atom_records(payload)[0], record)
        self.assertEqual(atom_records({'id': 'A02', 'unrelated': 'text'}), [])

    def test_unchanged_markdown_is_not_rewritten(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'compare-result.md'
            path.write_text('完整证据', encoding='utf-8')
            with patch('evaluation_judger.judge.Path.replace', side_effect=PermissionError('preview holds file')) as replace:
                write_text(path, '完整证据')
            replace.assert_not_called()

    def test_critical_review_reuses_locked_result_instead_of_superseded_checkpoints(self):
        atoms = [SimpleNamespace(id=str(i), metric=metric, weight=Decimal(100), kind="BIN", rule="检查该项", purpose="测试")
                 for i, metric in enumerate(METRICS)]
        rubric = SimpleNamespace(atoms=atoms, errors=[], by_metric=lambda metric: [a for a in atoms if a.metric == metric])
        task = SimpleNamespace(id="1.1", rubric=rubric)
        records = [{"id": a.id, "state": "1", "original_detail": "后来复核完善的锁定说明"} for a in atoms]
        result = {"fingerprint": "fixed", "atoms": records, "errors": [], "scores": calculate(rubric, records, [])}
        with TemporaryDirectory() as folder:
            root = Path(folder); workspace = root / "workspace"; workspace.mkdir()
            (workspace / "checkpoints").mkdir()
            stale = workspace / "checkpoints/atoms-000.json"
            stale.write_text("deliberately invalid old checkpoint", encoding="utf-8")
            path = root / "results/1.1/x.json"; write_json(path, result)
            manifest = {"fingerprint": "fixed", "files": [{"file": "delivery.md", "extracted": "delivery.md"}]}
            with patch("evaluation_judger.judge.workspace_for", return_value=(workspace, manifest)), \
                 patch("evaluation_judger.judge.run") as model:
                revised = judge_task(task, "x", {"critical_error_review": True}, root, root)
            model.assert_not_called()
            self.assertEqual(revised["atoms"], records)
            self.assertEqual(revised["scores"], result["scores"])
            self.assertEqual(revised["critical_error_review_version"], 1)
            self.assertEqual(stale.read_text(), "deliberately invalid old checkpoint")

    def test_brief_windows_lock_retries_without_losing_prior_checkpoint(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'checkpoint.json'
            write_json(path, {'version': 'old'})
            original_replace = Path.replace
            attempts = []
            def replace(source, target):
                attempts.append(1)
                if len(attempts) <= 2:
                    self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'version': 'old'})
                    raise PermissionError('temporary preview lock')
                return original_replace(source, target)
            with patch('evaluation_judger.judge.Path.replace', new=replace), patch('evaluation_judger.judge.time.sleep'):
                write_json(path, {'version': 'new'})
            self.assertEqual(len(attempts), 3)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')), {'version': 'new'})

    def test_old_mixed_batches_resume_without_model_calls(self):
        atoms = [SimpleNamespace(id=f"X{i}", metric=metric, weight=Decimal(100),
                                 kind="CLAIM-RATIO" if metric == "accuracy" else "BIN",
                                 rule="核验该事实是否有来源支持", purpose="测试核验")
                 for i, metric in enumerate(METRICS)]
        rubric = SimpleNamespace(atoms=atoms, errors=[], by_metric=lambda metric: [a for a in atoms if a.metric == metric])
        task = SimpleNamespace(id="demo", name="跨批次恢复", dimension="维度一", rubric=rubric)
        records = []
        for atom in atoms:
            record = {"id": atom.id, "state": 1, "observation": "已检查交付中的具体事实及来源",
                      "reason": "交付明确给出了该事实和可定位的来源，两者一致并满足规则要求。",
                      "evidence": [{"file": "delivery.md", "locator": "L1", "quote": "已核验事实"}]}
            if atom.kind == "CLAIM-RATIO":
                record.update(numerator=1, denominator=1, items=[{"claim": "已核验事实", "result": "supported"}])
            records.append(record)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "checkpoints").mkdir()
            (root / "probe-ok.json").write_text("{}", encoding="utf-8")
            for start in (0, 4):
                (root / "checkpoints" / f"atoms-{start:03d}.json").write_text(
                    json.dumps({"fingerprint": "fixed", "atoms": records[start:start + 4]}, ensure_ascii=False), encoding="utf-8")
            manifest = {"fingerprint": "fixed", "files": [{"file": "delivery.md", "extracted": "delivery.md"}], "process_record": {}}
            with patch("evaluation_judger.judge.workspace_for", return_value=(root, manifest)), \
                 patch("evaluation_judger.judge.run") as model:
                result = judge_task(task, "a", {"batch_size": 2}, root, root / "run")
            model.assert_not_called()
            self.assertEqual([record["id"] for record in result["atoms"]], [atom.id for atom in atoms])
            self.assertEqual(result["scores"]["quality_mean"], "5")
