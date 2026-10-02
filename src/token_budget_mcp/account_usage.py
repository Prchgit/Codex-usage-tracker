"""Normalize and cache built-in get_usage_limits snapshots; no network calls."""
import argparse
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import sqlite3
import sys
from .config import DEFAULT_ACCOUNT_USAGE_MAX_AGE, positive_number

SOURCE = 'codex_get_usage_limits'


def percentage(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return max(0, min(100, value))


def normalize_limits(payload, now=None, source=SOURCE, refresh_interval_seconds=None):
    if not isinstance(payload, dict): raise ValueError('Usage limits response must be an object')
    by_id = payload.get('rateLimitsByLimitId')
    buckets = by_id if isinstance(by_id, dict) and by_id else {'codex': payload.get('rateLimits')}
    limits = []
    for identifier, bucket in buckets.items():
        if not isinstance(bucket, dict): continue
        individual = bucket.get('individualLimit')
        if isinstance(individual, dict):
            remaining = percentage(individual.get('remainingPercent'))
            if remaining is not None:
                limits.append({'limit_id': identifier, 'label': 'Account', 'used_percent': 100 - remaining,
                    'resets_at': individual.get('resetsAt')})
                continue
        for name in ('primary', 'secondary'):
            window = bucket.get(name)
            if not isinstance(window, dict): continue
            used = percentage(window.get('usedPercent'))
            if used is not None:
                limits.append({'limit_id': identifier, 'label': name.capitalize(), 'used_percent': used,
                    'window_duration_mins': window.get('windowDurationMins'), 'resets_at': window.get('resetsAt')})
    stamp = now or datetime.now(timezone.utc)
    if refresh_interval_seconds is not None: positive_number(refresh_interval_seconds, 'refresh_interval_seconds')
    return {'source': source, 'updated_at': stamp.isoformat(), 'limits': limits,
        'auto_refresh': refresh_interval_seconds is not None, 'refresh_interval_seconds': refresh_interval_seconds,
        'basis': 'Account-wide limits reported by Codex; separate from per-chat token estimates.'}


def write_snapshot(database, payload, now=None, source=SOURCE, refresh_interval_seconds=None):
    snapshot = normalize_limits(payload, now, source, refresh_interval_seconds)
    with closing(sqlite3.connect(str(database))) as db, db:
        db.execute('CREATE TABLE IF NOT EXISTS account_usage_snapshot(id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL)')
        db.execute('INSERT OR REPLACE INTO account_usage_snapshot VALUES (1, ?)', (json.dumps(snapshot),))
    return snapshot


def read_snapshot(db, max_age_seconds=DEFAULT_ACCOUNT_USAGE_MAX_AGE, now=None):
    positive_number(max_age_seconds, 'account_usage_max_age')
    try:
        row = db.execute('SELECT body FROM account_usage_snapshot WHERE id=1').fetchone()
    except sqlite3.OperationalError:
        return None
    if row is None: return None
    try:
        snapshot = json.loads(row[0])
        age = ((now or datetime.now(timezone.utc)) - datetime.fromisoformat(snapshot['updated_at'])).total_seconds()
        snapshot['stale'] = age < 0 or age >= max_age_seconds
        return snapshot
    except (ValueError, TypeError, KeyError): return None


def main(argv=None):
    parser = argparse.ArgumentParser(description='Cache a get_usage_limits response supplied on stdin')
    parser.add_argument('--database', required=True)
    args = parser.parse_args(argv)
    try: snapshot = write_snapshot(args.database, json.load(sys.stdin))
    except (ValueError, OSError, sqlite3.Error):
        parser.error('Cannot save account usage; check response format and database access')
    print(json.dumps({'source': snapshot['source'], 'available_limits': len(snapshot['limits'])}))


if __name__ == '__main__': main()
