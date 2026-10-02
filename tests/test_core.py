import copy
import tempfile
import unittest
from pathlib import Path
from token_budget_mcp.core import BudgetService, DemoProvider, cost

class CountingProvider(DemoProvider):
    def __init__(self): self.calls = 0
    def generate(self,*args):
        self.calls += 1
        return super().generate(*args)

class CoreTests(unittest.TestCase):
    def setUp(self):
        self.provider = CountingProvider()
        self.service = BudgetService(':memory:',self.provider)
        self.messages = [{'role':'user','content':'Extract the city Singapore.'}]

    def preview(self, **kwargs): return self.service.preview(self.messages,**kwargs)

    def tearDown(self): self.service.close()

    def test_cached_input_and_reasoning_not_double_counted(self):
        rate = {'input':'2','cached_input':'0.5','output':'8'}
        self.assertEqual(cost(rate,1000,100,400),'0.00220000')

    def test_explicit_selection_and_duplicate_calls(self):
        p = self.preview(task_type='extraction')
        self.assertEqual(p['recommendation']['model'],'gpt-4.1-mini')
        with self.assertRaises(ValueError): self.service.execute(p['preview_id'],'gpt-4.1-mini')
        r = self.service.execute(p['preview_id'],'gpt-4.1-mini',True)
        self.assertEqual(self.service.execute(p['preview_id'],'gpt-4.1-mini',True),r)
        self.assertEqual(self.provider.calls,1)
        self.assertEqual(r['usage_basis'],'simulated')

    def test_snapshot_not_mutable_and_prices_frozen(self):
        p = self.preview()
        self.messages[0]['content']='changed'
        self.service.catalog['models']['gpt-4.1']['input']='999'
        record=self.service.get(p['preview_id'])
        self.assertEqual(record['messages'][0]['content'],'Extract the city Singapore.')
        self.assertEqual(record['candidates']['gpt-4.1']['pricing']['input'],'2.00')

    def test_no_downgrade_for_complex_task(self):
        self.assertEqual(self.preview(task_type='complex_reasoning')['recommendation']['model'],'gpt-4.1')

    def test_validation(self):
        for kwargs in ({'output_min':0},{'output_max':40000},{'selected_model':'unknown'},{'task_type':'unknown'}):
            with self.assertRaises(ValueError): self.preview(**kwargs)
        with self.assertRaises(ValueError): self.service.preview([{'role':'user','content':[]}])

    def test_missing_usage_is_unavailable(self):
        self.provider.generate=lambda *args: {'answer':'done','model':'gpt-4.1','status':'completed','usage':None}
        p=self.preview()
        r=self.service.execute(p['preview_id'],'gpt-4.1',True)
        self.assertIsNone(r['calculated_cost_usd'])
        self.assertEqual(r['estimate_comparison'],'unavailable')

    def test_failure_is_recorded_without_retry_or_sensitive_error(self):
        def fail(*args): raise RuntimeError('secret-example')
        self.provider.generate=fail
        p=self.preview()
        r=self.service.execute(p['preview_id'],'gpt-4.1',True)
        self.assertEqual(r['status'],'failed_or_unknown')
        self.assertNotIn('secret-example',str(r))
        self.assertEqual(self.service.execute(p['preview_id'],'gpt-4.1',True),r)

    def test_restart_retains_report(self):
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'records.sqlite3'
            a=BudgetService(database,self.provider)
            p=a.preview(self.messages)
            r=a.execute(p['preview_id'],'gpt-4.1',True)
            b=BudgetService(database,self.provider)
            self.assertEqual(b.get(r['request_id']),r)
            self.assertEqual(b.execute(p['preview_id'],'gpt-4.1',True),r)
            a.close()
            b.close()

    def test_expired_or_mode_changed_preview(self):
        p=self.preview()
        record=self.service.get(p['preview_id'])
        record['created_at']=0
        self.service.put(p['preview_id'],record)
        with self.assertRaises(ValueError): self.service.execute(p['preview_id'],'gpt-4.1',True)

if __name__=='__main__': unittest.main()
