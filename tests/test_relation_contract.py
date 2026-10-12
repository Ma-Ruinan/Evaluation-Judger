import copy
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest

project=Path(__file__).resolve().parents[1];sys.path.insert(0,str(project))
from evaluation_judger.rubric import METRICS
from evaluation_judger import contract_review as module

class RelationContractTests(unittest.TestCase):
 def setUp(self):
  atoms=[SimpleNamespace(id=f'X{i}',metric=m,weight=Decimal(100),kind='BIN',rule='满足具体证据条件为1，否则为0',purpose='核验内容') for i,m in enumerate(METRICS)]
  self.records=[{'id':a.id,'metric':a.metric,'weight':'100','kind':'BIN','state':'1',
    'observation':'原交付内容已定位','reason':'已核验原交付的具体内容与规则。',
    'evidence':[{'file':'delivery.md','locator':'L1','quote':'具体内容'}]} for a in atoms]
  rubric=SimpleNamespace(atoms=atoms,errors=[],text='固定规则',by_metric=lambda m:[a for a in atoms if a.metric==m])
  self.task=SimpleNamespace(id='demo',rubric=rubric)
  self.inventory={'comparisons':[{'id':'R1','kind':'asserted','numeric_relation_holds':False}]}
  self.payload={'checks':{k:'原材料与评分方法已逐项核查' for k in ('file_coverage','item_accounting','ownership','internal_consistency')},'defects':[],'corrections':[],'relation_findings':[]}
 def check(self,payload=None,inventory=None):
  return module.validate_review(payload or self.payload,self.task,self.records,{'delivery.md'},relation_inventory=self.inventory if inventory is None else inventory)
 def test_required_witness_cannot_be_silently_omitted(self):
  with self.assertRaisesRegex(ValueError,'omitted relation witnesses'):self.check()
 def test_exclusion_needs_specific_rubric_boundary(self):
  p=copy.deepcopy(self.payload);p['relation_findings']=[{'id':'R1','decision':'excluded','rule_id':'X5','reason':'引用处明确使用不同期间，不能作为同期间内部矛盾。'}]
  self.assertEqual(self.check(p)[0],self.records)
  p['relation_findings'][0]['rule_id']='unknown'
  with self.assertRaisesRegex(ValueError,'governing atom'):self.check(p)
 def test_confirmed_defect_links_actual_negative_owner(self):
  p=copy.deepcopy(self.payload);p['relation_findings']=[{'id':'R1','decision':'confirmed_defect','defect_id':'D1','reason':'相同期间同一指标两个比较方向不能同时成立。'}]
  p['defects']=[{'id':'D1','description':'同一指标在两处使用相反关系方向',
    'evidence':[{'file':'delivery.md','locator':'L1','quote':'具体内容'}],
    'owners':[{'atom_id':'X5','metric':'hallucination','consequence':'内部关系矛盾，按该项BIN规则记0'}]}]
  with self.assertRaisesRegex(ValueError,'non-full owner'):self.check(p)
  p['corrections']=[{**self.records[-1],'state':'0','reason':'两处明确的关系方向相反，满足内部矛盾失分条件。'}]
  self.assertEqual(self.check(p)[0][-1]['state'],'0')
 def test_conditional_failure_and_empty_inventory_need_no_scored_defect(self):
  self.assertEqual(self.check(inventory={'comparisons':[]})[0],self.records)
  self.assertEqual(self.check(inventory={'comparisons':[{'id':'R1','kind':'conditional','numeric_relation_holds':False}]})[0],self.records)
 def test_optional_explanation_of_supported_relation_is_accepted(self):
  p=copy.deepcopy(self.payload);p['relation_findings']=[{'id':'R2','decision':'excluded','rule_id':'X5','reason':'数值关系成立，不构成内部矛盾。'}]
  inv={'comparisons':[{'id':'R2','kind':'asserted','numeric_relation_holds':True}]}
  self.assertEqual(self.check(p,inv)[0],self.records)
 def test_unknown_or_duplicate_finding_rejected(self):
  p=copy.deepcopy(self.payload);p['relation_findings']=[{'id':'R2','decision':'excluded','rule_id':'X5','reason':'不同口径'}]
  with self.assertRaisesRegex(ValueError,'once'):self.check(p)
  p['relation_findings'][0]['id']='R1';p['relation_findings']*=2
  with self.assertRaisesRegex(ValueError,'once'):self.check(p)

if __name__=='__main__':unittest.main()
