import json
from contextlib import closing
import sqlite3
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import unittest
from token_budget_mcp.account_usage import normalize_limits, read_snapshot, write_snapshot


class AccountUsageTests(unittest.TestCase):
    now = datetime(2026, 10, 2, 12, tzinfo=timezone.utc)

    def test_business_limit_is_remaining_complement_without_sensitive_fields(self):
        snapshot = normalize_limits({'accountId': 'private-account', 'rateLimitsByLimitId': {
            'codex': {'individualLimit': {'remainingPercent': 71, 'limit': '1250', 'used': '356', 'resetsAt': 123}}}}, self.now)
        self.assertEqual(snapshot['limits'][0]['used_percent'], 29)
        self.assertFalse(snapshot['auto_refresh'])
        self.assertNotIn('private-account', json.dumps(snapshot))
        self.assertNotIn('1250', json.dumps(snapshot))

    def test_multiple_windows_are_not_added_or_averaged(self):
        snapshot = normalize_limits({'rateLimits': {'primary': {'usedPercent': 40, 'windowDurationMins': 300},
            'secondary': {'usedPercent': 80, 'windowDurationMins': 10080}}}, self.now)
        self.assertEqual([limit['used_percent'] for limit in snapshot['limits']], [40, 80])

    def test_duration_labels_and_unknown_duration_fallback(self):
        snapshot = normalize_limits({'rateLimits': {'primary': {'usedPercent': 40, 'windowDurationMins': 300},
            'secondary': {'usedPercent': 80, 'windowDurationMins': 10080}}}, self.now)
        self.assertEqual([limit['label'] for limit in snapshot['limits']], ['5h', 'Weekly'])
        snapshot = normalize_limits({'rateLimits': {'primary': {'usedPercent': 40, 'windowDurationMins': True}}}, self.now)
        self.assertEqual(snapshot['limits'][0]['label'], 'Primary')

    def test_by_id_preferred_over_legacy(self):
        snapshot = normalize_limits({'rateLimits': {'primary': {'usedPercent': 99}},
            'rateLimitsByLimitId': {'codex': {'primary': {'usedPercent': 12}}}}, self.now)
        self.assertEqual(snapshot['limits'][0]['used_percent'], 12)

    def test_missing_and_invalid_values_are_unavailable_not_zero(self):
        for value in (None, True, '20', float('nan')):
            result = normalize_limits({'rateLimits': {'primary': {'usedPercent': value}}}, self.now)
            self.assertEqual(result['limits'], [])
        with self.assertRaises(ValueError): normalize_limits([])

    def test_snapshot_persists_and_becomes_stale_at_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'usage.sqlite3'
            write_snapshot(path, {'rateLimits': {'primary': {'usedPercent': 20}}}, self.now)
            with closing(sqlite3.connect(path)) as db:
                self.assertFalse(read_snapshot(db, max_age_seconds=60, now=self.now + timedelta(seconds=59))['stale'])
                self.assertTrue(read_snapshot(db, max_age_seconds=60, now=self.now + timedelta(seconds=60))['stale'])
                self.assertTrue(read_snapshot(db, now=self.now - timedelta(seconds=1))['stale'])

    def test_database_without_snapshot_is_supported(self):
        with closing(sqlite3.connect(':memory:')) as db:
            self.assertIsNone(read_snapshot(db))
