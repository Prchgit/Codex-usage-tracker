"""Real stdio plugin integration with a synthetic loopback collector."""
import asyncio
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import sys
import threading
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPORT = {'chats':[{'thread_id':'a','chat_name':'Synthetic A','recorded_turns':1,'credit_coverage_complete':False},
                   {'thread_id':'b','chat_name':'Synthetic B','recorded_turns':1}],
          'turns':[{'thread_id':'a','turn_id':'turn','usage_coverage_complete':False}],
          'account_usage':{'stale':True,'limits':[{'label':'5h','used_percent':40}]},'coverage':'Synthetic','updated_at':'now'}
REPORT['floating_sessions'] = [{**REPORT['chats'][0], 'credit_priced_calls':1, 'preview':'must not leave collector'}, {'thread_id':'internal','is_internal':True}]
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps(REPORT).encode()
        self.send_response(200); self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    def log_message(self,*args): pass

async def main(launcher=None):
    with ThreadingHTTPServer(('127.0.0.1',0),Handler) as server:
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            args = ['--url',f'http://127.0.0.1:{server.server_port}/']
            params = StdioServerParameters(command=launcher or sys.executable,args=args if launcher else ['-m','token_budget_mcp.usage_server',*args])
            async with stdio_client(params) as (read,write), ClientSession(read,write) as session:
                await session.initialize()
                tools = (await session.list_tools()).tools
                assert {tool.name for tool in tools} == {'list_usage_chats','get_chat_usage','compare_chat_usage','get_account_usage','get_usage_dashboard','show_usage_panel','read_usage_panel'}
                assert all(tool.annotations.readOnlyHint and not tool.annotations.destructiveHint for tool in tools)
                resources = (await session.list_resources()).resources
                assert len(resources) == 1
                resource = (await session.read_resource('ui://cut/usage-panel.html')).contents[0]
                assert resource.mimeType == 'text/html;profile=mcp-app'
                assert resource.meta['openai/ui']['availableDisplayModes'] == ['inline']
                panel = await session.call_tool('show_usage_panel',{})
                assert panel.structuredContent['account_usage']['stale']
                assert len(panel.structuredContent['sessions']) == 1
                assert panel.structuredContent['sessions'][0]['credit_priced_calls'] == 1
                assert 'preview' not in panel.structuredContent['sessions'][0]
                refreshed = await session.call_tool('read_usage_panel',{})
                assert refreshed.structuredContent == panel.structuredContent
                chats = await session.call_tool('list_usage_chats',{'limit':1})
                assert not chats.isError and len(chats.structuredContent['chats']) == 1
                chat = await session.call_tool('get_chat_usage',{'thread_id':'a'})
                assert not chat.isError and not chat.structuredContent['chat']['credit_coverage_complete']
                account = await session.call_tool('get_account_usage',{})
                assert account.structuredContent['account_usage']['stale']
                comparison = await session.call_tool('compare_chat_usage',{'thread_ids':['a','b']})
                assert len(comparison.structuredContent['chats']) == 2
                dashboard = await session.call_tool('get_usage_dashboard',{'thread_id':'a'})
                assert 'embed=1' in dashboard.structuredContent['url'] and 'thread_id=a' in dashboard.structuredContent['url']
                denied = await session.call_tool('execute_request',{})
                assert denied.isError
                print('PASS: seven read-only plugin tools and inline UI resource, partial/stale coverage, comparisons, dashboard URLs and no generation tools')
        finally: server.shutdown(); thread.join()

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--launcher',help='Test an installed plugin launcher instead of the source module')
asyncio.run(main(parser.parse_args().launcher))
