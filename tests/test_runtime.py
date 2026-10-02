"""Behavior at configuration, HTTP, scan and installation boundaries."""
import copy
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from token_budget_mcp.config import positive_number
from token_budget_mcp.core import BudgetService, DemoProvider
from token_budget_mcp.monitor import Monitor, main
from token_budget_mcp.usage import floating_sessions
from token_budget_mcp.web_server import build_handler, collect_until_stopped

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_monitor import configure_hooks, main as install_monitor


class FakeSocket:
    def __init__(self, request):
        self.request = request
        self.response = bytearray()

    def makefile(self, *args):
        return io.BytesIO(self.request)

    def sendall(self, data):
        self.response.extend(data)


def request(handler, target, host='localhost'):
    connection = FakeSocket(f'GET {target} HTTP/1.0\r\nHost: {host}\r\n\r\n'.encode())
    handler(connection, ('127.0.0.1', 1234), Mock(server_name='localhost', server_port=8767))
    headers, body = bytes(connection.response).split(b'\r\n\r\n', 1)
    return headers.decode(), body


class RuntimeTests(unittest.TestCase):
    def test_invalid_numeric_settings(self):
        for value in (True, 0, -1, float('nan'), float('inf'), '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                positive_number(value, 'interval')
        self.assertEqual(positive_number(0.5, 'interval'), 0.5)

    def test_configurable_inactivity_boundary(self):
        from datetime import datetime, timezone
        now = datetime(2026, 10, 2, 13, 5, tzinfo=timezone.utc)
        rows = [dict(turn_id='recent', thread_id='a', started='2026-10-02T13:04:59Z'),
                dict(turn_id='older', thread_id='b', started='2026-10-02T13:04:30Z')]
        self.assertEqual(len(floating_sessions(rows, now, inactivity_seconds=31)), 2)
        self.assertEqual(len(floating_sessions(rows, now, inactivity_seconds=30)), 1)

    def test_query_route_is_json_and_filter_is_decoded(self):
        monitor = Mock()
        monitor.reports.return_value = {'turns': []}
        headers, body = request(build_handler(monitor), '/api/turns?thread_id=chat%20one')
        self.assertIn('200 OK', headers)
        self.assertIn('Content-Type: application/json', headers)
        self.assertEqual(json.loads(body), {'turns': []})
        monitor.reports.assert_called_once_with(thread_id='chat one')

    def test_http_rejects_unknown_hosts_and_routes(self):
        monitor = Mock()
        handler = build_handler(monitor)
        self.assertIn('403', request(handler, '/api/turns', 'attacker.example')[0])
        self.assertIn('404', request(handler, '/unknown')[0])
        monitor.reports.assert_not_called()

    def test_http_failure_does_not_expose_exception_details(self):
        monitor = Mock()
        monitor.reports.side_effect = ValueError('private exception contents')
        with self.assertLogs('token_budget_mcp.web_server', level='ERROR'):
            headers, body = request(build_handler(monitor), '/api/turns')
        self.assertIn('503', headers)
        self.assertNotIn(b'private exception contents', body)

    def test_dashboard_polling_configuration(self):
        headers, page = request(build_handler(Mock(), poll_interval=0.75), '/')
        self.assertIn('200 OK', headers)
        self.assertIn(b'const POLL_INTERVAL_MS = 750;', page)
        self.assertNotIn(b'__POLL_INTERVAL_MS__', page)

    def test_compact_panel_route_serves_reused_ui(self):
        monitor = Mock()
        headers, page = request(build_handler(monitor), '/panel')
        self.assertIn('200 OK', headers)
        self.assertIn(b'Codex Usage Tracker', page)
        self.assertIn(b"location.pathname==='/panel'", page)
        monitor.reports.assert_not_called()
        self.assertIn('403', request(build_handler(monitor), '/panel', 'attacker.example')[0])

    def test_scan_failure_retries_until_stop(self):
        stop = threading.Event()
        monitor = Mock()
        def scan(since):
            if monitor.scan.call_count == 1: raise OSError('unreadable log')
            stop.set()
        monitor.scan.side_effect = scan
        with self.assertLogs('token_budget_mcp.web_server', level='WARNING'):
            collect_until_stopped(monitor, 0, stop, 0.001)
        self.assertEqual(monitor.scan.call_count, 2)

    def test_malformed_log_does_not_prevent_valid_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / 'rollout-test.jsonl'
            log.write_text('[]\n' + json.dumps({'type': 'token_usage_record', 'payload': {
                'turn_id': 'turn', 'response_id': 'call', 'usage': {'input_tokens': 10, 'output_tokens': 2}}}) + '\n')
            monitor = Monitor(root / 'db.sqlite3', root)
            try:
                monitor.scan()
                report = monitor.reports()
                self.assertEqual(report['diagnostics'], 1)
                self.assertEqual(report['turns'][0]['model_calls'], 1)
                for event in ([], {'turn_id': 'turn', 'prompt': 123}):
                    with self.assertRaises(ValueError): monitor.submission(event)
            finally: monitor.close()

    def test_invalid_cli_options_fail_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'unused.sqlite3'
            for options in (['--port', '0'], ['--poll-interval', 'nan'], ['--since', 'not-a-date']):
                with patch('sys.stderr', io.StringIO()), self.assertRaises(SystemExit):
                    main(['--database', str(path)] + options)
                self.assertFalse(path.exists())

    def test_invalid_pricing_fails_before_database_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            prices = root / 'prices.json'
            prices.write_text('[]')
            with self.assertRaisesRegex(ValueError, 'Pricing configuration'):
                Monitor(root / 'unused.sqlite3', root, prices=prices)
            self.assertFalse((root / 'unused.sqlite3').exists())

    def test_readonly_database_path_with_reserved_uri_characters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'usage #1?.sqlite3'
            monitor = Monitor(path, root)
            monitor.close()
            reader = Monitor(path, root, readonly=True)
            try: self.assertEqual(reader.reports()['turns'], [])
            finally: reader.close()

    def test_preview_validation_and_configured_expiry(self):
        service = BudgetService(':memory:', DemoProvider(), preview_ttl_seconds=5)
        try:
            for messages in (None, 'text', [None], [1]):
                with self.assertRaises(ValueError): service.preview(messages)
            with self.assertRaises(ValueError): service.preview([{'role': 'user', 'content': 'text'}], output_min=True)
            with patch('token_budget_mcp.core.time.time', return_value=100):
                preview = service.preview([{'role': 'user', 'content': 'text'}])
            with patch('token_budget_mcp.core.time.time', return_value=106), self.assertRaisesRegex(ValueError, 'expired'):
                service.execute(preview['preview_id'], 'gpt-4.1', True)
        finally: service.close()


class InstallerTests(unittest.TestCase):
    def test_hook_upgrade_is_idempotent_and_preserves_unrelated_commands(self):
        old = {'type': 'command', 'command': '/usr/bin/python3 /private/runtime/run_monitor.py --hook'}
        other = {'type': 'command', 'command': 'echo other'}
        config = {'hooks': {'Stop': [{'hooks': [old, other]}]}}
        command = '/usr/bin/python3 /private/runtime/run_monitor.py --port 9000 --hook'
        configure_hooks(config, command)
        first = copy.deepcopy(config)
        configure_hooks(config, command)
        self.assertEqual(config, first)
        commands = [h['command'] for entry in config['hooks']['Stop'] for h in entry['hooks']]
        self.assertEqual(commands, ['echo other', command])

    def test_conflicting_agent_is_rejected_before_any_runtime_write(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / 'runtime'
            with patch('install_monitor.read_plist', return_value={'WorkingDirectory': '/unrelated'}), \
                 self.assertRaisesRegex(ValueError, 'unrelated'):
                install_monitor(['--runtime', str(runtime), '--python', sys.executable])
            self.assertFalse(runtime.exists())

    def test_installer_copies_modules_and_propagates_options(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch('install_monitor.read_plist', return_value=None), \
                 patch('install_monitor.activate_agent') as activate, patch('builtins.print'):
                install_monitor(['--runtime', str(root / 'runtime'), '--codex-home', str(root / 'codex'),
                    '--python', sys.executable, '--port', '9001', '--poll-interval', '0.5'])
            data = activate.call_args.args[1]
            arguments = data['ProgramArguments']
            self.assertEqual(arguments[arguments.index('--port') + 1], '9001')
            self.assertEqual(arguments[arguments.index('--poll-interval') + 1], '0.5')
            for module in ('config.py', 'usage.py', 'web_server.py'):
                self.assertTrue((root / 'runtime/token_budget_mcp' / module).exists())


if __name__ == '__main__':
    unittest.main()
