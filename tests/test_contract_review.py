import json
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from pypdf import PdfWriter
from pypdf.annotations import Link

from evaluation_judger.materials import extract, structured_evidence
from evaluation_judger.contract_review import count_issues, validate_review, review_contract, _merge_review_patch
from evaluation_judger.rubric import METRICS


def fixture():
    atoms = [SimpleNamespace(id=f'X{i}', metric=m, weight=Decimal(100), kind='BIN',
                             rule='满足具体证据条件为1，否则为0', purpose='核验内容') for i,m in enumerate(METRICS)]
    records = [{'id':a.id,'kind':'BIN','metric':a.metric,'weight':'100','state':'1',
                'observation':'交付包含该项要求的具体内容。','reason':'已经核对对应交付位置，具体内容符合这一评分项的要求。',
                'evidence':[{'file':'delivery.md','locator':'L1','quote':'具体内容'}]} for a in atoms]
    rubric = SimpleNamespace(atoms=atoms, errors=[], text='固定标准', by_metric=lambda m:[a for a in atoms if a.metric==m])
    return SimpleNamespace(id='demo',rubric=rubric), records


class ContractReviewTests(TestCase):
    def test_local_patch_preserves_other_corrections_and_ledger_then_revalidates_all(self):
        task,records=fixture()
        kept={**records[1],'state':'0','observation':'该要求的具体内容缺失，已核对交付对应位置。'}
        broken={**records[2],'reason':'理由不足'}
        candidate={'checks':{k:'独立核对了全部原始证据及固定规则' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},
                   'defects':[{'id':'D1','description':'交付对应位置缺少该项具体内容',
                       'evidence':kept['evidence'],'owners':[{'atom_id':kept['id'],'metric':kept['metric'],'consequence':'该项内容缺失，按固定规则记零'}]}],
                   'corrections':[kept,broken]}
        repair={'review_patch':{'corrections':[{**broken,'reason':records[2]['reason']}]}}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir()
            with patch('evaluation_judger.contract_review.run',side_effect=[
                    ('BEGIN_JUDGMENT '+json.dumps(candidate)+' END_JUDGMENT',True),
                    ('BEGIN_JUDGMENT '+json.dumps(repair)+' END_JUDGMENT',True)]) as model:
                final,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(review['status'],'passed')
            self.assertEqual(final[1]['state'],'0')
            self.assertEqual(review['review']['defects'],candidate['defects'])
            self.assertIn('reason has 4 characters, minimum 20',model.call_args_list[1].args[1])
            self.assertIn('review_patch',model.call_args_list[1].args[1])
            files=sorted(root.glob('contract-review-candidate-*.json'))
            self.assertEqual(json.loads(files[0].read_text(encoding='utf-8')),candidate)
            self.assertTrue(list((root/'events').glob('*-patch.json')))
            with patch('evaluation_judger.contract_review.run') as model:
                cached=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            model.assert_not_called();self.assertEqual(cached[0],final)

    def test_patch_never_bypasses_original_gates_or_infers_a_base(self):
        task,records=fixture()
        draft={'checks':{k:'已核对原始内容与规则依据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        with self.assertRaisesRegex(ValueError,'saved candidate'):
            _merge_review_patch({'review_patch':{}},None)
        with self.assertRaisesRegex(ValueError,'distinct atom IDs'):
            _merge_review_patch({'review_patch':{'corrections':[records[0],records[0]]}},draft)
        bad={'review_patch':{'corrections':[{**records[0],'reason':'过短'}]}}
        with self.assertRaisesRegex(ValueError,'too vague'):
            validate_review(_merge_review_patch(bad,draft),task,records,{'delivery.md'})
        unknown={'review_patch':{'corrections':[{**records[0],'id':'UNKNOWN'}]}}
        with self.assertRaisesRegex(ValueError,'Unknown or duplicate'):
            validate_review(_merge_review_patch(unknown,draft),task,records,{'delivery.md'})
        # Complete reviews retain their previous replacement semantics.
        self.assertIs(_merge_review_patch(draft,{'corrections':[records[0]]}),draft)

    def test_patch_cannot_hide_unrepaired_count_failure(self):
        task,records=fixture()
        task.rubric.atoms[1].kind='RATIO'
        counted={**records[1],'kind':'RATIO','numerator':4,'denominator':4,'items':[{'claim':'only one example'}]}
        draft={'checks':{k:'已核对原始内容与规则依据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[counted]}
        merged=_merge_review_patch({'review_patch':{'checks':{'ownership':'保留全部原子，具体缺陷归属已核对'}}},draft)
        with self.assertRaisesRegex(ValueError,'denominator 4 has 1'):
            validate_review(merged,task,records,{'delivery.md'})
        counted['items']=[{'method':'material_inspector operation=xml','scope':'verified range','location':'delivery.md','count':4}]
        self.assertTrue(count_issues(counted))
        counted['items'][0]['method']='material_inspector'
        self.assertEqual(count_issues(counted),[])

    def test_latest_saved_feedback_survives_scanning_older_events(self):
        import hashlib
        task,records=fixture()
        payload={'checks':{k:'独立核查全部原始材料与规则' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        digest=hashlib.sha256(json.dumps({'rubric':task.rubric.text,'atoms':records},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir();(root/'events').mkdir()
            (root/'checkpoints'/f'contract-review-{digest[:12]}.json').write_text(json.dumps({'input_hash':digest,'status':'incomplete','error':'LATEST SPECIFIC DIAGNOSTIC'}))
            (root/'events'/f'contract-{digest[:12]}-0.jsonl').write_text(json.dumps({'sessionID':'old','type':'text','part':{'text':'BEGIN_JUDGMENT {} END_JUDGMENT'}}))
            with patch('evaluation_judger.contract_review.run',return_value=('BEGIN_JUDGMENT '+json.dumps(payload)+' END_JUDGMENT',True)) as model:
                _,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(review['status'],'passed')
            self.assertTrue(model.call_args.args[1].endswith('Specific validation feedback: LATEST SPECIFIC DIAGNOSTIC'))

    def test_html_preserves_link_targets_and_empty_reference_values(self):
        with TemporaryDirectory() as folder:
            p=Path(folder)/'report.html'
            p.write_text('<p>正文</p><a href="https://example.org/a">来源</a>'
                '<script id="references-data">[{"title":"A","published_at":"2020-01-01"},'
                '{"title":"B","published_at":""}]</script><script>throw new Error("do not run")</script>',encoding='utf-8')
            raw=p.read_bytes()
            data=structured_evidence(p)
            self.assertEqual(data['links'][0]['target'],'https://example.org/a')
            refs=data['structured_data'][0]['data']
            self.assertEqual(sum(bool(r['published_at']) for r in refs),1)
            text,_=extract(p,None)
            self.assertIn('https://example.org/a',text)
            self.assertIn('"published_at": ""',text)
            self.assertNotIn('throw new Error',text)
            self.assertEqual(p.read_bytes(),raw)

    def test_pdf_link_annotations_exist_without_visible_url_text(self):
        with TemporaryDirectory() as folder:
            p=Path(folder)/'report.pdf'
            writer=PdfWriter();writer.add_blank_page(width=200,height=200)
            writer.add_annotation(page_number=0, annotation=Link(rect=(10,10,100,30),url='https://example.org/reference'))
            with p.open('wb') as f:writer.write(f)
            data=structured_evidence(p)
            self.assertEqual(data['links'][0]['target'],'https://example.org/reference')
            self.assertIn('page 1',data['links'][0]['locator'])
            self.assertIn('https://example.org/reference',extract(p,None)[0])

    def test_invalid_json_is_an_inspection_limitation_not_negative_evidence(self):
        with TemporaryDirectory() as folder:
            p=Path(folder)/'page.html';p.write_text('<script id="references-data">broken</script>')
            result=structured_evidence(p)
            self.assertTrue(result['limitations'])
            self.assertEqual(result['structured_data'],[])

    def test_group_template_does_not_account_for_remaining_elements(self):
        record={'id':'F01','kind':'RATIO','denominator':4,'items':[{'claim':'first'}, {'claim':'all others, same template'}]}
        self.assertTrue(count_issues(record))
        record['items'][1]['members']=['2','3','4']
        self.assertEqual(count_issues(record),[])
        target_count={'id':'C01','kind':'RATIO','rule':'COUNT: min(n/3,1)',
                      'denominator':3,'items':[{'claim':'observed total exceeds target'}]}
        self.assertEqual(count_issues(target_count),[])
        record['denominator']=1000
        record['items']=[{'count':1000,'method':'material_inspector','scope':'Sheet1!A2:A1001','location':'source.xlsx / exact count'}]
        self.assertEqual(count_issues(record),[])

    def test_one_defect_cannot_have_two_primary_owners_in_one_metric(self):
        task,records=fixture()
        payload={'checks':{k:'已核对相关证据及范围' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},
                 'defects':[{'id':'D1','description':'同一事实在两处出现相反表述',
                             'evidence':[{'file':'delivery.md','locator':'L1','quote':'具体内容'}],
                             'owners':[{'atom_id':'X1','metric':'coverage','consequence':'内容影响'},
                                       {'atom_id':'X1','metric':'coverage','consequence':'重复影响'}]}], 'corrections':[]}
        with self.assertRaisesRegex(ValueError,'only one primary owner'):
            validate_review(payload,task,records,{'delivery.md'})
        payload['defects'][0]['owners'].pop()
        final,_=validate_review(payload,task,records,{'delivery.md'})
        self.assertEqual(final,records)

    def test_bounded_failure_preserves_records_and_does_not_cache_success(self):
        task,records=fixture()
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir()
            with patch('evaluation_judger.contract_review.run',return_value=('{}',False)) as model:
                final,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(model.call_count,2)
            self.assertEqual(final,records)
            self.assertEqual(review['status'],'incomplete')
            self.assertIn('Specific validation feedback',model.call_args_list[1].args[1])

    def test_successful_review_checkpoint_resumes_without_model_call(self):
        task,records=fixture()
        payload={'checks':{k:'已核对相关范围和证据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir()
            with patch('evaluation_judger.contract_review.run',return_value=('BEGIN_JUDGMENT '+json.dumps(payload)+' END_JUDGMENT',True)) as model:
                first=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
                second=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(model.call_count,1)
            self.assertEqual(first,second)

    def test_rejected_candidate_is_preserved_and_given_to_bounded_repair(self):
        task,records=fixture()
        valid={'checks':{k:'已核查原始材料与判断依据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        invalid={**valid,'defects':[{'id':'D1','description':'引用了此前判分而不是交付事实',
            'evidence':[{'file':'contract-review-input.json','locator':'X1','quote':'先前判断'}],
            'owners':[{'atom_id':'X1','metric':'coverage','consequence':'假定内容缺失'}]}]}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir()
            with patch('evaluation_judger.contract_review.run',side_effect=[
                ('BEGIN_JUDGMENT '+json.dumps(invalid)+' END_JUDGMENT',True),
                ('BEGIN_JUDGMENT '+json.dumps(valid)+' END_JUDGMENT',True)]) as model:
                final,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(final,records)
            self.assertEqual(review['status'],'passed')
            self.assertIn('contract-review-candidate-',model.call_args_list[1].args[1])
            self.assertIn('Unknown or ambiguous evidence file',model.call_args_list[1].args[1])
            candidates=sorted(root.glob('contract-review-candidate-*.json'))
            self.assertEqual(json.loads(candidates[0].read_text(encoding='utf-8')),invalid)

    def test_incomplete_review_resumes_from_rejected_candidate_and_feedback(self):
        task,records=fixture()
        valid={'checks':{k:'核对材料后结果成立' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'checkpoints').mkdir()
            with patch('evaluation_judger.contract_review.run',return_value=('{}',True)):
                _,failed=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(failed['status'],'incomplete')
            old_candidates={p.name:p.read_bytes() for p in root.glob('contract-review-candidate-*.json')}
            with patch('evaluation_judger.contract_review.run',return_value=('BEGIN_JUDGMENT '+json.dumps(valid)+' END_JUDGMENT',True)) as model:
                _,passed=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(passed['status'],'passed')
            self.assertIn('contract-review-candidate-',model.call_args.args[1])
            self.assertIn(failed['error'],model.call_args.args[1])
            self.assertTrue(all((root/name).read_bytes()==content for name,content in old_candidates.items()))

    def test_same_input_interrupted_session_resumes_read_evidence_but_changed_input_does_not(self):
        import hashlib
        task,records=fixture()
        payload={'checks':{k:'逐项核查了固定规则与原始交付证据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        for changed in (False,True):
            with self.subTest(changed=changed),TemporaryDirectory() as folder:
                root=Path(folder);(root/'events').mkdir()
                digest=hashlib.sha256(json.dumps({'rubric':task.rubric.text,'atoms':records},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                read={'sessionID':'read-session','type':'tool_use','part':{'tool':'read','state':{'status':'completed'}}}
                (root/'events'/f'contract-{digest[:12]}-0.jsonl').write_text(json.dumps(read))
                current=[{**records[0],'observation':'新的原始交付观察发生了变化'},*records[1:]] if changed else records
                with patch('evaluation_judger.contract_review.run',return_value=('BEGIN_JUDGMENT '+json.dumps(payload)+' END_JUDGMENT',changed)) as model:
                    final,review=review_contract(task,current,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
                self.assertEqual(review['status'],'passed')
                self.assertEqual(model.call_args.kwargs['session_id'],None if changed else 'read-session')
                if not changed:self.assertIn('SAME INPUT session',model.call_args.args[1])

    def test_completed_same_input_event_is_recovered_without_new_model_call(self):
        import hashlib
        task,records=fixture();payload={'checks':{k:'逐项核对相关范围和原始证据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'events').mkdir()
            digest=hashlib.sha256(json.dumps({'rubric':task.rubric.text,'atoms':records},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            lines=[{'sessionID':'saved','type':'tool_use','part':{'tool':'read','state':{'status':'completed'}}},
                   {'sessionID':'saved','type':'text','part':{'text':'BEGIN_JUDGMENT '+json.dumps(payload)+' END_JUDGMENT'}}]
            (root/'events'/f'contract-{digest[:12]}-0.jsonl').write_text('\n'.join(map(json.dumps,lines)))
            with patch('evaluation_judger.contract_review.run') as model:
                final,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            model.assert_not_called();self.assertEqual(review['status'],'passed');self.assertIn('recovered_from_events',review)

    def test_unread_newer_session_cannot_inherit_read_flag_from_another_session(self):
        import hashlib,os
        task,records=fixture();payload={'checks':{k:'逐项核对相关范围和原始证据' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[]}
        with TemporaryDirectory() as folder:
            root=Path(folder);(root/'events').mkdir()
            digest=hashlib.sha256(json.dumps({'rubric':task.rubric.text,'atoms':records},ensure_ascii=False,sort_keys=True).encode()).hexdigest()
            older=root/'events'/f'contract-{digest[:12]}-0.jsonl';newer=root/'events'/f'contract-{digest[:12]}-1.jsonl'
            older.write_text(json.dumps({'sessionID':'old','type':'tool_use','part':{'tool':'read','state':{'status':'completed'}}}));os.utime(older,(10,10))
            newer.write_text(json.dumps({'sessionID':'new','type':'step_start'}));os.utime(newer,(20,20))
            with patch('evaluation_judger.contract_review.run',return_value=('BEGIN_JUDGMENT '+json.dumps(payload)+' END_JUDGMENT',False)):
                final,review=review_contract(task,records,workspace=root,project=root,known_files={'delivery.md'},model='mock',major=1)
            self.assertEqual(review['status'],'incomplete')
