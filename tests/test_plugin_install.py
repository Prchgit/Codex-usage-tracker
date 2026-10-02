"""Package staging, interpreter selection and plugin ownership checks."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1] / 'scripts'))
from install_plugin import select_python, stage_plugin
from plugin_common import plugin_uninstall_plan, uninstall_plugin, PLUGIN_ID, MARKETPLACE_NAME


class PluginInstallTests(unittest.TestCase):
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
