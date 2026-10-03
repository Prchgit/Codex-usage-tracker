"""Read-only MCP tools for the local collector; no generation or prompt snapshots."""
import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode, urlparse
from urllib.request import urlopen
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from .config import DEFAULT_PORT, PANEL_HOSTNAME, runtime_directory

MAX_REPORT_BYTES = 32 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 4
CHAT_FIELDS = ('thread_id', 'turn_id', 'chat_name', 'model', 'reasoning_effort', 'status',
    'recorded_turns', 'model_calls', 'first_recorded_at', 'last_activity_at', 'usage',
    'estimated_credits', 'known_estimated_credits', 'credit_coverage_complete',
    'missing_usage_turns', 'unsupported_usage_turns', 'unknown_credit_calls', 'pending_turns',
    'credit_priced_calls', 'usage_coverage_complete')
TURN_FIELDS = ('turn_id', 'thread_id', 'started', 'status', 'model', 'reasoning_effort',
    'model_calls', 'usage', 'usage_coverage_complete', 'estimated_credits',
    'known_estimated_credits', 'unknown_credit_calls', 'credits_pending')


def validate_dashboard_url(value):
    url = urlparse(value)
    if url.scheme != 'http' or url.hostname not in ('127.0.0.1', 'localhost') or url.username or url.password or url.path not in ('', '/') or url.query or url.fragment:
        raise ValueError('Dashboard must be a plain HTTP loopback URL')
    if url.port is not None and not 1 <= url.port <= 65535: raise ValueError('Invalid dashboard port')
    return value.rstrip('/') + '/'


def dashboard_url(runtime):
    manifest = Path(runtime) / 'installation.json'
    try: port = json.loads(manifest.read_text()).get('port', DEFAULT_PORT)
    except FileNotFoundError: port = DEFAULT_PORT
    if type(port) is not int or not 1 <= port <= 65535: raise ValueError('Invalid installed dashboard port')
    return f'http://127.0.0.1:{port}/'


