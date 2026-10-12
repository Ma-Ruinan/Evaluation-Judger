import copy
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

project=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(project))
from evaluation_judger import relation_inventory as module


class RelationInventoryTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
  self.workspace=Path(self.temp.name)
  self.source='A年营收135亿元，B预期167.05亿元。A高于B。A低于B。范围40-65。'
  for name in ['deliveries/a.txt','extracted/a.txt']:
   p=self.workspace/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(self.source,encoding='utf-8')
  self.manifest={'files':[{'category':'deliveries','file':'deliveries/a.txt','extracted':'extracted/a.txt'}]}
  (self.workspace/'manifest.json').write_text(json.dumps(self.manifest),encoding='utf-8')
  self.known={'deliveries/a.txt','extracted/a.txt','manifest.json'}
  self.payload={'coverage':'读取全部正文及图表描述','unresolved':[], 'comparisons':[{
   'id':'R001','subject':'A年营收与B预期','scope':'同一年，亿元',
   'left':{'value':'135','unit':'亿元','meaning':'A年营收','evidence':[self.evidence('A年营收135亿元')]},
   'right':{'value':'167.05','unit':'亿元','meaning':'B预期','evidence':[self.evidence('B预期167.05亿元')]},
   'operator':'gt','kind':'asserted','assertion':self.evidence('A高于B。'),'context':'同年同单位'}]}
 def evidence(self,quote): return {'file':'deliveries/a.txt','locator':'L1','quote':quote}
 def check(self,payload=None): return module.validate_inventory(payload or self.payload,self.workspace,self.known,self.manifest)
 def test_false_relation_is_witness_not_score(self):
  r=self.check();self.assertFalse(r['comparisons'][0]['numeric_relation_holds']);self.assertNotIn('scores',r)
 def test_valid_direction(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0].update(operator='lt',assertion=self.evidence('A低于B。'))
  self.assertTrue(self.check(p)['comparisons'][0]['numeric_relation_holds'])
 def test_array_commas_and_thousands_separators(self):
  self.assertEqual(module._quoted_numbers('data:[80.80,9.83]，价格1,919.50'),[module.Decimal('80.80'),module.Decimal('9.83'),module.Decimal('1919.50')])
 def test_declared_rounding_preserves_witness_and_notes(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0]['right']['value']='167'
  r=self.check(p)['comparisons'][0]
  self.assertFalse(r['numeric_relation_holds']);self.assertTrue(r['binding_notes'])
 def test_range_and_threshold_are_not_fabricated_scalars(self):
  for value in ['>135','40-65',float('nan'),True]:
   p=copy.deepcopy(self.payload);p['comparisons'][0]['left']['value']=value
   self.assertIsNone(self.check(p)['comparisons'][0]['numeric_relation_holds'])
 def test_different_units_do_not_automatically_fail_comparison(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0]['right']['unit']='百万元'
  self.assertIsNone(self.check(p)['comparisons'][0]['numeric_relation_holds'])
 def test_invented_quote_rejected(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0]['assertion']['quote']='不存在的关系断言'
  with self.assertRaisesRegex(ValueError,'Quote not found'):self.check(p)
 def test_operand_must_appear_in_own_quote(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0]['left']['value']='136'
  with self.assertRaisesRegex(ValueError,'occur in its own'):self.check(p)
 def test_unauthorized_path_rejected(self):
  p=copy.deepcopy(self.payload);p['comparisons'][0]['assertion']['file']='../other-subject.txt'
  with self.assertRaisesRegex(ValueError,'authorized'):self.check(p)
 def test_empty_inventory_allowed_and_duplicate_ids_rejected(self):
  p=copy.deepcopy(self.payload);p['comparisons']=[];self.assertEqual(self.check(p)['comparisons'],[])
  p=copy.deepcopy(self.payload);p['comparisons']*=2
  with self.assertRaisesRegex(ValueError,'distinct'):self.check(p)
 def test_bad_binding_does_not_discard_valid_relations(self):
  p=copy.deepcopy(self.payload);bad=copy.deepcopy(p['comparisons'][0]);bad['id']='R002';bad['left']['value']='136';p['comparisons'].append(bad)
  r=module.validate_inventory(p,self.workspace,self.known,self.manifest,tolerate_partial=True)
  self.assertEqual(len(r['comparisons']),1);self.assertFalse(r['comparisons'][0]['numeric_relation_holds'])
  self.assertEqual(len(r['validation_warnings']),1);self.assertEqual(r['unresolved'][0]['id'],'R002')
 def test_read_required_then_retry_and_valid_cache_reuse(self):
  args=dict(workspace=self.workspace,project=project,known_files=self.known,model='test',major=1,timeout=20)
  task=SimpleNamespace(id='1.1')
  with patch.object(module,'run',side_effect=[(json.dumps(self.payload),False),(json.dumps(self.payload),True)]) as invoke:
   first=module.inventory_relations(task,**args);self.assertEqual(invoke.call_count,2)
   second=module.inventory_relations(task,**args);self.assertEqual(invoke.call_count,2);self.assertEqual(first['input_hash'],second['input_hash'])
  self.assertEqual(len(list((self.workspace/'checkpoints').glob('relation-attempt-*.json'))),1)
 def test_invalid_candidate_saved_and_cross_run_resumes(self):
  args=dict(workspace=self.workspace,project=project,known_files=self.known,model='test',major=1,timeout=20)
  task=SimpleNamespace(id='1.1');bad=copy.deepcopy(self.payload);bad['comparisons'][0]['assertion']['quote']='不存在'
  with patch.object(module,'run',return_value=(json.dumps(bad),True)):
   with self.assertRaises(module.JudgeError): module.inventory_relations(task,**args)
  with patch.object(module,'run',return_value=(json.dumps(self.payload),True)) as invoke:
   fixed=module.inventory_relations(task,**args);self.assertEqual(fixed['status'],'passed')
   self.assertIn('rejected candidate',invoke.call_args.args[1])
  self.assertEqual(len(list(self.workspace.glob('relation-candidate-*.json'))),3)
 def test_cached_derived_findings_are_revalidated_and_written_consistently(self):
  args=dict(workspace=self.workspace,project=project,known_files=self.known,model='test',major=1,timeout=20)
  task=SimpleNamespace(id='demo')
  with patch.object(module,'run',return_value=(json.dumps(self.payload),True)) as invoke:
   first=module.inventory_relations(task,**args)
   checkpoint=next((self.workspace/'checkpoints').glob('relation-inventory-*.json'))
   stale=copy.deepcopy(first);stale['comparisons'][0]['numeric_relation_holds']=True
   checkpoint.write_text(json.dumps(stale),encoding='utf-8');(self.workspace/'relation-inventory.json').write_text(json.dumps(stale),encoding='utf-8')
   result=module.inventory_relations(task,**args)
   self.assertFalse(result['comparisons'][0]['numeric_relation_holds']);self.assertEqual(invoke.call_count,1)
   self.assertFalse(json.loads((self.workspace/'relation-inventory.json').read_text(encoding='utf-8'))['comparisons'][0]['numeric_relation_holds'])

if __name__=='__main__':unittest.main()
