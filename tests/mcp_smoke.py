"""Actual stdio MCP client integration, without paid API calls."""
import asyncio
import sys
import tempfile
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

async def main():
    with tempfile.TemporaryDirectory() as directory:
        params=StdioServerParameters(command=sys.executable,args=['-m','token_budget_mcp.server','--database',str(Path(directory)/'test.sqlite3')])
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names={tool.name for tool in (await session.list_tools()).tools}
                assert names=={'preview_request','execute_request','get_request_report','get_codex_usage'},names
                assert len((await session.list_resources()).resources)==1
                assert len((await session.list_prompts()).prompts)==1
                preview=await session.call_tool('preview_request',{'messages':[{'role':'user','content':'Extract Singapore.'}],'task_type':'extraction'})
                assert not preview.isError,preview
                data=preview.structuredContent
                denied=await session.call_tool('execute_request',{'preview_id':data['preview_id'],'model':'gpt-4.1-mini'})
                assert denied.isError
                result=await session.call_tool('execute_request',{'preview_id':data['preview_id'],'model':'gpt-4.1-mini','confirmed':True})
                assert not result.isError,result
                report=await session.call_tool('get_request_report',{'request_id':result.structuredContent['request_id']})
                assert report.structuredContent['usage_basis']=='simulated'
                print('PASS: stdio initialization, discovery, preview, confirmation gate, execution and report')

asyncio.run(main())
