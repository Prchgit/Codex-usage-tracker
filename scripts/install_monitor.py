"""Install reversible user-level watcher and hooks. Does not alter hook trust."""
import json
from pathlib import Path
import shlex
import shutil
from install_common import PROJECT, activate_agent, install_arguments, read_plist, run_installer, validate_options
from token_budget_mcp.config import DEFAULT_INACTIVITY_SECONDS, DEFAULT_ACCOUNT_REFRESH_INTERVAL, positive_number, parse_since

LABEL = 'com.local.codex-token-monitor'
PACKAGE_FILES = ('__init__.py', 'config.py', 'account_usage.py', 'account_refresh.py', 'core.py', 'usage.py', 'monitor.py', 'monitor_cli.py', 'web_server.py', 'usage_server.py', 'dashboard.html', 'credit_rates.json')
HOOK_EVENTS = ('UserPromptSubmit', 'Stop', 'Interrupt')
INTERRUPT_TIMEOUT = 3
HOOK_TIMEOUT = 10
LAUNCH_THROTTLE_SECONDS = 10


def configure_hooks(config, command, project=PROJECT):
    """Preserve unrelated hooks and install each monitor hook only once."""
    if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
        raise ValueError('Hook configuration must contain a hooks object')
    runner = shlex.split(command)[1]
    def belongs_to_monitor(hook):
        previous = hook.get('command', '')
        tokens = shlex.split(previous)
        return runner in tokens or ('token_budget_mcp.monitor' in previous and str(project) in previous)

    for event in HOOK_EVENTS:
        entries = config.setdefault('hooks', {}).setdefault(event, [])
        if not isinstance(entries, list): raise ValueError('Hook entries must be a list')
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('hooks', []), list):
                raise ValueError('Each hook entry must contain a hook list')
            for hook in entry.get('hooks', []):
                if not isinstance(hook, dict) or not isinstance(hook.get('command', ''), str):
                    raise ValueError('Hook commands must be strings')
            entry['hooks'] = [hook for hook in entry.get('hooks', []) if not belongs_to_monitor(hook)]
        entries[:] = [entry for entry in entries if entry.get('hooks')]
        if not any(hook.get('command') == command for entry in entries for hook in entry.get('hooks', [])):
            entries.append({'hooks': [{'type': 'command', 'command': command,
                'timeout': INTERRUPT_TIMEOUT if event == 'Interrupt' else HOOK_TIMEOUT}]})
    return config


def main(argv=None):
    parser = install_arguments(__doc__)
    parser.add_argument('--python', type=Path, default=Path('/usr/bin/python3'))
    parser.add_argument('--codex-home', type=Path, default=Path.home() / '.codex')
    parser.add_argument('--inactivity-seconds', type=float, default=DEFAULT_INACTIVITY_SECONDS)
    parser.add_argument('--codex-command', default=shutil.which('codex'))
    parser.add_argument('--account-refresh-interval', type=float, default=DEFAULT_ACCOUNT_REFRESH_INTERVAL)
    parser.add_argument('--with-hooks', action='store_true', help='Opt in to Codex submission hooks')
    parser.add_argument('--since', help='Optional ISO date/time cutoff for unseen logs')
    args = parser.parse_args(argv)
    validate_options(parser, args)
    try:
        positive_number(args.inactivity_seconds, 'inactivity_seconds')
        positive_number(args.account_refresh_interval, 'account_refresh_interval')
        parse_since(args.since)
    except ValueError as error: parser.error(str(error))
    if not args.python.is_file(): raise ValueError('Python executable does not exist')
    runtime = args.runtime
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    existing = read_plist(plist)
    # Check conflicts and parse hooks before writing runtime files or configuration.
    if existing and existing.get('WorkingDirectory') not in (str(PROJECT), str(runtime)):
        raise ValueError('An unrelated launch agent already uses this label')
    hooks_path = args.codex_home / 'hooks.json'
    config = (json.loads(hooks_path.read_text()) if hooks_path.exists() else {'hooks': {}}) if args.with_hooks else None
    runner = runtime / 'run_monitor.py'
    common_args = [str(args.python), str(runner), '--database', str(runtime / 'monitor.sqlite3'),
        '--sessions', str(args.codex_home / 'sessions'), '--port', str(args.port),
        '--poll-interval', str(args.poll_interval), '--inactivity-seconds', str(args.inactivity_seconds)]
    common_args += ['--account-refresh-interval', str(args.account_refresh_interval)]
    if args.codex_command: common_args += ['--codex-command', str(Path(args.codex_command).expanduser().resolve())]
    if args.since: common_args += ['--since', args.since]
    command = shlex.join(common_args + ['--hook'])
    if args.with_hooks: configure_hooks(config, command)
    package = runtime / 'token_budget_mcp'
    package.mkdir(parents=True, exist_ok=True)
    for name in PACKAGE_FILES:
        shutil.copy2(PROJECT / 'src/token_budget_mcp' / name, package / name)
    runner.write_text('from token_budget_mcp.monitor import main\nmain()\n')
    if args.with_hooks:
        args.codex_home.mkdir(parents=True, exist_ok=True)
        backup = hooks_path.with_name('hooks.before-token-monitor.json')
        if hooks_path.exists() and not backup.exists(): backup.write_bytes(hooks_path.read_bytes())
        hooks_path.write_text(json.dumps(config, indent=2) + '\n')
    activate_agent(plist, {'Label': LABEL, 'ProgramArguments': common_args,
        'WorkingDirectory': str(runtime), 'RunAtLoad': True, 'KeepAlive': True,
        'StandardOutPath': str(runtime / 'monitor.stdout.log'),
        'StandardErrorPath': str(runtime / 'monitor.stderr.log'), 'ThrottleInterval': LAUNCH_THROTTLE_SECONDS})
    print(json.dumps({'watcher': 'installed', 'dashboard': f'http://127.0.0.1:{args.port}',
        'hooks': 'configured; require review and trust in Codex before execution' if args.with_hooks else 'not modified', 'hooks_file': str(hooks_path)}))


if __name__ == '__main__':
    run_installer(main)
