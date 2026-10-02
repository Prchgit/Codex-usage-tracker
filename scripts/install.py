"""Activate the Mac tracker; MCP and submission hooks are opt-in."""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import socket
import subprocess
import time
import urllib.request

from install_common import PROJECT, DEFAULT_PORT, install_arguments, read_plist, run_installer, validate_options
from install_floating import main as install_panel
from install_monitor import LABEL, main as install_collector

MCP_NAME = 'codex-usage-tracker'
READY_TIMEOUT_SECONDS = 20
READY_POLL_SECONDS = 0.25


def check_prerequisites(codex_home, python, with_mcp):
    if platform.system() != 'Darwin': raise ValueError('CUT currently supports macOS only')
    if not python.is_file(): raise ValueError('Python is missing; install Apple command-line tools with xcode-select --install')
    if subprocess.run(['xcode-select', '-p'], capture_output=True).returncode:
        raise ValueError('Install Apple command-line tools with xcode-select --install, then retry')
    if not codex_home.is_dir(): raise ValueError('Open Codex once before activating CUT, or supply --codex-home')
    if with_mcp and (not shutil.which('uv') or not shutil.which('codex')):
        raise ValueError('--with-mcp requires uv and the Codex CLI on PATH; default tracking does not')


def choose_port(preferred, runtime):
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    existing = read_plist(plist)
    if existing and existing.get('WorkingDirectory') not in (str(PROJECT), str(runtime)):
        raise ValueError('An unrelated launch agent already uses the CUT collector label')
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1', preferred))
        except OSError:
            arguments = existing.get('ProgramArguments', []) if existing else []
            old_port = int(arguments[arguments.index('--port') + 1]) if '--port' in arguments else DEFAULT_PORT
            if existing and old_port == preferred: return preferred
            probe.bind(('127.0.0.1', 0))
        return probe.getsockname()[1]


def check_mcp_registration(executable, codex_home):
    result = subprocess.run(['codex', 'mcp', 'get', MCP_NAME, '--json'], capture_output=True, text=True,
        env={**os.environ, 'CODEX_HOME': str(codex_home)})
    if result.returncode == 0:
        existing = json.loads(result.stdout)
        transport = existing.get('transport', {})
        if transport.get('command') != str(executable):
            raise ValueError('An unrelated MCP registration already uses codex-usage-tracker; not replaced')


def enable_mcp(runtime, codex_home):
    executable = PROJECT / '.venv/bin/token-budget-mcp'
    check_mcp_registration(executable, codex_home)
    subprocess.run(['uv', 'sync', '--project', str(PROJECT)], check=True)
    subprocess.run(['codex', 'mcp', 'add', MCP_NAME, '--', str(executable),
        '--database', str(runtime / 'request-snapshots.sqlite3'),
        '--monitor-database', str(runtime / 'monitor.sqlite3'), '--sessions', str(codex_home / 'sessions')], check=True,
        env={**os.environ, 'CODEX_HOME': str(codex_home)})


def wait_until_ready(port):
    url = f'http://127.0.0.1:{port}/api/turns'
    deadline = time.monotonic() + READY_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as response:
                report = json.load(response)
                if isinstance(report.get('turns'), list) and isinstance(report.get('chats'), list): return
        except (OSError, ValueError): pass
        time.sleep(READY_POLL_SECONDS)
    raise ValueError('Collector did not become ready; inspect monitor.stderr.log in the runtime directory')


def main(argv=None):
    parser = install_arguments(__doc__)
    parser.add_argument('--python', type=Path, default=Path('/usr/bin/python3'))
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--codex-command', help='Codex executable for automatic account-limit refresh')
    parser.add_argument('--with-mcp', action='store_true', help='Register optional MCP tools in Codex (off by default)')
    parser.add_argument('--with-hooks', action='store_true', help='Add optional submission hooks; requires Codex trust')
    parser.add_argument('--since', help='Optional ISO date/time cutoff for previously unseen logs')
    args = parser.parse_args(argv)
    validate_options(parser, args)
    args.codex_home = args.codex_home.expanduser().resolve()
    args.python = args.python.expanduser().resolve()
    check_prerequisites(args.codex_home, args.python, args.with_mcp)
    if args.with_mcp: check_mcp_registration(PROJECT / '.venv/bin/token-budget-mcp', args.codex_home)
    port = choose_port(args.port, args.runtime)
    if args.runtime.is_symlink(): raise ValueError('Runtime must not be a symbolic link')
    args.runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if args.runtime.stat().st_uid != os.getuid(): raise ValueError('Runtime must be owned by the current user')
    args.runtime.chmod(0o700)
    common = ['--runtime', str(args.runtime), '--port', str(port), '--poll-interval', str(args.poll_interval)]
    collector = common + ['--python', str(args.python), '--codex-home', str(args.codex_home)]
    if args.codex_command: collector += ['--codex-command', args.codex_command]
    if args.with_hooks: collector += ['--with-hooks']
    if args.since: collector += ['--since', args.since]
    install_collector(collector)
    install_panel(common)
    wait_until_ready(port)
    if args.with_mcp: enable_mcp(args.runtime, args.codex_home)
    (args.runtime / 'installation.json').write_text(json.dumps({'port': port, 'mcp_requested': args.with_mcp,
        'hooks_requested': args.with_hooks, 'project': str(PROJECT)}, indent=2))
    print(f'CUT is active. History: http://127.0.0.1:{port}/')
    print('MCP enabled; reopen the Codex chat to discover tools.' if args.with_mcp else 'MCP registration unchanged (off on fresh installs). No API key or extra model requests are required.')


if __name__ == '__main__': run_installer(main)
