"""Read-only loopback dashboard and lifecycle of its background collector."""
import json
import logging
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from .config import DEFAULT_PORT, DEFAULT_POLL_INTERVAL, positive_number
from .config import DEFAULT_ACCOUNT_REFRESH_INTERVAL
from .account_refresh import find_codex, refresh_until_stopped

logger = logging.getLogger(__name__)
LOOPBACK_HOST = '127.0.0.1'
ALLOWED_HOSTS = frozenset((LOOPBACK_HOST, 'localhost'))


def build_handler(monitor, page=None, poll_interval=DEFAULT_POLL_INTERVAL, panel_page=None):
    positive_number(poll_interval, 'poll_interval')
    if page is None:
        page = Path(__file__).with_name('dashboard.html').read_bytes()
    page = page.replace(b'__POLL_INTERVAL_MS__', str(round(poll_interval * 1000)).encode())
    if panel_page is None:
        panel_page = Path(__file__).with_name('usage_panel.html').read_bytes()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host', '').split(':')[0] not in ALLOWED_HOSTS:
                self.send_error(403)
                return
            route = urlparse(self.path)
            if route.path not in ('/', '/panel', '/api/turns'):
                self.send_error(404)
                return
            is_api = route.path == '/api/turns'
            try:
                thread_id = parse_qs(route.query).get('thread_id', [None])[0]
                body = json.dumps(monitor.reports(thread_id=thread_id)).encode() if is_api else panel_page if route.path == '/panel' else page
            except (sqlite3.Error, ValueError, TypeError):
                logger.error('Cannot build usage report; collector data may be unavailable')
                self.send_error(503, 'Usage report temporarily unavailable')
                return
            self.send_response(200)
            self.send_header('Content-Type', 'application/json' if is_api else 'text/html; charset=utf-8')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                logger.debug('Dashboard client disconnected')

        def log_message(self, *args):
            # Avoid logging URLs, identifiers or returned chat metadata.
            pass

    return Handler


def collect_until_stopped(monitor, since, stop, poll_interval):
    while not stop.is_set():
        try:
            monitor.scan(since)
        except (OSError, sqlite3.Error, ValueError):
            logger.warning('Collector scan failed; retrying at the next poll')
        stop.wait(poll_interval)


def serve(monitor, port=DEFAULT_PORT, since=0, poll_interval=DEFAULT_POLL_INTERVAL,
          codex_command=None, account_refresh_interval=DEFAULT_ACCOUNT_REFRESH_INTERVAL):
    positive_number(poll_interval, 'poll_interval')
    positive_number(account_refresh_interval, 'account_refresh_interval')
    if type(port) is not int or not 1 <= port <= 65535:
        raise ValueError('Port must be between 1 and 65535')
    stop = threading.Event()
    # Bind before starting the worker so a port conflict cannot leave a stray scanner.
    with ThreadingHTTPServer((LOOPBACK_HOST, port), build_handler(monitor, poll_interval=poll_interval)) as server:
        worker = threading.Thread(target=collect_until_stopped,
            args=(monitor, since, stop, poll_interval), daemon=True)
        worker.start()
        command = find_codex(codex_command)
        account_worker = None
        if command:
            account_worker = threading.Thread(target=refresh_until_stopped,
                args=(monitor.database, monitor.sessions.parent, command, stop, account_refresh_interval), daemon=True)
            account_worker.start()
        else:
            logger.warning('Codex executable unavailable; account limits cannot refresh automatically')
        try:
            server.serve_forever()
        finally:
            stop.set()
            worker.join()
            if account_worker: account_worker.join()
