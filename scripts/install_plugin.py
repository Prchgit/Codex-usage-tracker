"""Install the local read-only Codex Usage Tracker plugin and its isolated MCP dependencies."""
import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import socket
import sqlite3
import time
from urllib.request import urlopen
from install_common import PROJECT
from token_budget_mcp.config import runtime_directory, PANEL_HOSTNAME

from plugin_common import PLUGIN_NAME, MARKETPLACE_NAME, PLUGIN_ID, PLUGIN_COLLECTOR_LABEL, PLUGIN_PORT, plugin_runtime_directory, plugin_uninstall_plan, uninstall_plugin
from install_common import read_plist, activate_agent

READY_TIMEOUT_SECONDS = 15
READY_POLL_SECONDS = 0.25

def select_python(explicit=None):
    candidates = [explicit] if explicit else [sys.executable] + [shutil.which(name) for name in ('python3.14','python3.13','python3.12','python3.11','python3.10')]
    for candidate in candidates:
        if not candidate: continue
        command = shutil.which(str(candidate)) or str(Path(candidate).expanduser())
        result = subprocess.run([command,'-c','import sys; print(int(sys.version_info >= (3,10)))'],capture_output=True,text=True)
        if result.returncode == 0 and result.stdout.strip() == '1': return command
    raise ValueError('The plugin requires Python 3.10+. Supply --python /path/to/python3.12 (the base collector supports Python 3.9)')


def stage_plugin(runtime):
    root = runtime / 'plugin-marketplace'
    template = PROJECT / 'plugins' / PLUGIN_NAME
    destination = root / 'plugins' / PLUGIN_NAME
    if (root / '.agents/plugins/marketplace.json').exists():
        previous = json.loads((root / '.agents/plugins/marketplace.json').read_text())
        if previous.get('name') != MARKETPLACE_NAME: raise ValueError('Unrelated marketplace occupies the plugin staging directory')
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copytree(template,destination,dirs_exist_ok=True)
    config_path = destination / 'mcp.json'
    config = json.loads(config_path.read_text())
    config['mcpServers']['usage']['env'] = {'CODEX_USAGE_TRACKER_RUNTIME_DIR':str(runtime)}
    config_path.write_text(json.dumps(config,indent=2) + '\n')
    marketplace = root / '.agents/plugins/marketplace.json'
    marketplace.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(PROJECT / '.agents/plugins/marketplace.json',marketplace)
    return root



def install_collector(runtime, codex_home, command, python, port, history_source=None):
    agent = Path.home() / 'Library/LaunchAgents' / (PLUGIN_COLLECTOR_LABEL + '.plist')
    existing = read_plist(agent)
    if existing and str(runtime / 'run_monitor.py') not in existing.get('ProgramArguments',[]):
        raise ValueError('An unrelated collector uses the standalone label')
    arguments = existing.get('ProgramArguments',[]) if existing else []
    existing_port = int(arguments[arguments.index('--port')+1]) if '--port' in arguments else None
    with socket.socket() as probe:
        try: probe.bind(('127.0.0.1',port))
        except OSError:
            if existing_port != port: raise ValueError('Standalone panel port is occupied; supply --port')
    destination = runtime / 'monitor.sqlite3'
    if history_source and not destination.exists() and (history_source / 'monitor.sqlite3').exists():
        with closing(sqlite3.connect((history_source / 'monitor.sqlite3').as_uri() + '?mode=ro',uri=True)) as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
    subprocess.run([sys.executable,str(PROJECT / 'scripts/install_monitor.py'),'--runtime',str(runtime),
        '--codex-home',str(codex_home),'--python',str(python),'--port',str(port),
        '--codex-command',command,'--label',PLUGIN_COLLECTOR_LABEL],check=True)
    (runtime / 'installation.json').write_text(json.dumps({'mode':'standalone-plugin','collector_label':PLUGIN_COLLECTOR_LABEL,
        'port':port,'codex_home':str(codex_home),'mcp_requested':False},indent=2)+'\n')
    wait_for_panel(port)

def wait_for_panel(port):
    deadline=time.monotonic()+READY_TIMEOUT_SECONDS
    while time.monotonic()<deadline:
        try:
            with urlopen(f'http://127.0.0.1:{port}/panel',timeout=1) as response:
                if response.status==200:return
        except OSError: pass
        time.sleep(READY_POLL_SECONDS)
    raise ValueError('Standalone collector did not become ready; inspect its monitor.stderr.log')


