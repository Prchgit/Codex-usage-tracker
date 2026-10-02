"""Uninstall lifecycle checks use temporary files and mocked launchctl."""
import json
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import uninstall


class UninstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.runtime = self.home / 'runtime'
        self.runtime.mkdir()
        (self.runtime / 'installation.json').write_text(json.dumps({'project':'/test/cut','mcp_requested':False}))
        self.database = self.runtime / 'monitor.sqlite3'
        self.database.write_bytes(b'kept-history')
        (self.runtime / 'run_monitor.py').write_text('program')
        agents = self.home / 'Library/LaunchAgents'
        agents.mkdir(parents=True)
        self.plist = agents / (uninstall.COLLECTOR_LABEL + '.plist')
        self.plist.write_bytes(plistlib.dumps({'ProgramArguments':['/usr/bin/python3',str(self.runtime / 'run_monitor.py')]}))
        self.args = ['--runtime',str(self.runtime),'--codex-home',str(self.home / '.codex')]
        self.print_patch = patch('builtins.print')
        self.print_patch.start()
        self.home_patch = patch('pathlib.Path.home', return_value=self.home)
        self.home_patch.start()
        self.command_patch = patch('uninstall.shutil.which', return_value=None)
        self.command_patch.start()

    def tearDown(self):
        self.command_patch.stop(); self.home_patch.stop(); self.print_patch.stop(); self.temp.cleanup()

    def test_default_uninstall_stops_services_preserves_history_and_is_repeatable(self):
        with patch('uninstall.subprocess.run', return_value=subprocess.CompletedProcess([],0)) as run:
            uninstall.main(self.args)
            self.assertEqual(run.call_args.args[0][1], 'bootout')
            self.assertFalse(self.plist.exists())
            self.assertFalse((self.runtime / 'run_monitor.py').exists())
            self.assertEqual(self.database.read_bytes(), b'kept-history')
            uninstall.main(self.args)
        self.assertEqual(self.database.read_bytes(), b'kept-history')

    def test_dry_run_does_not_change_files_or_stop_services(self):
        with patch('uninstall.subprocess.run') as run:
            uninstall.main(self.args + ['--dry-run'])
            run.assert_not_called()
        self.assertTrue(self.plist.exists())
        self.assertTrue((self.runtime / 'run_monitor.py').exists())

    def test_unrelated_agent_is_preserved_before_any_changes(self):
        self.plist.write_bytes(plistlib.dumps({'ProgramArguments':['/other/program']}))
        with patch('uninstall.subprocess.run') as run, self.assertRaises(ValueError):
            uninstall.main(self.args)
        run.assert_not_called()
        self.assertTrue(self.plist.exists())
        self.assertTrue((self.runtime / 'installation.json').exists())

    def test_already_stopped_service_can_be_uninstalled(self):
        outcomes = [subprocess.CompletedProcess([],5), subprocess.CompletedProcess([],113)]
        with patch('uninstall.subprocess.run', side_effect=outcomes):
            uninstall.main(self.args)
        self.assertFalse(self.plist.exists())
        self.assertEqual(self.database.read_bytes(), b'kept-history')

    def test_only_owned_hooks_removed(self):
        runner = self.runtime / 'run_monitor.py'
        config = {'other':True, 'hooks':{'Stop':[{'hooks':[{'command':f'python3 {runner} --hook'}, {'command':'echo other'}]}]}}
        result = uninstall.remove_hooks(config, runner)
        self.assertEqual(result['hooks']['Stop'][0]['hooks'], [{'command':'echo other'}])
        self.assertTrue(result['other'])

    def test_failure_to_stop_running_service_keeps_program_and_plist(self):
        outcomes = [subprocess.CompletedProcess([],5),subprocess.CompletedProcess([],0)]
        with patch('uninstall.subprocess.run',side_effect=outcomes), self.assertRaises(ValueError):
            uninstall.main(self.args)
        self.assertTrue(self.plist.exists())
        self.assertTrue((self.runtime / 'run_monitor.py').exists())

    def test_owned_optional_mcp_removed_and_unrelated_registration_rejected(self):
        for command, owned in [('/test/cut/.venv/bin/token-budget-mcp', True),('/other/mcp',False)]:
            with self.subTest(command=command):
                get = subprocess.CompletedProcess([],0,stdout=json.dumps({'transport':{'command':command}}))
                outcomes = [get,subprocess.CompletedProcess([],0),subprocess.CompletedProcess([],0)]
                with patch('uninstall.subprocess.run',side_effect=outcomes) as run:
                    if owned:
                        uninstall.main(self.args + ['--codex-command','codex'])
                        self.assertEqual(run.call_args_list[1].args[0], ['codex','mcp','remove','codex-usage-tracker'])
                    else:
                        # Restore a manifest after the successful subcase.
                        (self.runtime / 'installation.json').write_text(json.dumps({'project':'/test/cut'}))
                        with self.assertRaises(ValueError): uninstall.main(self.args + ['--codex-command','codex'])
                        self.assertEqual(run.call_count,1)


if __name__ == '__main__': unittest.main()
