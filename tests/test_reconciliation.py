import copy,json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from evaluation_judger.reconciliation import validate_resolution,reconcile_reviews
from evaluation_judger.opencode import JudgeError
from test_contract_review import fixture

class ReconciliationTests(TestCase):
    def setUp(self):
        self.task,self.records=fixture()
        correction={**self.records[0],'state':'0','observation':'原文确实缺少该项所要求的实质内容。','reason':'核验原始交付后确认该项要求未满足，按固定判据给出零状态，保留其他有效判断。'}
        self.payload={'checks':{k:'已按原文与固定标准核验具体判据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[correction],'errors':[],
                      'disagreements':[{'atom_id':'X0','resolution':'不同复核使用了不同时间口径，已核对原文对应的同一时期并按固定判据解决分歧。'}]}
    def validate(self,payload=None,allowed=()):
        return validate_resolution(payload or self.payload,self.task,self.records,{'delivery.md'},relation_inventory=None,contested_atom_ids=['X0'],allowed_errors=set(allowed))
    def test_resolves_contested_atom_and_keeps_original_records_unchanged(self):
        final,errors,_=self.validate();self.assertEqual(final[0]['state'],'0');self.assertEqual(self.records[0]['state'],'1');self.assertEqual(errors,[])
    def test_omitted_or_unknown_disagreements_are_rejected(self):
        for findings in ([],[{'atom_id':'unknown','resolution':'核验原文后确认具体依据和判据，其他记录保持原样。'}]):
            bad={**self.payload,'disagreements':findings}
            with self.assertRaises(ValueError):self.validate(bad)
    def test_unauthorized_evidence_and_unreviewed_severe_error_are_rejected(self):
        bad=copy.deepcopy(self.payload);bad['corrections'][0]['evidence'][0]['file']='outside.txt'
        with self.assertRaises(ValueError):self.validate(bad)
        self.task.rubric.errors=[SimpleNamespace(id='E01',caps={})]
        bad={**self.payload,'errors':[{'id':'E01','triggered':True,'reason':'确认原文显示实质错误','evidence':[{'file':'delivery.md','locator':'L1','quote':'具体内容'}]}]}
        with self.assertRaises(ValueError):self.validate(bad)
    def test_semantic_correction_alias_and_detailed_observation_repair_no_score_change(self):
        raw=copy.deepcopy(self.payload); raw['atom_corrections']=raw.pop('corrections')
        raw['atom_corrections'][0]['reason']='按固定判据不满足。'
        raw['atom_corrections'][0]['observation']='原始交付文件没有提供该条所要求的具体内容，逐段查找后的结果能够直接支持这一判定。'
        final,_,review=self.validate(raw)
        self.assertEqual(final[0]['state'],'0');self.assertIn(raw['atom_corrections'][0]['observation'],final[0]['reason'])
        self.assertTrue(review['normalization_notes']);self.assertEqual(raw['atom_corrections'][0]['reason'],'按固定判据不满足。')
        conflicting={**raw,'corrections':[]}
        with self.assertRaises(ValueError):self.validate(conflicting)

    def test_explicit_error_resolution_identifiers_are_accepted_without_omitting_atoms(self):
        self.task.rubric.errors=[SimpleNamespace(id='E01',caps={})]
        raw=copy.deepcopy(self.payload);raw['errors']=[{'id':'E01','triggered':False,'reason':'核验原文后该项关键错误触发条件未得到支持。','evidence':[]}]
        raw['disagreements'].append({'error_id':'E01','resolution':'独立检查关键错误对应的同一时间与命题，原反证不成立，因此不触发该项封顶。'})
        self.assertFalse(self.validate(raw)[1][0]['triggered'])
        raw['disagreements']=raw['disagreements'][1:]
        with self.assertRaises(ValueError):self.validate(raw)

    def test_explicit_unchanged_ratio_preserves_full_prior_items_but_not_changed_operands(self):
        self.task.rubric.atoms[1].kind='RATIO'
        self.records[1].update(kind='RATIO',state='0.5',numerator=2,denominator=4,
            items=[{'claim':f'具体主张{i}', 'result':i<2, 'location':f'L{i+1}'} for i in range(4)])
        raw=copy.deepcopy(self.payload)
        raw['corrections'].append({**self.records[1],'items':['两项满足、两项不满足'],'change':'维持原判断'})
        final,_,review=self.validate(raw)
        self.assertEqual(final[1]['items'],self.records[1]['items']);self.assertTrue(review['normalization_notes'])
        raw['corrections'][-1].update(numerator=3,state='0.75')
        with self.assertRaises(ValueError):self.validate(raw)

    def native(self,root):
        return reconcile_reviews(self.task,self.records,[],contested_decisions=[],contract_review={'status':'passed'},critical_review={'review':{'atom_corrections':[{'id':'X0'}]}},relation_inventory=None,workspace=root,project=root,known_files={'delivery.md'},model='model',major=1)
    def test_read_required_bounded_retry_and_valid_cache(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('evaluation_judger.reconciliation.run',side_effect=[(json.dumps(self.payload),False),(json.dumps(self.payload),True)]) as model:
                result=self.native(root)
            self.assertEqual(model.call_count,2);self.assertEqual(result[2]['status'],'passed')
            with patch('evaluation_judger.reconciliation.run') as cached:self.native(root)
            cached.assert_not_called()
    def test_older_valid_candidate_is_recovered_when_latest_draft_is_invalid(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)
            with patch('evaluation_judger.reconciliation.run',return_value=(json.dumps(self.payload),True)):self.native(root)
            first=next(root.glob('review-reconciliation-candidate*.json'))
            next_path=first.with_name(first.name.rsplit('-',1)[0]+'-1.json')
            next_path.write_text(json.dumps({**self.payload,'disagreements':[]}))
            next((root/'checkpoints').glob('review-reconciliation-*.json')).unlink()
            with patch('evaluation_judger.reconciliation.run') as model:result=self.native(root)
            model.assert_not_called();self.assertEqual(result[2]['recovered_from_candidate'],first.name)
            self.assertTrue(next_path.exists())

    def test_failed_candidates_are_preserved_and_native_resume_uses_diagnostic(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);bad={**self.payload,'disagreements':[]}
            with patch('evaluation_judger.reconciliation.run',return_value=(json.dumps(bad),True)),self.assertRaises(JudgeError):self.native(root)
            self.assertEqual(len(list(root.glob('review-reconciliation-candidate*.json'))),2)
            with patch('evaluation_judger.reconciliation.run',return_value=(json.dumps(self.payload),True)) as model:result=self.native(root)
            self.assertIn('Unresolved contested atoms: X0',model.call_args.args[1]);self.assertEqual(result[2]['status'],'passed')
            self.assertEqual(len(list(root.glob('review-reconciliation-candidate*.json'))),3)