class UsageReader:
    def __init__(self, url, fetch=None):
        self.url = validate_dashboard_url(url)
        self.fetch = fetch

    def report(self, thread_id=None):
        endpoint = self.url + 'api/turns' + ('?' + urlencode({'thread_id':thread_id}) if thread_id else '')
        try:
            if self.fetch is not None:
                report = self.fetch(endpoint)
            else:
                with urlopen(endpoint, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    raw = response.read(MAX_REPORT_BYTES + 1)
                    if len(raw) > MAX_REPORT_BYTES: raise ValueError('Report exceeds size limit')
                    report = json.loads(raw)
            if not isinstance(report, dict) or not isinstance(report.get('chats'), list) or not isinstance(report.get('turns'), list):
                raise ValueError('Unsupported collector report')
            return report
        except (OSError, ValueError, TypeError) as error:
            raise ValueError('Tracker unavailable. Start the local Codex Usage Tracker collector, then retry.') from error

    @staticmethod
    def visible(row):
        return not row.get('is_internal') and row.get('model') != 'codex-auto-review'

    @staticmethod
    def limit(value):
        if type(value) is not int or not 1 <= value <= 100: raise ValueError('Limit must be between 1 and 100')
        return value

    def list_chats(self, query=None, limit=20):
        self.limit(limit)
        if query is not None and not isinstance(query, str): raise ValueError('Query must be text')
        report = self.report()
        rows = [row for row in report['chats'] if self.visible(row) and
            (not query or query.casefold() in (row.get('chat_name') or '').casefold())]
        rows.sort(key=lambda row:row.get('last_activity_at') or '', reverse=True)
        return {'chats':[{k:row.get(k) for k in CHAT_FIELDS} for row in rows[:limit]],
            'matching_chats':len(rows), 'truncated':len(rows)>limit, 'coverage':report.get('coverage'),
            'updated_at':report.get('updated_at'), 'scope':'Cumulative locally recorded chat totals'}

    def chat_usage(self, thread_id, limit=20):
        self.limit(limit)
        if not isinstance(thread_id, str) or not thread_id.strip(): raise ValueError('Supply an exact chat thread ID')
        report = self.report(thread_id)
        chat = next((row for row in report['chats'] if row.get('thread_id') == thread_id and self.visible(row)), None)
        if chat is None: raise ValueError('Chat not found in visible local history. Use list_usage_chats to locate it.')
        turns = [row for row in report['turns'] if row.get('thread_id') == thread_id and self.visible(row)]
        return {'chat':{k:chat.get(k) for k in CHAT_FIELDS},
            'recent_turns':[{k:row.get(k) for k in TURN_FIELDS} for row in turns[:limit]],
            'returned_turns':min(limit,len(turns)), 'history_limit':limit,
            'history_truncated':chat.get('recorded_turns',0)>min(limit,len(turns)),
            'coverage':report.get('coverage'), 'updated_at':report.get('updated_at'),
            'scope':'Chat totals include all persisted turns; recent_turns is a bounded history page'}

    def compare(self, thread_ids):
        if not isinstance(thread_ids, list) or not 2 <= len(thread_ids) <= 20 or any(not isinstance(i,str) or not i for i in thread_ids):
            raise ValueError('Supply two to twenty exact chat thread IDs')
        if len(set(thread_ids)) != len(thread_ids): raise ValueError('Chat IDs must be distinct')
        report = self.report()
        by_id = {row.get('thread_id'):row for row in report['chats'] if self.visible(row)}
        if any(identifier not in by_id for identifier in thread_ids): raise ValueError('One or more chats are unavailable; use list_usage_chats first')
        return {'chats':[{k:by_id[identifier].get(k) for k in CHAT_FIELDS} for identifier in thread_ids],
            'coverage':report.get('coverage'), 'updated_at':report.get('updated_at'),
            'scope':'Cumulative recorded totals, potentially covering different time periods'}


def build_server(reader):
    server = FastMCP('Codex Usage Tracker', instructions='Read local recorded usage only. Credits are estimates; preserve partial and stale markers. Never infer that the latest chat is the current chat.')
    def result(data):
        return CallToolResult(content=[TextContent(type='text',text=json.dumps(data))], structuredContent=data)
    readonly = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False)

    panel_uri = 'ui://codex-usage-tracker/usage-panel.html'

    @server.resource(panel_uri, mime_type='text/html;profile=mcp-app',
        meta={'ui':{'prefersBorder':False}, 'openai/ui':{'availableDisplayModes':['inline']}})
    def usage_panel_html() -> str:
        return Path(__file__).with_name('usage_panel.html').read_text()

    def panel_data():
        report = reader.report()
        rows = [row for row in report.get('floating_sessions', []) if reader.visible(row)]
        return {'sessions':[{k:row.get(k) for k in CHAT_FIELDS} for row in rows[:20]],
            'truncated':len(rows)>20, 'account_usage':report.get('account_usage'),
            'updated_at':report.get('updated_at'), 'coverage':report.get('coverage')}

    @server.tool(annotations=readonly, meta={'ui':{'visibility':['app']}})
    def read_usage_panel() -> CallToolResult:
        """Refresh the compact Codex Usage Tracker panel's local usage without remounting the UI."""
        return result(panel_data())

    @server.tool(annotations=readonly, meta={'ui':{'resourceUri':panel_uri},
        'openai/outputTemplate':panel_uri})
    def show_usage_panel() -> CallToolResult:
        """Display the original Codex Usage Tracker account header and active-chat usage as an inline live card."""
        return result(panel_data())

    @server.tool(annotations=readonly)
    def list_usage_chats(query: str | None = None, limit: int = 20) -> CallToolResult:
        """Find local chats by title and return cumulative totals. Internal approval sessions are excluded."""
        return result(reader.list_chats(query, limit))

    @server.tool(annotations=readonly)
    def get_chat_usage(thread_id: str, limit: int = 20) -> CallToolResult:
        """Read one exact chat's cumulative usage and up to 100 recent turns. Never guess the current chat ID."""
        return result(reader.chat_usage(thread_id, limit))

    @server.tool(annotations=readonly)
    def compare_chat_usage(thread_ids: list[str]) -> CallToolResult:
        """Compare cumulative recorded tokens and estimated credits for two to twenty exact chat IDs."""
        return result(reader.compare(thread_ids))

    @server.tool(annotations=readonly)
    def get_account_usage() -> CallToolResult:
        """Read cached account limits, including window labels, reset times and staleness. No account/network refresh."""
        report = reader.report()
        return result({'account_usage':report.get('account_usage'),'updated_at':report.get('updated_at'),
            'scope':'Account-wide reported limits; distinct from chat credit estimates'})

    @server.tool(annotations=readonly)
    def get_usage_dashboard(thread_id: str | None = None) -> CallToolResult:
        """Return the loopback dashboard URL for opening in the host's browser panel. This does not open an external browser."""
        parameters = {'embed':'1'}
        if thread_id is not None:
            reader.chat_usage(thread_id, 1)
            parameters['thread_id'] = thread_id
        display_url = reader.url.replace('127.0.0.1', PANEL_HOSTNAME).replace('://localhost', '://' + PANEL_HOSTNAME)
        return result({'url':display_url + '?' + urlencode(parameters),
            'title':'Codex Usage Tracker','preferred_surface':'in-app browser panel',
            'local_only':True})
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=runtime_directory())
    parser.add_argument('--url',help='Override the installed loopback dashboard URL')
    args = parser.parse_args(argv)
    build_server(UsageReader(args.url or dashboard_url(args.runtime))).run(transport='stdio')


if __name__ == '__main__': main()
