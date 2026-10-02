"""Read-only plugin behavior uses synthetic collector reports."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from token_budget_mcp.usage_server import UsageReader, build_server, dashboard_url, validate_dashboard_url


class UsageServerTests(unittest.TestCase):
    def setUp(self):
        self.report = {'chats':[
            {'thread_id':'a & b','chat_name':'Example chat','model':'model','recorded_turns':120,
             'last_activity_at':'2026-10-03T00:00:00Z','credit_coverage_complete':False,'usage':{'output_tokens':10}},
            {'thread_id':'other','chat_name':'Other chat','model':'model','recorded_turns':1},
            {'thread_id':'internal','chat_name':'Internal','is_internal':True}],
            'turns':[{'thread_id':'a & b','turn_id':'turn','preview':{'private':'excluded'},'usage_coverage_complete':False}],
            'coverage':'Local only','updated_at':'now','account_usage':{'stale':True,'limits':[]}}
        self.urls = []
        def fetch(url):
            self.urls.append(url)
            return copy.deepcopy(self.report)
        self.reader = UsageReader('http://127.0.0.1:8767/',fetch)

    def test_chat_query_is_bounded_and_internal_chats_excluded(self):
        result = self.reader.list_chats('example',1)
        self.assertEqual(result['matching_chats'],1)
        self.assertEqual(result['chats'][0]['thread_id'],'a & b')
        self.assertFalse(result['chats'][0]['credit_coverage_complete'])
        self.assertEqual(len(self.reader.list_chats()['chats']),2)
        result = self.reader.list_chats(limit=1)
        self.assertTrue(result['truncated'])

    def test_exact_chat_filter_preserves_partial_coverage_and_excludes_previews(self):
        result = self.reader.chat_usage('a & b',1)
        self.assertIn('thread_id=a+%26+b',self.urls[-1])
        self.assertTrue(result['history_truncated'])
        self.assertFalse(result['recent_turns'][0]['usage_coverage_complete'])
        self.assertNotIn('preview',result['recent_turns'][0])
        with self.assertRaises(ValueError): self.reader.chat_usage('unknown')
        with self.assertRaises(ValueError): self.reader.chat_usage('internal')

    def test_comparison_preserves_requested_order_and_rejects_unknown_ids(self):
        self.assertEqual([c['thread_id'] for c in self.reader.compare(['other','a & b'])['chats']],['other','a & b'])
        for ids in (['other'],['other','other'],['other','missing'],[1,2]):
            with self.assertRaises(ValueError): self.reader.compare(ids)

    def test_bad_limits_rejected_before_fetching(self):
        for limit in (0,101,True,'10'):
            with self.assertRaises(ValueError): self.reader.list_chats(limit=limit)
        self.assertEqual(self.urls,[])

    def test_only_loopback_urls_allowed_and_custom_port_is_discovered(self):
        for url in ('https://127.0.0.1/','http://example.com/','http://user:pass@localhost/','http://localhost/api/','http://localhost/?query=1'):
            with self.assertRaises(ValueError): validate_dashboard_url(url)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'installation.json').write_text(json.dumps({'port':9234}))
            self.assertEqual(dashboard_url(root),'http://127.0.0.1:9234/')

    def test_failure_does_not_expose_exception_or_prompt_text(self):
        def fail(url): raise OSError('private-detail')
        with self.assertRaises(ValueError) as error: UsageReader(self.reader.url,fail).list_chats()
        self.assertNotIn('private-detail',str(error.exception))


if __name__ == '__main__': unittest.main()
