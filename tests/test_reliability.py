"""Regression coverage for damaged logs, growing history and replayed records."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from token_budget_mcp.monitor import Monitor


class ReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / 'monitor.sqlite3'
        self.monitor = Monitor(self.database, self.root)
        self.state = {'thread': 'chat'}

    def tearDown(self):
        self.monitor.close()
        self.temp.cleanup()

    def record(self, response='good', **usage):
        counts = {'input_tokens': 100, 'cached_input_tokens': 80, 'output_tokens': 10}
        counts.update(usage)
        return {'type': 'token_usage_record', 'timestamp': '2026-10-02T13:00:00Z', 'payload': {
            'turn_id': 'turn', 'response_id': response, 'model': 'gpt-6.1-sol', 'usage': counts}}

    def completed(self):
        self.monitor.turn('turn', 'chat', started='2026-10-02T13:00:00Z', status='completed')
        self.monitor.accept(self.record(), self.state)

    def test_bad_optional_counts_are_unknown_and_valid_calls_survive(self):
        for bad in ('80', True, -1, [], {}, 101):
            with self.subTest(value=bad):
                self.monitor.accept(self.record(str(bad), cached_input_tokens=bad), self.state)
        self.completed()
        result = self.monitor.reports()
        self.assertEqual(result['turns'][0]['usage']['input_tokens'], 700)
        self.assertIsNone(result['turns'][0]['usage']['new_input_tokens'])
        self.assertFalse(result['chats'][0]['credit_coverage_complete'])
        self.assertEqual(result['chats'][0]['known_estimated_credits'], '0.00370000')

    def test_legacy_damaged_calls_do_not_poison_report(self):
        self.completed()
        for identifier, raw in [('bad-json','{'), ('bad-count',json.dumps({'input_tokens': '100', 'output_tokens': 10})),
                                ('bad-cache', json.dumps({'input_tokens':100,'output_tokens':10,'cached_input_tokens':'80'}))]:
            self.monitor.db.execute('INSERT INTO calls VALUES (?,?,?,?,?)', (identifier,'turn','gpt-6.1-sol',raw,None))
        chat = self.monitor.reports()['chats'][0]
        self.assertEqual(chat['usage']['input_tokens'], 200)
        self.assertFalse(chat['credit_coverage_complete'])
        self.assertEqual(chat['unknown_credit_calls'], 3)

    def test_rejected_record_marks_root_turn_partial_after_restart(self):
        self.completed()
        bad = self.record('bad', input_tokens='100')
        bad['payload'].update(turn_id='child', root_turn_id='turn')
        self.monitor.accept(bad, {'thread':'child-chat'})
        self.monitor.db.commit()
        self.monitor.close()
        self.monitor = Monitor(self.database, self.root)
        result = self.monitor.reports()
        self.assertFalse(result['turns'][0]['usage_coverage_complete'])
        self.assertIsNone(result['turns'][0]['estimated_credits'])
        self.assertFalse(result['chats'][0]['credit_coverage_complete'])
        self.assertEqual(result['chats'][0]['unsupported_usage_turns'], 1)

    def test_malformed_metadata_and_json_do_not_stop_scanning(self):
        self.monitor.turn('turn', 'chat', status='completed')
        rows = [json.dumps({'type':'turn_context','payload':{'turn_id':'turn'}}), '{',
                json.dumps({'type':'turn_context','payload':{'turn_id':[], 'model':{}}}), json.dumps(self.record())]
        (self.root / 'rollout-test.jsonl').write_text('\n'.join(rows) + '\n')
        self.monitor.scan()
        result = self.monitor.reports()
        self.assertEqual(result['turns'][0]['model_calls'], 1)
        self.assertFalse(result['turns'][0]['usage_coverage_complete'])

    def test_upgrade_marks_legacy_file_diagnostics_conservatively(self):
        self.completed()
        self.monitor.db.execute('DROP TABLE turn_issues')
        self.monitor.db.execute('INSERT INTO files VALUES (?,?,?,?)', ('old-log', 1, 1, json.dumps({'thread':'chat','turn':'turn'})))
        self.monitor.db.execute('INSERT INTO diagnostics VALUES (?,?)', ('old-log', 'Unsupported old record'))
        self.monitor.db.commit(); self.monitor.close()
        self.monitor = Monitor(self.database, self.root)
        self.assertFalse(self.monitor.reports()['chats'][0]['credit_coverage_complete'])
        self.assertEqual(self.monitor.reports()['chats'][0]['known_estimated_credits'], '0.00370000')

    def test_replayed_start_and_submission_keep_terminal_status_and_credits(self):
        self.completed()
        self.monitor.accept({'type':'event_msg', 'payload':{'type':'task_started','turn_id':'turn'}}, self.state)
        self.monitor.submission({'turn_id':'turn','session_id':'chat','prompt':'synthetic'})
        result = self.monitor.reports()['chats'][0]
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['estimated_credits'], '0.00370000')
        self.monitor.turn('interrupted', 'chat', status='interrupted')
        self.monitor.turn('interrupted', status='running')
        self.assertEqual(self.monitor.db.execute("SELECT status FROM turns WHERE id='interrupted'").fetchone()[0], 'interrupted')

    def test_summaries_cached_but_new_calls_and_rate_changes_refresh(self):
        self.completed()
        from token_budget_mcp.usage import summarize_calls
        with patch('token_budget_mcp.monitor.summarize_calls', wraps=summarize_calls) as summarize:
            self.monitor.reports(); self.monitor.reports()
            self.assertEqual(summarize.call_count, 1)
            self.monitor.accept(self.record('new'), self.state)
            self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'], '0.00740000')
            self.assertEqual(summarize.call_count, 2)
            self.monitor.credit_rates['models']['gpt-6.1-sol']['output'] = '500'
            self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'], '0.01240000')
            self.assertEqual(summarize.call_count, 3)

    def test_sql_query_count_does_not_grow_per_turn_and_index_exists(self):
        for identifier in range(200):
            self.monitor.turn(str(identifier), 'chat', status='completed')
        self.monitor.db.commit()
        statements = []
        self.monitor.db.set_trace_callback(statements.append)
        self.assertEqual(self.monitor.reports()['chats'][0]['recorded_turns'], 200)
        reads = [s for s in statements if s.startswith('SELECT')]
        self.assertLess(len(reads), 12)
        plan = self.monitor.db.execute("EXPLAIN QUERY PLAN SELECT usage FROM calls WHERE turn_id='turn'").fetchall()
        self.assertTrue(any('USING INDEX' in row[-1] for row in plan))


if __name__ == '__main__': unittest.main()
