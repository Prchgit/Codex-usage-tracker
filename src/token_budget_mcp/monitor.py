"""Local Codex usage observer. No model calls and no prompt storage."""
from contextlib import closing
from collections import defaultdict
import json
import math
import logging
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from .usage import FIELDS, aggregate_chats, floating_sessions, session_display_name, summarize_calls, normalize_usage
from .config import (DEFAULT_INACTIVITY_SECONDS, DEFAULT_HISTORY_LIMIT,
                     DEFAULT_DATABASE_TIMEOUT, STATE_DATABASE_TIMEOUT, positive_number)

from .account_usage import read_snapshot

logger = logging.getLogger(__name__)

class Monitor:
    def __init__(self, database, sessions, prices=None, readonly=False, state_database=None, inactivity_seconds=DEFAULT_INACTIVITY_SECONDS, credit_rates=None):
        self.inactivity_seconds = positive_number(inactivity_seconds, "inactivity_seconds")
        self.database = str(database)
        self.sessions = Path(sessions)
        self.state_database = Path(state_database) if state_database else None
        self.credit_rates = json.loads(Path(credit_rates or Path(__file__).with_name('credit_rates.json')).read_text())
        self.prices = json.loads(Path(prices).read_text()) if prices else {'version':'not-configured','models':{}}
        for rates in (self.credit_rates, self.prices):
            if not isinstance(rates, dict) or not isinstance(rates.get('models'), dict) or not isinstance(rates.get('version'), str):
                raise ValueError('Pricing configuration must contain version and models')
        if not isinstance(self.credit_rates.get('basis'), str):
            raise ValueError('Credit rates must describe their basis')
        self.lock = threading.RLock()
        self.summary_cache = {}
        self.summary_rates = None
        connection = Path(self.database).resolve().as_uri() + '?mode=ro' if readonly else self.database
        self.db = sqlite3.connect(connection, uri=readonly,
                                 check_same_thread=False, timeout=DEFAULT_DATABASE_TIMEOUT)
        if not readonly:
            had_issues = self.db.execute("SELECT 1 FROM sqlite_master WHERE name='turn_issues'").fetchone() is not None
            self.db.execute('PRAGMA journal_mode=WAL')
            self.db.executescript('''
          CREATE TABLE IF NOT EXISTS files(path TEXT PRIMARY KEY, offset INTEGER, inode INTEGER, state TEXT);
          CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY, thread TEXT, model TEXT, started TEXT,
            status TEXT, preview TEXT);
          CREATE TABLE IF NOT EXISTS calls(id TEXT PRIMARY KEY, turn_id TEXT, model TEXT, usage TEXT, timestamp TEXT);
          CREATE TABLE IF NOT EXISTS turn_owners(turn_id TEXT PRIMARY KEY, thread TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS turn_settings(turn_id TEXT PRIMARY KEY, reasoning_effort TEXT);
          CREATE TABLE IF NOT EXISTS activity(turn_id TEXT PRIMARY KEY, timestamp TEXT);
          CREATE INDEX IF NOT EXISTS calls_turn_timestamp ON calls(turn_id, timestamp);
          CREATE INDEX IF NOT EXISTS turns_thread_started ON turns(thread, started);
          CREATE TABLE IF NOT EXISTS turn_issues(turn_id TEXT PRIMARY KEY, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS diagnostics(path TEXT PRIMARY KEY, message TEXT);
            ''')
            if not had_issues:
                # Old versions retained diagnostics only per file. Conservatively mark
                # its chat's turns rather than present already-skipped usage as complete.
                for raw, in self.db.execute('SELECT files.state FROM files JOIN diagnostics ON files.path=diagnostics.path').fetchall():
                    try: state = json.loads(raw)
                    except (ValueError, TypeError): continue
                    if not isinstance(state, dict): continue
                    if isinstance(state.get('thread'), str):
                        self.db.execute("INSERT OR IGNORE INTO turn_issues SELECT id, 'Legacy unsupported usage' FROM turns WHERE thread=?", (state['thread'],))
                    else: self.mark_incomplete(state.get('turn'))
            self.db.commit()

    def close(self): self.db.close()

    def turn(self, identifier, thread=None, model=None, started=None, status=None, preview=None):
        self.db.execute('''INSERT INTO turns VALUES (?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
            thread=COALESCE(excluded.thread,turns.thread),model=COALESCE(excluded.model,turns.model),
            started=COALESCE(turns.started,excluded.started), status=CASE WHEN turns.status IN ('completed','interrupted')
                THEN turns.status ELSE COALESCE(excluded.status,turns.status) END,
            preview=COALESCE(excluded.preview,turns.preview)''',
            (identifier,thread,model,started,status,json.dumps(preview) if preview else None))

    def parent_owner(self, identifier, thread):
        """Only records belonging to the root turn can establish its owning chat."""
        if identifier and thread:
            self.db.execute('INSERT OR IGNORE INTO turn_owners VALUES (?,?)',(identifier,thread))
        row=self.db.execute('SELECT thread FROM turn_owners WHERE turn_id=?',(identifier,)).fetchone()
        return row[0] if row else None

    def repair_ownership(self):
        """Reconstruct owners from local parent metadata without changing usage/calls."""
        with self.lock:
            before=dict(self.db.execute('SELECT id,thread FROM turns'))
            paths=[Path(row[0]) for row in self.db.execute('SELECT path FROM files')]
            candidates={}
            unreadable=0
            for path in paths:
                thread=None
                try:
                    with path.open() as stream:
                        for line in stream:
                            try: row=json.loads(line)
                            except (ValueError,TypeError): continue
                            payload=row.get('payload') or {}
                            if not isinstance(payload,dict): continue
                            kind=payload.get('type',row.get('type'))
                            if row.get('type')=='session_meta': thread=payload.get('id')
                            identifier=payload.get('turn_id')
                            if kind in ('task_started','turn_context') and identifier and thread:
                                candidates[identifier]=thread
                            elif kind=='token_usage_record' and identifier and payload.get('root_turn_id') in (None,identifier):
                                owner=payload.get('thread_id') or thread
                                if owner: candidates.setdefault(identifier,owner)
                except OSError: unreadable+=1
            for identifier,thread in candidates.items():
                self.db.execute('INSERT OR REPLACE INTO turn_owners VALUES (?,?)',(identifier,thread))
            self.db.execute('UPDATE turns SET thread=(SELECT thread FROM turn_owners WHERE turn_id=turns.id)')
            self.db.commit()
            after=dict(self.db.execute('SELECT id,thread FROM turns'))
            return {'corrected_turns':sum(before[k]!=v for k,v in after.items()),
                    'unresolved_turns':sum(v is None for v in after.values()),'unreadable_files':unreadable,
                    'calls_modified':0}

    def touch(self, identifier, stamp):
        if identifier and stamp:
            self.db.execute('INSERT INTO activity VALUES (?,?) ON CONFLICT(turn_id) DO UPDATE SET timestamp=MAX(activity.timestamp,excluded.timestamp)', (identifier, stamp))

    def mark_incomplete(self, identifier):
        if isinstance(identifier, str) and identifier:
            self.db.execute('INSERT OR IGNORE INTO turn_issues VALUES (?,?)',
                (identifier, 'Unsupported usage record'))

    def accept(self, row, state):
        if not isinstance(row, dict): raise ValueError("Log record must be an object")
        payload=row.get('payload') or {}
        if not isinstance(payload,dict): return
        kind=payload.get('type',row.get('type'))
        stamp=row.get('timestamp')
        for value in (stamp, payload.get('id'), payload.get('turn_id'), payload.get('root_turn_id'), payload.get('thread_id'), payload.get('model')):
            if value is not None and not isinstance(value, str):
                self.mark_incomplete(state.get('turn'))
                raise ValueError('Log identifiers, model and timestamp must be text')
        if row.get('type')=='session_meta': state['thread']=payload.get('id')
        if kind in ('task_started','turn_context'):
            identifier=payload.get('turn_id')
            if identifier:
                state['turn']=identifier
                if payload.get('model'): state['model']=payload['model']
                self.turn(identifier,self.parent_owner(identifier,state.get('thread')),payload.get('model'),stamp,
                          'running' if kind=='task_started' else None)
                effort=payload.get('effort') or payload.get('reasoning_effort')
                if isinstance(effort,str):
                    self.db.execute('INSERT OR REPLACE INTO turn_settings VALUES (?,?)',(identifier,effort))
        if kind=='token_usage_record':
            identifier=payload.get('root_turn_id') or payload.get('turn_id')
            response=payload.get('response_id')
            usage=payload.get('usage')
            if not isinstance(identifier, str) or not identifier or not isinstance(response, str) or not response:
                state['unsupported_usage'] = True
                self.mark_incomplete(identifier or state.get('turn'))
                return
            try:
                clean, invalid = normalize_usage(usage)
            except ValueError:
                state['unsupported_usage'] = True
                self.mark_incomplete(identifier)
                return
            if invalid:
                state['unsupported_usage'] = True
                self.mark_incomplete(identifier)
            is_child=payload.get('turn_id') != identifier
            owner=self.parent_owner(identifier,None if is_child else payload.get('thread_id') or state.get('thread'))
            self.turn(identifier,owner,None,None if is_child else stamp)
            self.db.execute('INSERT OR IGNORE INTO calls VALUES (?,?,?,?,?)',
                (response,identifier,payload.get('model') or state.get('model'),json.dumps(clean),stamp))
        if kind in ('task_complete','turn_aborted'):
            identifier=payload.get('turn_id') or state.get('turn')
            if identifier: self.turn(identifier,status='completed' if kind=='task_complete' else 'interrupted')
        if row.get('type') != 'session_meta':
            self.touch(payload.get('root_turn_id') or payload.get('turn_id') or state.get('turn'), stamp)
        # Deliberately ignore message content and cumulative token_count snapshots:
        # adding both snapshots and individual records would double count.

    def scan(self, since=0):
        with self.lock:
            for path in self.sessions.glob('**/rollout-*.jsonl'):
                try: stat=path.stat()
                except OSError:
                    logger.warning('Cannot stat a session log; continuing with other files')
                    continue
                saved=self.db.execute('SELECT offset,inode,state FROM files WHERE path=?',(str(path),)).fetchone()
                if not saved and stat.st_mtime < since: continue
                offset,inode,state = saved if saved else (0,stat.st_ino,'{}')
                state=json.loads(state)
                if inode!=stat.st_ino or offset>stat.st_size: offset,state=0,{}
                if offset==stat.st_size: continue
                try:
                    with path.open('rb') as stream:
                        stream.seek(offset)
                        while True:
                            position=stream.tell(); line=stream.readline()
                            if not line or not line.endswith(b'\n'):
                                stream.seek(position); break
                            try: self.accept(json.loads(line),state)
                            except (ValueError,TypeError,KeyError):
                                self.mark_incomplete(state.get('turn'))
                                self.db.execute('INSERT OR REPLACE INTO diagnostics VALUES (?,?)',
                                    (str(path),'Unsupported record encountered; completeness may be affected.'))
                        offset=stream.tell()
                    self.db.execute('INSERT OR REPLACE INTO files VALUES (?,?,?,?)',
                        (str(path),offset,stat.st_ino,json.dumps(state)))
                    if state.get('unsupported_usage'):
                        self.db.execute('INSERT OR REPLACE INTO diagnostics VALUES (?,?)',
                            (str(path),'Usage schema unsupported; some usage unavailable.'))
                    self.db.commit()
                except OSError:
                    self.db.rollback()
                    logger.warning('Cannot read a session log; continuing with other files')

    def submission(self, event):
        if not isinstance(event, dict): raise ValueError("Hook event must be an object")
        if not isinstance(event.get("prompt", ""), str): raise ValueError("Hook prompt must be text")
        identifier=event.get('turn_id')
        if not identifier: return {'systemMessage':'Token monitor: no turn ID supplied; preview unavailable.'}
        prompt=event.get('prompt','')
        n=max(1,math.ceil(len(prompt.encode('utf-8'))/3))
        with self.lock:
            row=self.db.execute('SELECT usage FROM calls WHERE turn_id IN (SELECT id FROM turns WHERE thread=?) ORDER BY timestamp DESC LIMIT 1',
                                (event.get('session_id'),)).fetchone()
            try: previous = normalize_usage(json.loads(row[0]))[0]['input_tokens'] if row else None
            except (ValueError, TypeError): previous = None
            simple=any(word in prompt.lower() for word in ('extract','classify','rewrite','translate','summarize'))
            preview={'prompt_tokens_estimate':n,'estimated_next_input_tokens':previous+n if previous else None,
                'basis':'UTF-8 length heuristic plus previous observed input; not full request count',
                'output_estimate':None,'cost_estimate':None,
                'recommendation': 'Consider a cheaper available Codex model for this apparently simple task; no validated model mapping.' if simple
                                  else 'Retain selected model; no validated cheaper-model rule for this task.',
                'model':event.get('model'),'prompt_stored':False}
            self.turn(identifier,self.parent_owner(identifier,event.get('session_id')),event.get('model'),datetime.now(timezone.utc).isoformat(),'submitted',preview)
            self.touch(identifier, datetime.now(timezone.utc).isoformat())
            self.db.commit()
        return {'systemMessage':f"Token monitor: message ≈{n} tokens (heuristic; full context excluded). {preview['recommendation']}"}

    def chat_names(self, threads):
        """Read only displayed names/titles, never first messages or prompt previews."""
        paths = [self.state_database] if self.state_database else sorted(
            self.sessions.parent.glob('state_*.sqlite'),
            key=lambda p: int(p.stem.split('_')[-1]) if p.stem.split('_')[-1].isdigit() else 0,
            reverse=True)
        for path in paths:
            try:
                with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True, timeout=STATE_DATABASE_TIMEOUT)) as db:
                    columns = {row[1] for row in db.execute('PRAGMA table_info(threads)')}
                    if not {'id','title'}.issubset(columns): continue
                    name = "COALESCE(NULLIF(name,''),title)" if 'name' in columns else 'title'
                    identifiers = list(filter(None, threads))
                    if not identifiers: return {}
                    placeholders = ','.join('?' for _ in identifiers)
                    source = 'thread_source' if 'thread_source' in columns else "'unknown'"
                    effort = 'reasoning_effort' if 'reasoning_effort' in columns else 'NULL'
                    details = 'source' if 'source' in columns else 'NULL'
                    return {identifier: {'name': session_display_name(title,origin,metadata), 'internal': origin == 'guardian_review','reasoning_effort': setting}
                            for identifier, title, origin, setting, metadata in db.execute(f'SELECT id,{name},{source},{effort},{details} FROM threads WHERE id IN ({placeholders})', identifiers)}
            except (sqlite3.Error, OSError): continue
        return {}

    def reports(self, limit=DEFAULT_HISTORY_LIMIT, thread_id=None):
        if type(limit) is not int or limit < 1:
            raise ValueError("History limit must be a positive integer")
        with self.lock:
            rows=self.db.execute('SELECT id,thread,model,started,status,preview FROM turns ORDER BY started DESC').fetchall()
            names = self.chat_names({row[1] for row in rows})
            # Batch reads avoid a table scan and several SQL round trips per turn.
            calls_by_turn = defaultdict(list)
            last_calls = {}
            for turn, model, usage, stamp in self.db.execute('SELECT turn_id,model,usage,timestamp FROM calls'):
                calls_by_turn[turn].append((model, usage))
                if stamp: last_calls[turn] = max(last_calls.get(turn, stamp), stamp)
            def optional_rows(query):
                try: return self.db.execute(query).fetchall()
                except sqlite3.OperationalError: return []  # Older read-only databases.
            settings = dict(optional_rows('SELECT turn_id,reasoning_effort FROM turn_settings'))
            activities = dict(optional_rows('SELECT turn_id,timestamp FROM activity'))
            issues = dict(optional_rows('SELECT turn_id,reason FROM turn_issues'))
            rates = json.dumps([self.credit_rates, self.prices], sort_keys=True)
            if rates != self.summary_rates:
                self.summary_cache.clear()
                self.summary_rates = rates
            reports=[]
            latest_threads=set()
            for identifier,thread,model,started,status,preview in rows:
                effort = settings.get(identifier)
                if identifier not in settings and thread not in latest_threads:
                    effort = names.get(thread, {}).get('reasoning_effort')
                latest_threads.add(thread)
                calls = calls_by_turn.get(identifier, [])
                signature = tuple(calls)
                cached = self.summary_cache.get(identifier)
                if cached is None or cached[0] != signature:
                    cached = (signature, summarize_calls(calls, self.credit_rates, self.prices))
                    self.summary_cache[identifier] = cached
                summary = dict(cached[1])
                if identifier in issues:
                    summary['usage_coverage_complete'] = False
                    summary['estimated_credits'] = None
                    summary['api_equivalent_cost_usd'] = None
                last_activity = max(filter(None, [started, last_calls.get(identifier), activities.get(identifier)]), default=None)
                reports.append({'chat_name':names.get(thread,{}).get('name'),'is_internal':names.get(thread,{}).get('internal',False),'last_activity_at':last_activity,'turn_id':identifier,'thread_id':thread,'model':model,'reasoning_effort':effort,'started':started,'status':status,
                    'preview':json.loads(preview) if preview else None,'model_calls':len(calls),
                    **summary, 'usage_basis':'codex_local_usage_records' if calls else 'unavailable',
                    'credits_pending':status not in ('completed','interrupted'),
                    'credit_basis':self.credit_rates['basis'], 'credit_rates_version':self.credit_rates['version'],
                    'cost_basis':'API-equivalent estimate, not subscription billing; only exact configured model matches priced',
                    'pricing_version':self.prices['version']})
            chats = aggregate_chats(reports)
            return {'account_usage':read_snapshot(self.db),'chats':chats,'credit_summary':{'consumed':None,'total':None,'basis':'Actual account credit consumption and allocation are not supplied by local token usage records.'},'turns':[r for r in reports if thread_id is None or r['thread_id']==thread_id][:limit],'floating_sessions':floating_sessions(chats, inactivity_seconds=self.inactivity_seconds),'inactivity_seconds':self.inactivity_seconds,'diagnostics':self.db.execute('SELECT COUNT(*) FROM diagnostics').fetchone()[0],
                'coverage':'Local session logs only; remote/cloud sessions and unrecorded usage excluded.',
                'updated_at':datetime.now(timezone.utc).isoformat()}


# Keep this import for existing callers; HTTP responsibilities live in web_server.
from .web_server import serve


def main(argv=None):
    """Compatibility entry point; parsing lives in monitor_cli."""
    from .monitor_cli import main as run
    return run(argv)

if __name__ == '__main__':
    main()
