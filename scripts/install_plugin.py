"""Install the local read-only CUT plugin and its isolated MCP dependencies."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from install_common import PROJECT
from token_budget_mcp.config import runtime_directory

from plugin_common import PLUGIN_NAME, MARKETPLACE_NAME, PLUGIN_ID

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
    config['mcpServers']['usage']['env'] = {'CUT_RUNTIME_DIR':str(runtime)}
    config_path.write_text(json.dumps(config,indent=2) + '\n')
    marketplace = root / '.agents/plugins/marketplace.json'
    marketplace.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(PROJECT / '.agents/plugins/marketplace.json',marketplace)
    return root


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime',type=Path,default=runtime_directory())
    parser.add_argument('--codex-home',type=Path,default=Path(os.environ.get('CODEX_HOME') or Path.home() / '.codex'))
    parser.add_argument('--python',help='Python 3.10+ used only for the MCP environment')
    parser.add_argument('--codex-command',default=shutil.which('codex'))
    args = parser.parse_args(argv)
    if not args.codex_command: raise ValueError('Codex CLI must be installed to register the plugin')
    python = select_python(args.python)
    runtime = args.runtime.expanduser().resolve()
    codex_home = args.codex_home.expanduser().resolve()
    environment = {**os.environ,'CODEX_HOME':str(codex_home)}
    if not (runtime / 'installation.json').exists():
        subprocess.run([sys.executable,str(PROJECT / 'scripts/install.py'),'--runtime',str(runtime),'--codex-home',str(codex_home)],check=True)
    if runtime.stat().st_uid != os.getuid(): raise ValueError('Runtime must be owned by the current user')
    package = runtime / 'token_budget_mcp'
    if not (package / 'config.py').exists(): raise ValueError('Install the local collector before the plugin')
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
                raise ValueError('A different marketplace already uses ' + MARKETPLACE_NAME)
    venv = runtime / 'mcp-venv'
    if not (venv / 'bin/python').exists(): subprocess.run([python,'-m','venv',str(venv)],check=True)
    subprocess.run([str(venv / 'bin/python'),'-m','pip','install','--disable-pip-version-check','mcp>=1.20,<2'],check=True)
    shutil.copy2(PROJECT / 'src/token_budget_mcp/usage_server.py',package / 'usage_server.py')
    (runtime / 'run_usage_mcp.py').write_text('from token_budget_mcp.usage_server import main\nmain()\n')
    root = stage_plugin(runtime)
    subprocess.run([args.codex_command,'plugin','marketplace','add',str(root),'--json'],cwd=runtime,env=environment,check=True)
    installed = subprocess.run([args.codex_command,'plugin','add',PLUGIN_ID,'--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
    info = json.loads(installed.stdout)
    if info.get('pluginId') != PLUGIN_ID: raise ValueError('Codex returned an unexpected plugin identity')
    (runtime / 'plugin-installation.json').write_text(json.dumps({'plugin_id':PLUGIN_ID,'marketplace':MARKETPLACE_NAME,
        'marketplace_root':str(root),'installed_path':info.get('installedPath')},indent=2) + '\n')
    print('CUT read-only plugin installed. Open a new Codex chat to discover the tools and usage skill.')
    print('Local collector and floating panel remain available; no API key or generation tools are included.')


if __name__ == '__main__':
    try: main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Plugin installation failed: {error}') from error
