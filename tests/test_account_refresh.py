"""Account RPC and timer behavior without external services or model calls."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from token_budget_mcp.account_refresh import fetch_limits, refresh_until_stopped


class FakeStop:
    def __init__(self): self.waits = []
    def is_set(self): return len(self.waits) >= 2
    def wait(self, seconds): self.waits.append(seconds)


class AccountRefreshTests(unittest.TestCase):
    def server(self, root, error=False):
        script = root / 'fake-codex'
        script.write_text('#!' + sys.executable + '\n' + '''
import sys,json
for line in sys.stdin:
    request=json.loads(line)
    if request.get('method')=='initialize':
        print(json.dumps({'id':1,'result':{}}),flush=True)
        print(json.dumps({'method':'notification','params':{}}),flush=True)
    elif request.get('method')=='account/rateLimits/read':
        print(json.dumps(REPLY),flush=True)
    elif request.get('method')=='initialized': pass
    else: raise RuntimeError('Unexpected RPC method')
'''.replace('REPLY', repr({'id': 2, 'error': {'message': 'private-error-text'}} if error else {
            'id': 2, 'result': {'rateLimits': {'primary': {'usedPercent': 30}}}})))
        script.chmod(0o700)
        return script

    def test_protocol_only_initializes_and_reads_account_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            payload = fetch_limits(self.server(root), root, timeout=3)
            self.assertEqual(payload['rateLimits']['primary']['usedPercent'], 30)

    def test_rpc_error_does_not_expose_private_error_details(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaises(RuntimeError) as error:
                fetch_limits(self.server(root, error=True), root, timeout=3)
            self.assertNotIn('private-error-text', str(error.exception))

    def test_hung_process_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / 'hung-codex'
            script.write_text('#!' + sys.executable + '\nimport time\ntime.sleep(30)\n')
            script.chmod(0o700)
            with self.assertRaises(TimeoutError): fetch_limits(script, root, timeout=0.1)

    def test_refresh_runs_every_sixty_seconds(self):
        stop = FakeStop()
        with patch('token_budget_mcp.account_refresh.time.monotonic', return_value=0), \
             patch('token_budget_mcp.account_refresh.fetch_limits', return_value={}) as fetch, \
             patch('token_budget_mcp.account_refresh.write_snapshot') as write:
            refresh_until_stopped('/db', '/codex', '/binary', stop)
        self.assertEqual(stop.waits, [60, 60])
        self.assertEqual(fetch.call_count, 2)
        self.assertEqual(write.call_count, 2)
        self.assertTrue(write.call_args.kwargs['refresh_interval_seconds'] == 60)
        self.assertEqual(write.call_args.kwargs['source'], 'codex_app_server')

    def test_failure_keeps_cached_value_and_retries(self):
        stop = FakeStop()
        with patch('token_budget_mcp.account_refresh.time.monotonic', return_value=0), \
             patch('token_budget_mcp.account_refresh.fetch_limits', side_effect=[RuntimeError('private'), {}]), \
             patch('token_budget_mcp.account_refresh.write_snapshot') as write, \
             self.assertLogs('token_budget_mcp.account_refresh', level='WARNING') as logs:
            refresh_until_stopped('/db', '/codex', '/binary', stop)
        self.assertEqual(write.call_count, 1)
        self.assertNotIn('private', str(logs.output))
        self.assertEqual(stop.waits, [60, 60])


if __name__ == '__main__': unittest.main()
