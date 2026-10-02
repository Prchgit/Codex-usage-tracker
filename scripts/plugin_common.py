"""Ownership-aware lifecycle for Codex Usage Tracker's private local marketplace."""
import json
import os
from pathlib import Path
import subprocess

PLUGIN_NAME = 'codex-usage-tracker'
MARKETPLACE_NAME = 'codex-usage-tracker-local'
PLUGIN_ID = PLUGIN_NAME + '@' + MARKETPLACE_NAME


def plugin_uninstall_plan(runtime, codex_home, command):
    path = runtime / 'plugin-installation.json'
    if not path.exists(): return None
    if not command: raise ValueError('Codex CLI is required to uninstall the Codex Usage Tracker plugin')
    installation = json.loads(path.read_text())
    expected_root = runtime / 'plugin-marketplace'
    if installation.get('plugin_id') != PLUGIN_ID or Path(installation.get('marketplace_root','')).resolve() != expected_root.resolve():
        raise ValueError('Unrecognized plugin installation metadata; not removed')
    environment = {**os.environ,'CODEX_HOME':str(codex_home)}
    result = subprocess.run([command,'plugin','marketplace','list','--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
    data = json.loads(result.stdout)
    entries = data if isinstance(data,list) else data.get('marketplaces',[])
    marketplace = next((entry for entry in entries if entry.get('name') == MARKETPLACE_NAME),None)
    if marketplace and Path(marketplace['root']).resolve() != expected_root.resolve():
        raise ValueError('A different marketplace uses the Codex Usage Tracker name; not removed')
    result = subprocess.run([command,'plugin','list','--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
    plugins = json.loads(result.stdout).get('installed',[])
    installed = any(item.get('pluginId') == PLUGIN_ID for item in plugins)
    return {'remove_plugin':installed,'remove_marketplace':marketplace is not None}


def uninstall_plugin(plan, runtime, codex_home, command):
    environment = {**os.environ,'CODEX_HOME':str(codex_home)}
    if plan['remove_plugin']:
        subprocess.run([command,'plugin','remove',PLUGIN_ID,'--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
    if plan['remove_marketplace']:
        subprocess.run([command,'plugin','marketplace','remove',MARKETPLACE_NAME,'--json'],cwd=runtime,env=environment,capture_output=True,text=True,check=True)
