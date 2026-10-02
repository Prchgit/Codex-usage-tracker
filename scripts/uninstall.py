"""Uninstall Codex Usage Tracker services and program files while retaining local usage history."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess

from install_common import read_plist
from plugin_common import plugin_uninstall_plan, uninstall_plugin, plugin_runtime_directory, PLUGIN_COLLECTOR_LABEL
from install_monitor import LABEL as COLLECTOR_LABEL
from install_floating import LABEL as PANEL_LABEL, APP_NAME, BINARY_NAME
from token_budget_mcp.config import runtime_directory


def remove_hooks(config, runner, project=None):
    """Remove only Codex Usage Tracker commands; keep unrelated entries and configuration."""
    hooks = config.get('hooks', {})
    if not isinstance(hooks, dict): raise ValueError('Invalid hooks configuration')
    for event, entries in hooks.items():
        if not isinstance(entries, list): raise ValueError('Invalid hook entries')
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get('hooks', []), list):
                raise ValueError('Invalid hook entry')
            remaining = []
            for hook in entry.get('hooks', []):
                if not isinstance(hook, dict): raise ValueError('Invalid hook command')
                tokens = shlex.split(hook.get('command', ''))
                owned = str(runner) in tokens or (project is not None and
                    'token_budget_mcp.monitor' in tokens and str(project) in tokens)
                if not owned: remaining.append(hook)
            entry['hooks'] = remaining
        hooks[event] = [entry for entry in entries if entry.get('hooks')]
    return config


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path)
    parser.add_argument('--plugin', action='store_true', help='Remove the independent plugin installation, retaining history')
    parser.add_argument('--codex-home', type=Path, default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--codex-command', default=shutil.which('codex'))
    parser.add_argument('--dry-run', action='store_true', help='Show the uninstall plan without changing anything')
    args = parser.parse_args(argv)
    runtime = (args.runtime or (plugin_runtime_directory() if args.plugin else runtime_directory())).expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    manifest = runtime / 'installation.json'
    installation = json.loads(manifest.read_text()) if manifest.exists() else {}
    project = Path(installation['project']) if installation.get('project') else None
    runner = runtime / 'run_monitor.py'
    binary = runtime / APP_NAME / 'Contents/MacOS' / BINARY_NAME
    agents = []
    if args.plugin and installation and installation.get('mode') != 'standalone-plugin':
        raise ValueError('This runtime is not an independent plugin installation')
    standalone = args.plugin or installation.get('mode') == 'standalone-plugin'
    if standalone and installation and installation.get('collector_label') != PLUGIN_COLLECTOR_LABEL:
        raise ValueError('Unrecognized standalone collector label; not removed')
    for label in ((PLUGIN_COLLECTOR_LABEL,) if standalone else (PANEL_LABEL, COLLECTOR_LABEL)):
        path = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
        existing = read_plist(path)
        if existing:
            arguments = existing.get('ProgramArguments', [])
            owned = arguments == [str(binary)] if label == PANEL_LABEL else str(runner) in arguments
            if not owned: raise ValueError('Unrelated launch agent uses ' + label + '; not removed')
            agents.append(path)
    hooks_path = codex_home / 'hooks.json'
    hooks = None
    if hooks_path.exists():
        original = hooks_path.read_text()
        config = remove_hooks(json.loads(original), runner, project)
        if config != json.loads(original): hooks = json.dumps(config, indent=2) + '\n'
    mcp = False
    if not standalone and (args.codex_command or installation.get('mcp_requested')):
        if not args.codex_command: raise ValueError('Codex CLI is needed to remove the optional MCP registration')
        result = subprocess.run([args.codex_command, 'mcp', 'get', 'codex-usage-tracker', '--json'],
            capture_output=True, text=True, env={**os.environ, 'CODEX_HOME': str(codex_home)})
        if result.returncode == 0:
            command = json.loads(result.stdout).get('transport', {}).get('command')
            if project is None or command != str(project / '.venv/bin/token-budget-mcp'):
                raise ValueError('Unrelated MCP registration uses codex-usage-tracker; not removed')
            mcp = True
        elif 'not found' not in result.stderr.lower() and 'no mcp server' not in result.stderr.lower():
            raise ValueError('Cannot inspect optional MCP registration; retry after checking Codex CLI access')
    plugin_plan = plugin_uninstall_plan(runtime, codex_home, args.codex_command)
    # Recognized installs may remove program files, but never history databases or logs.
    programs = [runtime / APP_NAME, runtime / 'token_budget_mcp', runtime / 'clang-cache', runner, manifest, runtime / '__pycache__', runtime / 'run_usage_mcp.py', runtime / 'mcp-venv', runtime / 'plugin-marketplace', runtime / 'plugin-installation.json'] if installation else []
    print(json.dumps({'dry_run': args.dry_run, 'launch_agents': [str(p) for p in agents],
        'program_files': [str(p) for p in programs], 'remove_mcp': mcp,
        'remove_hooks': hooks is not None, 'plugin': plugin_plan, 'history_retained': str(runtime)}))
    if args.dry_run: return
    if plugin_plan: uninstall_plugin(plugin_plan, runtime, codex_home, args.codex_command)
    if mcp:
        subprocess.run([args.codex_command, 'mcp', 'remove', 'codex-usage-tracker'], check=True,
            env={**os.environ, 'CODEX_HOME': str(codex_home)})
    if hooks is not None: hooks_path.write_text(hooks)
    for path in agents:
        result = subprocess.run(['launchctl', 'bootout', f'gui/{os.getuid()}', str(path)], capture_output=True)
        # A stopped service is fine; unexpected errors must not leave it running with deleted files.
        if result.returncode != 0:
            status = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{path.stem}'], capture_output=True)
            if status.returncode not in (3, 113):
                raise ValueError('Cannot stop tracker service; program files retained')
        path.unlink()
    for path in programs:
        if path.is_symlink(): path.unlink()
        elif path.is_dir(): shutil.rmtree(path)
        elif path.exists(): path.unlink()
    print('Codex Usage Tracker uninstalled. Usage history and diagnostic logs retained in ' + str(runtime))


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Uninstall failed: {error}') from error