def register_plugin(runtime, root, codex_home, command, migration=None):
    environment = {**os.environ,'CODEX_HOME':str(codex_home)}
    if migration: uninstall_plugin(migration[1],migration[0],codex_home,command)
    try:
        subprocess.run([command,'plugin','marketplace','add',str(root),'--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
        installed = subprocess.run([command,'plugin','add',PLUGIN_ID,'--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
        info = json.loads(installed.stdout)
        if info.get('pluginId') != PLUGIN_ID: raise ValueError('Codex returned an unexpected plugin identity')
        return info
    except (OSError,ValueError,subprocess.CalledProcessError):
        if migration:
            # Restore the known previous package if registration fails mid-migration.
            subprocess.run([command,'plugin','remove',PLUGIN_ID,'--json'],cwd=runtime,env=environment,capture_output=True)
            subprocess.run([command,'plugin','marketplace','remove',MARKETPLACE_NAME,'--json'],cwd=runtime,env=environment,capture_output=True)
            subprocess.run([command,'plugin','marketplace','add',str(migration[0] / 'plugin-marketplace'),'--json'],cwd=runtime,env=environment,capture_output=True,check=True)
            subprocess.run([command,'plugin','add',PLUGIN_ID,'--json'],cwd=runtime,env=environment,capture_output=True,check=True)
        raise

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=plugin_runtime_directory())
    parser.add_argument('--port',type=int,default=PLUGIN_PORT)
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--python',help='Python 3.10+ used only for the MCP environment')
    parser.add_argument('--codex-command',default=shutil.which('codex'))
    args = parser.parse_args(argv)
    if not args.codex_command: raise ValueError('Codex CLI must be installed to register the plugin')
    python = select_python(args.python)
    runtime = args.runtime.expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    environment = {**os.environ,'CODEX_HOME':str(codex_home)}
    if not 1 <= args.port <= 65535: raise ValueError('Port must be between 1 and 65535')
    if runtime.exists() and runtime.stat().st_uid != os.getuid(): raise ValueError('Runtime must be owned by the current user')
    manifest = runtime / 'installation.json'
    installation = json.loads(manifest.read_text()) if manifest.exists() else {}
    if installation and installation.get('mode') != 'standalone-plugin': raise ValueError('Choose a separate runtime; the original installation must remain independent')
    runtime.mkdir(parents=True,exist_ok=True,mode=0o700)
    runtime.chmod(0o700)
    package = runtime / 'token_budget_mcp'
    migration = None
    root = runtime / 'plugin-marketplace'
    # Check name conflicts before registering or replacing a marketplace.
    listed = subprocess.run([args.codex_command,'plugin','marketplace','list','--json'],
        cwd=runtime,env=environment,capture_output=True,text=True,check=True)
    data = json.loads(listed.stdout)
    entries = data if isinstance(data,list) else data.get('marketplaces',[])
    for entry in entries:
        if entry.get('name') == MARKETPLACE_NAME:
            existing_root = entry.get('root') or entry.get('path') or entry.get('source')
            if isinstance(existing_root,dict): existing_root = existing_root.get('path') or existing_root.get('source')
            if existing_root and Path(existing_root).resolve() != root.resolve():
                legacy = runtime_directory().resolve()
                if Path(existing_root).resolve() != legacy / 'plugin-marketplace' or not (legacy / 'plugin-installation.json').exists():
                    raise ValueError('A different marketplace already uses ' + MARKETPLACE_NAME)
                migration = (legacy, plugin_uninstall_plan(legacy,codex_home,args.codex_command))
    venv = runtime / 'mcp-venv'
    if not (venv / 'bin/python').exists(): subprocess.run([python,'-m','venv',str(venv)],check=True)
    subprocess.run([str(venv / 'bin/python'),'-m','pip','install','--disable-pip-version-check','mcp>=1.20,<2'],check=True)
    install_collector(runtime, codex_home, args.codex_command, venv / 'bin/python', args.port, migration[0] if migration else None)
    (runtime / 'run_usage_mcp.py').write_text('from token_budget_mcp.usage_server import main\nmain()\n')
    root = stage_plugin(runtime)
    info = register_plugin(runtime,root,codex_home,args.codex_command,migration)
    # Verify the service again after plugin registration and any migration cleanup.
    service = subprocess.run(['launchctl','print',f'gui/{os.getuid()}/{PLUGIN_COLLECTOR_LABEL}'],capture_output=True)
    if service.returncode != 0:
        agent = Path.home() / 'Library/LaunchAgents' / (PLUGIN_COLLECTOR_LABEL + '.plist')
        data = read_plist(agent)
        if not data or str(runtime / 'run_monitor.py') not in data.get('ProgramArguments',[]):
            raise ValueError('Independent collector registration is missing or unrelated')
        activate_agent(agent,data)
    wait_for_panel(args.port)
    (runtime / 'plugin-installation.json').write_text(json.dumps({'plugin_id':PLUGIN_ID,'marketplace':MARKETPLACE_NAME,
        'marketplace_root':str(root),'installed_path':info.get('installedPath')},indent=2) + '\n')
    if migration: (migration[0] / 'plugin-installation.json').unlink()
    print(f'Independent compact panel: http://{PANEL_HOSTNAME}:{args.port}/panel')
    print('Codex Usage Tracker read-only plugin installed. Open a new Codex chat to discover the tools and usage skill.')
    print('Collector, UI assets and MCP dependencies are installed together. No original installation or floating app is required.')


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Plugin installation failed: {error}') from error
