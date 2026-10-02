"""Local stdio MCP interface."""
import argparse
import json
from pathlib import Path
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from .core import BudgetService, DemoProvider, OpenAIProvider
from .config import runtime_directory


def build_server(service, monitor_database=None, sessions=None):
    mcp = FastMCP('Token Budget')

    def result(data):
        return CallToolResult(content=[TextContent(type='text', text=data.get('view', json.dumps(data)))],
                              structuredContent=data)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False))
    def preview_request(messages: list[dict[str,str]], selected_model: str = 'gpt-4.1',
                        task_type: str = 'general', output_min: int = 128, output_max: int = 512) -> CallToolResult:
        """Preview supplied text only. Save snapshot; estimate costs and suggest a model. No generation."""
        return result(service.preview(messages,selected_model,task_type,output_min,output_max))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True))
    def execute_request(preview_id: str, model: str, confirmed: bool = False) -> CallToolResult:
        """Execute reviewed snapshot after user selects model. Live mode incurs API charges. Do not infer confirmation."""
        return result(service.execute(preview_id,model,confirmed))

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_request_report(request_id: str) -> CallToolResult:
        """Retrieve existing report without making another model call."""
        data = service.get(request_id)
        if not request_id.startswith('r_'):
            raise ValueError('Expected execution request ID')
        return result(data)

    @mcp.tool(annotations=ToolAnnotations(readOnlyHint=True))
    def get_codex_usage() -> CallToolResult:
        """Read automatically observed recent local Codex turns. No model calls. Costs may be unavailable."""
        from .monitor import Monitor
        monitor = Monitor(monitor_database or runtime_directory()/'monitor.sqlite3',
                          sessions or Path.home()/'.codex/sessions', readonly=True)
        try:
            return result(monitor.reports())
        finally:
            monitor.close()

    @mcp.resource('budget://catalog')
    def catalog() -> str:
        """Configured model pricing and source references."""
        return json.dumps(service.catalog)

    @mcp.prompt()
    def preview_then_execute() -> str:
        return ('Ask for request content and output allowance. Call preview_request, show its view and model tradeoff. '
                'Wait for explicit user selection before execute_request with confirmed=true. '
                'Show answer and usage report. Never claim this measures host application usage.')
    return mcp


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='Use OpenAI API; default is an offline demo')
    parser.add_argument('--database', default='token-budget.sqlite3')
    parser.add_argument('--catalog')
    parser.add_argument('--monitor-database', help='Observed Codex usage database path')
    parser.add_argument('--sessions', help='Local Codex sessions directory')
    args = parser.parse_args()
    service = BudgetService(Path(args.database), OpenAIProvider() if args.live else DemoProvider(), args.catalog)
    try:
        build_server(service, args.monitor_database, args.sessions).run(transport='stdio')
    finally:
        service.close()

if __name__ == '__main__':
    main()
