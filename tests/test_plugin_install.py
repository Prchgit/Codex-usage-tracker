"""Package staging, interpreter selection and plugin ownership checks."""
import json
from pathlib import Path
import subprocess
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_plugin import select_python, stage_plugin, install_collector, register_plugin
from plugin_common import plugin_uninstall_plan, uninstall_plugin, PLUGIN_ID, MARKETPLACE_NAME, PLUGIN_COLLECTOR_LABEL


class PluginInstallTests(unittest.TestCase):
    def test_failed_migration_restores_previous_registration(self):
        failure = subprocess.CalledProcessError(1,['codex'])
        restored = subprocess.CompletedProcess([],0)
        with tempfile.TemporaryDirectory() as directory, patch('install_plugin.uninstall_plugin') as remove, patch('install_plugin.subprocess.run',side_effect=[failure,restored,restored,restored,restored]) as run:
            runtime = Path(directory)
            old = runtime / 'original'
            with self.assertRaises(subprocess.CalledProcessError):
                register_plugin(runtime,runtime / 'plugin-marketplace',runtime / 'codex','codex',(old,{}))
            remove.assert_called_once()
            self.assertIn(str(old / 'plugin-marketplace'),run.call_args_list[-2].args[0])
            self.assertEqual(run.call_args_list[-1].args[0],['codex','plugin','add',PLUGIN_ID,'--json'])

    def test_independent_collector_copies_history_and_installs_own_service(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            legacy, runtime = root / 'legacy', root / 'independent'
            legacy.mkdir(); runtime.mkdir()
            with sqlite3.connect(legacy / 'monitor.sqlite3') as db:
                db.execute('CREATE TABLE example(value)'); db.execute('INSERT INTO example VALUES (42)')
            response = MagicMock(); response.__enter__.return_value.status = 200
            with patch('install_plugin.read_plist',return_value=None), patch('install_plugin.socket.socket'), patch('install_plugin.urlopen',return_value=response), patch('install_plugin.subprocess.run') as run:
                install_collector(runtime,root / 'codex','codex',root / 'python',8768,legacy)
            command = run.call_args.args[0]
            self.assertIn('install_monitor.py', command[1])
            self.assertEqual(command[command.index('--label')+1],PLUGIN_COLLECTOR_LABEL)
            self.assertNotIn('--with-hooks',command)
            self.assertEqual(json.loads((runtime / 'installation.json').read_text())['mode'],'standalone-plugin')
            with sqlite3.connect(runtime / 'monitor.sqlite3') as db:
                self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0],42)

    def test_staged_package_carries_custom_runtime_and_executable_launcher(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory).resolve()
            root = stage_plugin(runtime)
            plugin = root / 'plugins/codex-usage-tracker'
            config = json.loads((plugin / 'mcp.json').read_text())
            self.assertEqual(config['mcpServers']['usage']['env']['CODEX_USAGE_TRACKER_RUNTIME_DIR'],str(runtime))
            launcher = plugin / config['mcpServers']['usage']['command']
            self.assertTrue(launcher.stat().st_mode & 0o111)
            self.assertTrue((plugin / 'skills/usage/SKILL.md').exists())
            self.assertEqual(json.loads((root / '.agents/plugins/marketplace.json').read_text())['name'],MARKETPLACE_NAME)

    def test_python_version_checked_before_building_environment(self):
        with patch('install_plugin.subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout='0\n')):
            with self.assertRaises(ValueError): select_python(sys.executable)
        with patch('install_plugin.subprocess.run',return_value=subprocess.CompletedProcess([],0,stdout='1\n')):
            self.assertEqual(select_python(sys.executable),sys.executable)

    def test_uninstaller_removes_only_owned_plugin_and_marketplace(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory).resolve()
            root = runtime / 'plugin-marketplace'
            (runtime / 'plugin-installation.json').write_text(json.dumps({'plugin_id':PLUGIN_ID,'marketplace_root':str(root)}))
            results = [subprocess.CompletedProcess([],0,stdout=json.dumps({'marketplaces':[{'name':MARKETPLACE_NAME,'root':str(root)}]})),
                       subprocess.CompletedProcess([],0,stdout=json.dumps({'installed':[{'pluginId':PLUGIN_ID}]}))]
            with patch('plugin_common.subprocess.run',side_effect=results):
                plan = plugin_uninstall_plan(runtime,runtime / 'codex','codex')
            self.assertEqual(plan,{'remove_plugin':True,'remove_marketplace':True})
            with patch('plugin_common.subprocess.run',return_value=subprocess.CompletedProcess([],0)) as run:
                uninstall_plugin(plan,runtime,runtime / 'codex','codex')
                self.assertEqual(run.call_count,2)
                self.assertIn(PLUGIN_ID,run.call_args_list[0].args[0])
            bad = subprocess.CompletedProcess([],0,stdout=json.dumps({'marketplaces':[{'name':MARKETPLACE_NAME,'root':'/unrelated'}]}))
            with patch('plugin_common.subprocess.run',return_value=bad) as run, self.assertRaises(ValueError):
                plugin_uninstall_plan(runtime,runtime / 'codex','codex')
            self.assertEqual(run.call_count,1)

    def test_missing_marker_has_no_plugin_side_effects(self):
        with tempfile.TemporaryDirectory() as directory, patch('plugin_common.subprocess.run') as run:
            self.assertIsNone(plugin_uninstall_plan(Path(directory),Path(directory),'codex'))
            run.assert_not_called()


if __name__ == '__main__': unittest.main()
