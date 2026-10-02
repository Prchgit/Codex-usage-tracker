"""Command-line and Codex hook entry points for the local observer."""
import argparse
import json
import logging
import sqlite3
import sys
import time
from pathlib import Path
from .config import (DEFAULT_PORT, DEFAULT_POLL_INTERVAL, DEFAULT_INACTIVITY_SECONDS,
                     DEFAULT_HOOK_LOOKBACK_SECONDS, DEFAULT_ACCOUNT_REFRESH_INTERVAL, positive_number, parse_since)
from .monitor import Monitor
from .web_server import serve


def main(argv=None):
    parser=argparse.ArgumentParser(description="Observe local Codex usage without making model calls")
    parser.add_argument('--database',required=True)
    parser.add_argument('--sessions',default=str(Path.home()/'.codex/sessions'))
    parser.add_argument('--prices')
    parser.add_argument('--since', help='ISO date/time cutoff for previously unseen logs; default: all local logs')
    parser.add_argument('--port',type=int,default=DEFAULT_PORT)
    parser.add_argument('--poll-interval',type=float,default=DEFAULT_POLL_INTERVAL)
    parser.add_argument('--inactivity-seconds',type=float,default=DEFAULT_INACTIVITY_SECONDS)
    parser.add_argument('--credit-rates')
    parser.add_argument('--codex-command', help='Codex executable used for account-limit refresh')
    parser.add_argument('--account-refresh-interval', type=float, default=DEFAULT_ACCOUNT_REFRESH_INTERVAL)
    parser.add_argument('--hook',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--repair-ownership',action='store_true')
    args=parser.parse_args(argv)
    try:
        positive_number(args.poll_interval, 'poll_interval')
        positive_number(args.inactivity_seconds, 'inactivity_seconds')
        positive_number(args.account_refresh_interval, 'account_refresh_interval')
        if not 1 <= args.port <= 65535: raise ValueError('Port must be between 1 and 65535')
        since = parse_since(args.since)
    except ValueError as error:
        parser.error(str(error))
    logging.basicConfig(level=logging.WARNING)
    try:
        monitor=Monitor(args.database,args.sessions,args.prices,
                        inactivity_seconds=args.inactivity_seconds, credit_rates=args.credit_rates)
    except (OSError, sqlite3.Error, ValueError):
        parser.error('Cannot initialize collector; check database access and pricing configuration')
    try:
        if args.repair_ownership:
            print(json.dumps(monitor.repair_ownership()))
        elif args.hook:
            event=json.load(sys.stdin)
            if not isinstance(event, dict): raise ValueError('Hook event must be an object')
            if event.get('hook_event_name')=='UserPromptSubmit':
                output=monitor.submission(event)
            else:
                monitor.scan(time.time()-DEFAULT_HOOK_LOOKBACK_SECONDS)
                rows=[r for r in monitor.reports()['turns'] if r['turn_id']==event.get('turn_id')]
                usage=rows[0]['usage'] if rows else None
                output={'systemMessage':f"Token monitor: recorded so far {usage['input_tokens']} input / {usage['output_tokens']} output tokens. Final report: http://127.0.0.1:{args.port}"} if usage else {}
            print(json.dumps(output))
        elif args.once:
            monitor.scan(since)
            print(json.dumps(monitor.reports(),indent=2))
        else: serve(monitor,args.port,since,poll_interval=args.poll_interval,
                    codex_command=args.codex_command, account_refresh_interval=args.account_refresh_interval)
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.error(f'Collector operation failed ({type(error).__name__}); check input and local file access')
    finally: monitor.close()
