"""Read Codex account limits on a timer, without starting threads or model turns."""
import json
import logging
import os
from pathlib import Path
import select
import shutil
import sqlite3
import subprocess
import time

from .account_usage import write_snapshot
from .config import DEFAULT_ACCOUNT_REFRESH_INTERVAL, DEFAULT_LIMITS_REQUEST_TIMEOUT, positive_number

logger = logging.getLogger(__name__)
MAX_REPLY_BYTES = 1_000_000


def find_codex(command=None):
    if command:
        path = shutil.which(str(command)) or str(Path(command).expanduser())
        return path if os.path.isfile(path) and os.access(path, os.X_OK) else None
    return shutil.which('codex')


def read_reply(process, identifier, deadline, pending):
    while True:
        while b'\n' in pending:
            line, _, rest = pending.partition(b'\n')
            pending[:] = rest
            if not line.strip(): continue
            message = json.loads(line)
            if not isinstance(message, dict): raise ValueError('Invalid Codex reply')
            if message.get('id') != identifier: continue
            if 'error' in message: raise RuntimeError('Codex account limits unavailable; check CLI sign-in')
            result = message.get('result')
            if not isinstance(result, dict): raise ValueError('Invalid Codex limits result')
            return result
        remaining = deadline - time.monotonic()
        if remaining <= 0: raise TimeoutError('Codex account limits request timed out')
        ready, _, _ = select.select([process.stdout], [], [], remaining)
        if not ready: raise TimeoutError('Codex account limits request timed out')
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk: raise RuntimeError('Codex account limits process ended')
        pending.extend(chunk)
        if len(pending) > MAX_REPLY_BYTES: raise ValueError('Codex reply is too large')


def fetch_limits(command, codex_home, timeout=DEFAULT_LIMITS_REQUEST_TIMEOUT):
    positive_number(timeout, 'limits_request_timeout')
    pending = bytearray()
    deadline = time.monotonic() + timeout
    with subprocess.Popen([str(command), 'app-server', '--listen', 'stdio://'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            env={**os.environ, 'CODEX_HOME': str(codex_home)}, bufsize=0) as process:
        def send(message):
            process.stdin.write(json.dumps(message).encode() + b'\n')
            process.stdin.flush()
        try:
            send({'id': 1, 'method': 'initialize', 'params': {'clientInfo': {
                'name': 'codex_usage_tracker', 'title': 'Codex Usage Tracker', 'version': '0.1.0'}}})
            read_reply(process, 1, deadline, pending)
            send({'method': 'initialized', 'params': {}})
            send({'id': 2, 'method': 'account/rateLimits/read'})
            return read_reply(process, 2, deadline, pending)
        finally:
            if process.poll() is None:
                process.terminate()
                try: process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


def refresh_until_stopped(database, codex_home, command, stop, interval=DEFAULT_ACCOUNT_REFRESH_INTERVAL):
    positive_number(interval, 'account_refresh_interval')
    # Each attempt has a bounded timeout; the last successful value survives failures.
    while not stop.is_set():
        started = time.monotonic()
        try:
            payload = fetch_limits(command, codex_home)
            write_snapshot(database, payload, source='codex_app_server', refresh_interval_seconds=interval)
        except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError):
            logger.warning('Account limits refresh failed; check Codex CLI sign-in. Retrying next minute.')
        stop.wait(max(0, interval - (time.monotonic() - started)))
