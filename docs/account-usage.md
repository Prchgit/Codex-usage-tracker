# Optional account usage snapshot

Ask Codex to call its built-in `get_usage_limits` tool, then save only its rate-limit fields to a local JSON file outside the repository. Feed that response to:

```sh
uv run python -m token_budget_mcp.account_usage --database "$HOME/.local/share/codex-token-monitor/monitor.sqlite3" < /path/to/usage-limits.json
```

For an individual limit, used percentage is `100 - remainingPercent`. Primary and secondary windows remain separate. Missing values show unavailable. The cache stores normalized percentages, window/reset metadata, and fetch time rather than account IDs, credentials, or raw spending values. Snapshots are marked stale after 15 minutes.

The built-in tool is callable inside a Codex conversation; the background panel cannot invoke it directly. This feature is a manual bridge, not automatic refresh. Ordinary local token tracking works without it.
