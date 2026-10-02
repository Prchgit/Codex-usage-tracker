# Automatic account usage refresh

Overall % used refreshes immediately on startup and every 60 seconds while the collector runs. It uses the Codex CLI's read-only `account/rateLimits/read` method through [Codex app-server](https://developers.openai.com/codex/app-server). It starts no model turns and consumes no inference tokens.

The installer detects `codex` on PATH and saves its executable path for the background service. The CLI needs an existing ChatGPT-backed sign-in in the configured Codex home. To select another executable, install with `python3 scripts/install.py --codex-command /path/to/codex`. The collector's `--account-refresh-interval` option controls the interval in seconds; the default is 60.

Failed refreshes preserve the last successful reading and retry on the next scheduled refresh. Readings become stale after two minutes. Without a working CLI/sign-in, ordinary local token tracking still works and account usage remains unavailable or stale.

Primary and secondary limit windows remain separate. Missing values show unavailable. The cache stores normalized percentages, window/reset metadata, and fetch time rather than account IDs, credentials, or raw spending values.

## Manual fallback

Codex's built-in `get_usage_limits` tool is callable inside a conversation, rather than by the background panel. Save only its rate-limit fields to a local JSON file outside the repository and import that response:

```sh
uv run python -m token_budget_mcp.account_usage --database "$HOME/.local/share/codex-token-monitor/monitor.sqlite3" < /path/to/usage-limits.json
```

A successful automatic refresh replaces this manual snapshot.
