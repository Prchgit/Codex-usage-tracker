# Codex Usage Tracker (CUT)

A local usage tracker for Codex on **macOS**. It shows recently active chats in a floating panel, keeps usage history, and displays new input, cached input, output tokens, and estimated credits per chat.

The panel appears while Codex or ChatGPT Work is foreground and hides when you switch apps. Tracking runs locally in the background. **No API key, MCP connection, or extra model requests are needed for ordinary tracking.**

## Try it from Codex

1. Clone this repository, or ask Codex to clone it:

   ```sh
   git clone https://github.com/Prchgit/Codex-usage-tracker.git
   ```

2. Open the cloned folder as a local project in Codex.
3. Send **“Activate CUT”**. The project's `AGENTS.md` tells Codex how to check prerequisites, install the collector and panel, and verify activation.

Opening the repository does not install anything by itself. Activation installs two user LaunchAgents and a local runtime under `~/.local/share/codex-token-monitor/`. No administrator privileges are needed for CUT itself.

**Prerequisites for this source-based trial:** macOS, Codex opened at least once, Python 3.9+, and Apple's command-line tools. If the tools are missing, run `xcode-select --install` once and complete Apple's installer. A bundled, signed installer is not included yet.

## Install directly

From the cloned folder:

```sh
python3 scripts/install.py
```

The installer checks prerequisites, installs both components, handles an occupied dashboard port, and verifies the local API. The usual history address is `http://127.0.0.1:8767/`; the installer prints the actual address. Use **−** to collapse the panel to its header and **＋** to expand it. This preference survives relaunches and login. If the panel is hidden, choose **◉ Tracker → Show / hide floating usage**.

MCP is **off by default**. To let an assistant query collected usage through tools:

```sh
python3 scripts/install.py --with-mcp
```

This optional mode also requires `uv` and the Codex CLI. It installs the Python MCP dependencies and registers `codex-usage-tracker` with Codex. Reopen the Codex chat to discover it. See [Codex MCP documentation](https://developers.openai.com/codex/mcp). The optional request-execution tools remain in offline demo mode unless separately configured for live OpenAI API use.

Submission hooks are also opt-in:

```sh
python3 scripts/install.py --with-hooks
```

Review and trust these hooks in Codex before using them. Collection works without hooks; hooks add a rough submission estimate, not the full assembled request's token count. Both flags can be combined.

## What the numbers mean

- **New:** input tokens minus cached input tokens.
- **Cache:** recorded cached input tokens.
- **Out:** recorded output tokens. Reasoning tokens are a subset, not added twice.
- **Credits:** cumulative estimates using the versioned Standard-speed rates in `credit_rates.json`. They update after a turn completes or is interrupted.
- **Partial:** some locally observed calls or turns could not be measured or priced. Unknown model rates are never guessed. Malformed usage is isolated and affected totals are marked partial; valid recorded calls remain available. When upgrading older databases with file-level diagnostics, the affected chat history is conservatively marked partial because the original rejected records were not attributed to individual turns.
- **Overall % used:** account limits refresh automatically every 60 seconds through the signed-in Codex CLI. No model turn or API key is required. Time windows are labeled by their duration, such as **5h** and **Weekly**, rather than Primary and Secondary; dashboard tooltips show reset times when available. If the CLI or sign-in is unavailable, the last successful value remains visible and becomes stale after two minutes. See [account usage](docs/account-usage.md).

History shows the latest 100 turns, but per-chat totals include all persisted turns. One turn may include multiple model calls and steered messages. Collection depends on Codex's local log format; remote/cloud activity and sessions without compatible local records are not covered. Showing the panel in Work does not guarantee that every Work request is measured.

## Options

```sh
python3 scripts/install.py --help
```

Use `--codex-home` for a custom Codex data location, `--runtime` for the tracker data directory, `--port` for a preferred port, `--poll-interval` for polling seconds, and `--since` for an ISO cutoff for previously unseen logs. `CODEX_HOME` and `CUT_RUNTIME_DIR` supply default paths. The installer retains existing history when run again.

## Update or uninstall

To update, pull the latest code and run the installer again, including any optional flags you want enabled:

```sh
git pull
python3 scripts/install.py
```

To uninstall the tracker while retaining usage history and diagnostic logs:

```sh
python3 scripts/uninstall.py
```

Use `--dry-run` to preview the changes. For a custom installation, supply the same `--runtime` and `--codex-home` paths used during installation. The command stops and removes CUT's two LaunchAgents and installed program files, removes its owned hooks and optional MCP registration, and preserves unrelated configuration. Removing an optional MCP registration requires the Codex CLI. Usage databases and logs remain in the runtime directory for a later reinstall.

## Privacy and trial limitations

The collector stores usage metadata rather than prompt text. Chat titles are read from local Codex metadata, and panel diagnostics can contain titles. The combined installer restricts the runtime directory to its owner. The dashboard binds only to loopback, but its local API does not yet authenticate callers: other local processes can access usage metadata. Use this trial on a trusted personal machine.

No databases, account snapshots, logs, credentials, or local conversation records are included in this repository. Optional request-execution tools store supplied prompts and responses in their own local database. Windows/Linux installers, bundled runtimes, and signing/notarization remain future work.

## Development and verification

Python 3.10+ and `uv` are required for the full development environment:

```sh
uv sync
uv run python -m unittest discover -s tests -v
uv run python tests/mcp_smoke.py
node tests/dashboard_smoke.cjs
clang -fobjc-arc -fmodules native/UsagePanel.m native/ScreenDetection.m \
  -o /tmp/CodexUsagePanel-test -framework Cocoa
/tmp/CodexUsagePanel-test --self-test
```

Tests use synthetic fixtures and offline/mocked providers. No paid model calls are required. The existing collector was validated on macOS with local Codex records; compatibility with other versions needs testing.

Responsibilities are split between `monitor.py` (logs/storage), `usage.py` (aggregation), `web_server.py` (HTTP), `monitor_cli.py` (CLI/hooks), and the native panel/foreground modules. See `AGENTS.md` for project coding rules.
