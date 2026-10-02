"""One-command activation behavior; no user configuration or services are changed."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import install


class SetupTests(unittest.TestCase):
    def activate(self, root, extra):
        runtime = root / 'runtime'
        with patch.object(install, 'check_prerequisites') as prerequisites, \
             patch.object(install, 'choose_port', return_value=9000), \
             patch.object(install, 'install_collector') as collector, \
             patch.object(install, 'install_panel') as panel, \
             patch.object(install, 'wait_until_ready') as ready, \
             patch.object(install, 'check_mcp_registration') as registration, \
             patch.object(install, 'enable_mcp') as mcp, patch('builtins.print'):
            install.main(['--runtime', str(runtime), '--codex-home', str(root / 'codex')] + extra)
            return runtime, prerequisites, collector, panel, ready, registration, mcp

    def test_default_install_does_not_register_mcp_or_enable_hooks(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime, prerequisites, collector, panel, ready, registration, mcp = self.activate(Path(directory), [])
            self.assertFalse(prerequisites.call_args.args[2])
            registration.assert_not_called()
            mcp.assert_not_called()
            self.assertNotIn('--with-hooks', collector.call_args.args[0])
            self.assertIn('9000', panel.call_args.args[0])
            ready.assert_called_once_with(9000)
            self.assertEqual(runtime.stat().st_mode & 0o777, 0o700)
            self.assertFalse(json.loads((runtime / 'installation.json').read_text())['mcp_requested'])

    def test_optional_flags_are_explicitly_propagated(self):
        with tempfile.TemporaryDirectory() as directory:
            result = self.activate(Path(directory), ['--with-mcp', '--with-hooks'])
            runtime, prerequisites, collector, _, _, registration, mcp = result
            self.assertTrue(prerequisites.call_args.args[2])
            self.assertIn('--with-hooks', collector.call_args.args[0])
            registration.assert_called_once()
            mcp.assert_called_once_with(runtime.resolve(), (Path(directory) / 'codex').resolve())

    def test_missing_platform_or_tools_produce_actionable_errors(self):
        with patch('install.platform.system', return_value='Linux'), self.assertRaisesRegex(ValueError, 'macOS'):
            install.check_prerequisites(Path('/codex'), Path(sys.executable), False)
        with patch('install.platform.system', return_value='Darwin'), \
             patch('install.subprocess.run', return_value=Mock(returncode=1)), \
             self.assertRaisesRegex(ValueError, 'xcode-select --install'):
            install.check_prerequisites(Path('/codex'), Path(sys.executable), False)

    def test_unrelated_mcp_registration_is_not_overwritten(self):
        response = Mock(returncode=0, stdout=json.dumps({'transport': {'command': '/unrelated/server'}}))
        with patch('install.subprocess.run', return_value=response) as run, \
             self.assertRaisesRegex(ValueError, 'not replaced'):
            install.check_mcp_registration(Path('/cut/server'), Path('/codex'))
        self.assertEqual(run.call_count, 1)

    def test_busy_port_selects_an_available_port_for_fresh_install(self):
        probe = Mock()
        probe.bind.side_effect = [OSError('busy'), None]
        probe.getsockname.return_value = ('127.0.0.1', 9999)
        context = Mock()
        context.__enter__ = Mock(return_value=probe)
        context.__exit__ = Mock(return_value=False)
        with patch('install.read_plist', return_value=None), patch('install.socket.socket', return_value=context):
            self.assertEqual(install.choose_port(8767, Path('/runtime')), 9999)


if __name__ == '__main__': unittest.main()
