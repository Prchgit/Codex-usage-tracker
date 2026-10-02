from contextlib import closing
import json
import tempfile
import sqlite3
import unittest
from pathlib import Path
from datetime import datetime, timezone
from token_budget_mcp.monitor import Monitor, floating_sessions, session_display_name

class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.root=Path(self.tmp.name)
        self.log=self.root/'rollout-test.jsonl'
        self.monitor=Monitor(self.root/'test.sqlite3',self.root)
    def tearDown(self): self.monitor.close(); self.tmp.cleanup()
    def append(self,typ,payload):
        with self.log.open('a') as f: f.write(json.dumps({'type':typ,'timestamp':'2026-10-02T13:00:00Z','payload':payload})+'\n')
    def start(self):
        self.append('session_meta',{'id':'thread'})
        self.append('event_msg',{'type':'task_started','turn_id':'turn'})
        self.append('turn_context',{'turn_id':'turn','model':'unknown-model'})
    def usage(self,response='response1',turn='turn',root=None):
        self.append('token_usage_record',{'response_id':response,'turn_id':turn,'root_turn_id':root,
            'thread_id':'thread','usage':{'input_tokens':100,'cached_input_tokens':80,
            'output_tokens':10,'reasoning_output_tokens':3,'total_tokens':110}})
    def test_usage_dedup_and_restart(self):
        self.start(); self.usage(); self.usage()
        self.append('event_msg',{'type':'token_count','info':{'total_token_usage':{'input_tokens':9999}}})
        self.append('event_msg',{'type':'task_complete','turn_id':'turn'})
        self.monitor.scan()
        row=self.monitor.reports()['turns'][0]
        self.assertEqual(row['usage']['total_tokens'],110)
        self.assertEqual(row['model_calls'],1)
        self.assertEqual(row['status'],'completed')
        self.assertIsNone(row['api_equivalent_cost_usd'])
        self.monitor.close(); self.monitor=Monitor(self.root/'test.sqlite3',self.root)
        self.monitor.scan(); self.assertEqual(self.monitor.reports()['turns'][0]['model_calls'],1)
    def test_partial_lines_and_multiple_calls(self):
        self.start(); self.usage()
        partial=json.dumps({'type':'token_usage_record','payload':{'response_id':'response2','turn_id':'turn','usage':{'input_tokens':50,'output_tokens':5}}})
        with self.log.open('a') as f: f.write(partial)
        self.monitor.scan(); self.assertEqual(self.monitor.reports()['turns'][0]['model_calls'],1)
        with self.log.open('a') as f: f.write('\n')
        self.monitor.scan(); row=self.monitor.reports()['turns'][0]
        self.assertEqual(row['model_calls'],2)
        self.assertEqual(row['usage']['input_tokens'],150)
        self.assertIsNone(row['usage']['cached_input_tokens'])
    def test_child_calls_aggregate_under_root(self):
        self.start(); self.usage(); self.usage('child-response','child-turn','turn')
        self.monitor.scan(); self.assertEqual(self.monitor.reports()['turns'][0]['model_calls'],2)
    def test_root_owner_is_independent_of_child_arrival_order(self):
        for parent_first in (True,False):
            root='root-'+str(parent_first)
            parent={'type':'turn_context','timestamp':'2026-10-02T12:00:00Z','payload':{
                'turn_id':root,'model':'gpt-6.1-sol'}}
            child={'type':'token_usage_record','timestamp':'2026-10-02T12:01:00Z','payload':{
                'response_id':'child-response-'+root,'turn_id':'child-turn','root_turn_id':root,
                'thread_id':'child-chat','model':'gpt-6.1-sol',
                'usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}}}
            if parent_first: self.monitor.accept(parent,{'thread':'parent-chat'})
            self.monitor.accept(child,{'thread':'child-chat'})
            if not parent_first:
                pending=next(t for t in self.monitor.reports()['turns'] if t['turn_id']==root)
                self.assertIsNone(pending['thread_id'])
                self.assertIsNone(pending['started'])
                self.monitor.accept(parent,{'thread':'parent-chat'})
            self.monitor.accept(child,{'thread':'child-chat'})  # Duplicate stays deduplicated.
            second=json.loads(json.dumps(child));second['payload']['response_id']='second-'+root
            second['payload']['thread_id']='another-child'
            self.monitor.accept(second,{'thread':'another-child'})
            self.monitor.turn(root,status='completed'); self.monitor.db.commit()
            result=next(t for t in self.monitor.reports()['turns'] if t['turn_id']==root)
            self.assertEqual(result['thread_id'],'parent-chat')
            self.assertEqual(result['model_calls'],2)
            self.assertEqual(result['estimated_credits'],'0.00740000')
        self.monitor.close(); self.monitor=Monitor(self.root/'test.sqlite3',self.root)
        self.assertEqual({t['thread_id'] for t in self.monitor.reports()['turns']},{'parent-chat'})
    def test_repair_ownership_preserves_calls(self):
        self.start(); self.usage(); self.monitor.scan()
        before=self.monitor.db.execute('SELECT * FROM calls').fetchall()
        self.monitor.db.execute("UPDATE turns SET thread='incorrect-child'")
        self.monitor.db.execute('DELETE FROM turn_owners');self.monitor.db.commit()
        result=self.monitor.repair_ownership()
        self.assertEqual(result['corrected_turns'],1)
        self.assertEqual(result['unresolved_turns'],0)
        self.assertEqual(self.monitor.reports()['turns'][0]['thread_id'],'thread')
        self.assertEqual(self.monitor.db.execute('SELECT * FROM calls').fetchall(),before)
        self.assertEqual(self.monitor.repair_ownership()['corrected_turns'],0)
    def test_repair_does_not_infer_owner_from_child_only_log(self):
        self.append('session_meta',{'id':'child-chat'})
        self.append('token_usage_record',{'response_id':'child','turn_id':'child-turn','root_turn_id':'root',
            'thread_id':'child-chat','usage':{'input_tokens':10,'cached_input_tokens':0,'output_tokens':1}})
        self.monitor.scan()
        self.monitor.db.execute("UPDATE turns SET thread='child-chat' WHERE id='root'")
        result=self.monitor.repair_ownership()
        self.assertEqual(result['unresolved_turns'],1)
        self.assertIsNone(self.monitor.reports()['turns'][0]['thread_id'])

    def test_submission_does_not_store_prompt(self):
        response=self.monitor.submission({'turn_id':'turn','session_id':'thread','model':'unknown-model',
            'prompt':'Rewrite unique-private-text','hook_event_name':'UserPromptSubmit'})
        self.assertIn('systemMessage',response)
        row=self.monitor.reports()['turns'][0]
        self.assertFalse(row['preview']['prompt_stored'])
        self.assertNotIn('unique-private-text',json.dumps(row))
    def test_explicit_pricing_only_and_reasoning_not_added(self):
        self.start(); self.usage(); self.monitor.scan()
        self.monitor.prices={'version':'test','models':{'unknown-model':{'input':'2','cached_input':'0.5','output':'8'}}}
        self.assertEqual(self.monitor.reports()['turns'][0]['api_equivalent_cost_usd'],'0.00016000')
    def test_non_usage_activity_updates_without_retaining_content(self):
        self.start(); self.monitor.scan()
        with self.log.open('a') as f:
            f.write(json.dumps({'type':'response_item','timestamp':'2026-10-02T13:02:00Z',
                'payload':{'type':'function_call','arguments':'private content'}})+'\n')
        self.monitor.scan()
        row=self.monitor.reports()['turns'][0]
        self.assertEqual(row['last_activity_at'],'2026-10-02T13:02:00Z')
        self.assertNotIn('private content',json.dumps(row))

    def test_live_chat_name_prefers_renamed_name_and_refreshes(self):
        state=self.root/'state_5.sqlite'
        with closing(sqlite3.connect(state)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT, title TEXT, name TEXT, thread_source TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?,?)',('thread','GenAI learn','Learn GenAI','user'))
        self.monitor.state_database=state
        self.start(); self.monitor.scan()
        self.assertEqual(self.monitor.reports()['turns'][0]['chat_name'],'Learn GenAI')
        with closing(sqlite3.connect(state)) as db, db: db.execute("UPDATE threads SET name='Renamed chat'")
        self.assertEqual(self.monitor.reports()['turns'][0]['chat_name'],'Renamed chat')
        with closing(sqlite3.connect(state)) as db, db: db.execute("UPDATE threads SET name=NULL")
        self.assertEqual(self.monitor.reports()['turns'][0]['chat_name'],'GenAI learn')
        with closing(sqlite3.connect(state)) as db, db: db.execute("UPDATE threads SET thread_source='guardian_review'")
        self.assertEqual(self.monitor.reports()['floating_sessions'],[])

    def test_credit_estimate_cached_input_and_reasoning(self):
        self.start()
        self.append('token_usage_record', {'response_id':'known','turn_id':'turn','model':'gpt-6.1-sol',
            'usage':{'input_tokens':100000,'cached_input_tokens':90000,'output_tokens':10000,
            'reasoning_output_tokens':3000,'total_tokens':110000}})
        self.monitor.scan(); row=self.monitor.reports()['turns'][0]
        self.assertEqual(row['usage']['new_input_tokens'],10000)
        self.assertEqual(row['estimated_credits'],'3.22500000')
        self.assertIn('Standard',row['credit_basis'])
        self.append('token_usage_record', {'response_id':'second','turn_id':'turn','model':'gpt-6-sol',
            'usage':{'input_tokens':100000,'cached_input_tokens':90000,'output_tokens':10000}})
        self.monitor.scan(); row=self.monitor.reports()['turns'][0]
        self.assertEqual(row['estimated_credits'],'6.67500000')
        self.assertEqual(row['usage']['new_input_tokens'],20000)
    def test_unknown_or_missing_cache_does_not_guess_credits(self):
        self.start(); self.usage(); self.monitor.scan()
        self.assertIsNone(self.monitor.reports()['turns'][0]['estimated_credits'])
        self.append('token_usage_record', {'response_id':'no-cache','turn_id':'another','model':'gpt-6.1-sol',
            'usage':{'input_tokens':100,'output_tokens':10}})
        self.monitor.scan()
        row=next(r for r in self.monitor.reports()['turns'] if r['turn_id']=='another')
        self.assertIsNone(row['estimated_credits'])
        self.assertIsNone(row['usage']['new_input_tokens'])
    def test_invalid_cached_count_does_not_produce_negative_new_input(self):
        self.start()
        self.append('token_usage_record', {'response_id':'bad-cache','turn_id':'turn','model':'gpt-6.1-sol',
            'usage':{'input_tokens':100,'cached_input_tokens':200,'output_tokens':10}})
        self.monitor.scan(); row=self.monitor.reports()['turns'][0]
        self.assertIsNone(row['estimated_credits'])
        self.assertIsNone(row['usage']['new_input_tokens'])

    def test_credit_header_does_not_infer_account_credits(self):
        self.start(); self.usage(); self.monitor.scan()
        credits=self.monitor.reports()['credit_summary']
        self.assertIsNone(credits['consumed'])
        self.assertIsNone(credits['total'])

    def test_chat_totals_include_older_turns_beyond_page_limit(self):
        state={'thread':'chat-a','model':'gpt-6.1-sol'}
        for n in range(105):
            turn='a-'+str(n)
            stamp='2026-10-02T13:00:%02dZ' % (n % 60)
            self.monitor.accept({'type':'event_msg','timestamp':stamp,'payload':{'type':'task_started','turn_id':turn}},state)
            self.monitor.accept({'type':'token_usage_record','timestamp':stamp,'payload':{
                'response_id':'r-'+str(n),'turn_id':turn,'thread_id':'chat-a','model':'gpt-6.1-sol',
                'usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}}},state)
        self.monitor.db.execute("UPDATE turns SET status='completed'")
        self.monitor.db.commit()
        report=self.monitor.reports(limit=1)
        self.assertEqual(len(report['turns']),1)
        chat=report['chats'][0]
        self.assertEqual(chat['recorded_turns'],105)
        self.assertEqual(chat['model_calls'],105)
        self.assertEqual(chat['usage']['new_input_tokens'],2100)
        self.assertEqual(chat['estimated_credits'],'0.38850000')
        self.assertEqual(report['floating_sessions'][0]['estimated_credits'],'0.38850000')
        self.monitor.close(); self.monitor=Monitor(self.root/'test.sqlite3',self.root)
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.38850000')
    def test_chat_history_filter_and_partial_totals(self):
        state={'thread':'chat-a'}
        for turn,thread,model in [('one','chat-a','gpt-6.1-sol'),('two','chat-a','unknown'),('three','chat-b','gpt-6-sol')]:
            self.monitor.accept({'type':'token_usage_record','timestamp':'2026-10-02T13:00:00Z','payload':{
                'response_id':turn,'turn_id':turn,'thread_id':thread,'model':model,
                'usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}}},state)
        self.monitor.db.execute("UPDATE turns SET status='completed'")
        report=self.monitor.reports(thread_id='chat-a')
        self.assertEqual({t['turn_id'] for t in report['turns']},{'one','two'})
        self.assertEqual(len(report['chats']),2)
        chat=next(c for c in report['chats'] if c['thread_id']=='chat-a')
        self.assertIsNone(chat['estimated_credits'])
        self.assertEqual(chat['known_estimated_credits'],'0.00370000')
        self.assertEqual(chat['unknown_credit_calls'],1)
        self.assertEqual(chat['usage']['new_input_tokens'],40)
        self.assertFalse(chat['credit_coverage_complete'])

    def test_reasoning_effort_is_recorded_per_turn(self):
        state={'thread':'chat'}
        for turn,effort,stamp in [('old','low','2026-10-02T13:00:00Z'),('new','high','2026-10-02T13:01:00Z')]:
            self.monitor.accept({'type':'turn_context','timestamp':stamp,'payload':{
                'turn_id':turn,'model':'gpt-6.1-sol','effort':effort}},state)
        self.monitor.db.commit()
        reports=self.monitor.reports()
        self.assertEqual([t['reasoning_effort'] for t in reports['turns']],['high','low'])
        self.assertEqual(reports['chats'][0]['reasoning_effort'],'high')
        self.monitor.close(); self.monitor=Monitor(self.root/'test.sqlite3',self.root)
        self.assertEqual(self.monitor.reports()['chats'][0]['reasoning_effort'],'high')
    def test_latest_setting_is_not_assigned_to_historical_turns(self):
        state_db=self.root/'state_5.sqlite'
        with closing(sqlite3.connect(state_db)) as db, db:
            db.execute('CREATE TABLE threads(id TEXT,title TEXT,reasoning_effort TEXT)')
            db.execute('INSERT INTO threads VALUES (?,?,?)',('chat','Chat title','medium'))
        self.monitor.state_database=state_db
        self.monitor.turn('old','chat',started='2026-10-02T13:00:00Z')
        self.monitor.turn('new','chat',started='2026-10-02T13:01:00Z')
        rows=self.monitor.reports()['turns']
        self.assertEqual(rows[0]['reasoning_effort'],'medium')
        self.assertIsNone(rows[1]['reasoning_effort'])

    def test_chat_credits_hold_until_turn_finishes_and_survive_restart(self):
        state={'thread':'chat','model':'gpt-6.1-sol'}
        def record(turn,response):
            self.monitor.accept({'type':'token_usage_record','timestamp':'2026-10-02T13:01:00Z','payload':{
                'response_id':response,'turn_id':turn,'thread_id':'chat','model':'gpt-6.1-sol',
                'usage':{'input_tokens':100,'cached_input_tokens':80,'output_tokens':10}}},state)
        self.monitor.turn('previous','chat',started='2026-10-02T13:00:00Z',status='completed')
        record('previous','one')
        self.monitor.turn('active','chat',started='2026-10-02T13:01:00Z',status='running')
        record('active','two')
        chat=self.monitor.reports()['chats'][0]
        self.assertEqual(chat['estimated_credits'],'0.00370000')
        self.assertEqual(chat['usage']['new_input_tokens'],40)
        self.assertEqual(chat['pending_turns'],1)
        self.assertFalse(chat['credits_pending'])
        record('active','three')
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.00370000')
        self.monitor.db.commit(); self.monitor.close()
        self.monitor=Monitor(self.root/'test.sqlite3',self.root)
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.00370000')
        self.monitor.turn('active',status='completed')
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.01110000')
        self.monitor.turn('aborted','chat',started='2026-10-02T13:02:00Z',status='running')
        record('aborted','four')
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.01110000')
        self.monitor.turn('aborted',status='interrupted')
        self.assertEqual(self.monitor.reports()['chats'][0]['estimated_credits'],'0.01480000')

    def test_schema_drift_is_visible(self):
        self.start(); self.append('token_usage_record',{'usage':{'unexpected':1}})
        self.monitor.scan(); self.assertEqual(self.monitor.reports()['diagnostics'],1)

class FloatingSessionsTests(unittest.TestCase):
    now = datetime(2026, 10, 2, 13, 5, tzinfo=timezone.utc)
    def row(self, turn, thread, stamp, model='model'):
        return {'turn_id':turn,'thread_id':thread,'model':model,'started':stamp,'last_activity_at':stamp,'status':'running'}
    def test_multiple_active_chats_and_latest_per_chat(self):
        rows=[self.row('a2','a','2026-10-02T13:04:59Z'),self.row('b1','b','2026-10-02T13:04:00Z'),
              self.row('a1','a','2026-10-02T13:03:30Z')]
        self.assertEqual([r['turn_id'] for r in floating_sessions(rows,self.now)],['a2','b1'])
    def test_exact_timeout_and_fallback(self):
        rows=[self.row('a','a','2026-10-02T13:04:59Z'),self.row('b','b','2026-10-02T13:03:00Z')]
        self.assertEqual([r['turn_id'] for r in floating_sessions(rows,self.now)],['a'])
        later=datetime(2026,10,2,13,10,tzinfo=timezone.utc)
        self.assertEqual([r['turn_id'] for r in floating_sessions(rows,later)],['a'])
    def test_stale_latest_does_not_hide_active_other_chat(self):
        rows=[self.row('a','a','2026-10-02T13:01:00Z'),self.row('b','b','2026-10-02T13:04:30Z')]
        self.assertEqual([r['turn_id'] for r in floating_sessions(rows,self.now)],['b'])
    def test_internal_approvals_excluded_and_empty(self):
        rows=[self.row('internal','i','2026-10-02T13:04:59Z','codex-auto-review')]
        self.assertEqual(floating_sessions(rows,self.now),[])
        guardian=self.row('g','guardian','2026-10-02T13:04:59Z'); guardian['is_internal']=True
        self.assertEqual(floating_sessions([guardian],self.now),[])
    def test_completed_chat_gets_grace_period(self):
        row=self.row('a','a','2026-10-02T13:04:00Z');row['status']='completed'
        self.assertEqual(floating_sessions([row],self.now),[row])


class SessionDisplayNameTests(unittest.TestCase):
    def test_named_chat_is_preserved(self):
        self.assertEqual(session_display_name('Learn GenAI','user',None),'Learn GenAI')
    def test_review_task_uses_meaningful_name(self):
        source=json.dumps({'subagent':{'thread_spawn':{'agent_path':'/root/uncommitted_review','agent_nickname':'Mill'}}})
        self.assertEqual(session_display_name('', 'subagent',source),'Code review')
    def test_other_internal_task_and_fallback(self):
        source={'subagent':{'thread_spawn':{'agent_path':'/root/test_runner'}}}
        self.assertEqual(session_display_name(None,'subagent',source),'Test runner')
        self.assertEqual(session_display_name(None,'subagent','invalid'),'Agent session')
        self.assertIsNone(session_display_name(None,'user','invalid'))

if __name__=='__main__': unittest.main()
