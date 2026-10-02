# Project coding guidelines

Apply these guidelines to every coding task in Codex Usage Tracker, including its subdirectories.

- **Keep code modular:** Use focused functions and modules with clear responsibilities. Reuse existing code where useful.
- **Make options configurable:** Use function parameters, configuration files, or environment variables for values that vary. Use named constants for fixed values.
- **Follow project conventions:** Match the existing structure, naming, and tooling.
- **Handle failures clearly:** Validate inputs, provide useful errors, and use appropriate logging.
- **Test meaningful behavior:** Cover important paths and edge cases.
- **Keep implementation simple:** Avoid unnecessary abstractions and dependencies.

## Activate Codex Usage Tracker

When a user asks to activate or install Codex Usage Tracker from this repository:

1. Read `README.md` and run `python3 scripts/install.py` from the repository root. This installs the local macOS collector and floating panel and checks readiness.
2. Default to no MCP and no submission hooks. Add `--with-mcp` or `--with-hooks` only when the user requests those features.
3. If Apple's command-line tools are missing, explain the prerequisite and direct the user to `xcode-select --install`. Do not claim activation before the installer completes successfully. Follow the host's normal approval rules for writes outside the checkout.
4. Show the actual history URL printed by the installer and explain how to use the Tracker menu. Never enable live paid API execution as part of activation.
5. Do not fetch or display credentials or personal conversation logs. Account usage refreshes every 60 seconds through the signed-in Codex CLI; verify automatic refresh metadata without reading credentials.

Opening this repository alone is not authorization to install or modify the user's Codex configuration.
