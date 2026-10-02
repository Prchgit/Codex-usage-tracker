"""Install the native floating viewer; leave collector and hooks unchanged."""
from pathlib import Path
import plistlib
import subprocess
from install_common import PROJECT, activate_agent, install_arguments, read_plist, run_installer, validate_options

LABEL = 'com.local.codex-usage-panel'
APP_NAME = 'Codex Usage.app'
BINARY_NAME = 'CodexUsagePanel'


def main(argv=None):
    parser = install_arguments(__doc__)
    parser.add_argument('--clang', type=Path, default=Path('/usr/bin/clang'))
    args = parser.parse_args(argv)
    validate_options(parser, args)
    contents = args.runtime / APP_NAME / 'Contents'
    binary = contents / 'MacOS' / BINARY_NAME
    plist = Path.home() / 'Library/LaunchAgents' / (LABEL + '.plist')
    existing = read_plist(plist)
    if existing and existing.get('ProgramArguments') != [str(binary)]:
        raise ValueError('An unrelated launch agent uses this label')
    binary.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([str(args.clang), '-fobjc-arc', '-fmodules',
        '-fmodules-cache-path=' + str(args.runtime / 'clang-cache'),
        str(PROJECT / 'native/UsagePanel.m'), str(PROJECT / 'native/ScreenDetection.m'),
        '-o', str(binary), '-framework', 'Cocoa'], check=True)
    contents.joinpath('Info.plist').write_bytes(plistlib.dumps({
        'CFBundleExecutable': BINARY_NAME, 'CFBundleIdentifier': LABEL,
        'CFBundleName': 'Codex Usage Tracker', 'CFBundleDisplayName': 'Codex Usage Tracker',
        'CFBundlePackageType': 'APPL', 'LSUIElement': True, 'NSHighResolutionCapable': True, 'CFBundleVersion': '1'}))
    activate_agent(plist, {'Label': LABEL, 'ProgramArguments': [str(binary)], 'RunAtLoad': True,
        'EnvironmentVariables': {'CUT_DASHBOARD_URL': f'http://127.0.0.1:{args.port}/', 'CUT_POLL_INTERVAL': str(args.poll_interval)},
        'StandardErrorPath': str(args.runtime / 'panel.stderr.log'),
        'StandardOutPath': str(args.runtime / 'panel.stdout.log')})
    print('Floating view installed and started: ' + str(args.runtime / APP_NAME))


if __name__ == '__main__':
    run_installer(main)
