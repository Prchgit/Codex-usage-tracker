"""Shared user-level installer configuration and launch-agent operations."""
import argparse
import os
from pathlib import Path
import plistlib
import subprocess
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from token_budget_mcp.config import DEFAULT_PORT, DEFAULT_POLL_INTERVAL, positive_number, runtime_directory


def install_arguments(description):
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--runtime', type=Path, default=runtime_directory())
    parser.add_argument('--port', type=int, default=DEFAULT_PORT)
    parser.add_argument('--poll-interval', type=float, default=DEFAULT_POLL_INTERVAL)
    return parser


def validate_options(parser, args):
    try:
        if not 1 <= args.port <= 65535: raise ValueError('Port must be between 1 and 65535')
        positive_number(args.poll_interval, 'poll_interval')
    except ValueError as error:
        parser.error(str(error))
    args.runtime = args.runtime.expanduser().resolve()


def read_plist(path):
    try:
        return plistlib.loads(path.read_bytes()) if path.exists() else None
    except (OSError, ValueError, plistlib.InvalidFileException) as error:
        raise ValueError(f'Cannot read existing launch agent: {path}') from error


def activate_agent(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data))
    domain = f'gui/{os.getuid()}'
    # bootout may fail when this is the first installation.
    subprocess.run(['launchctl', 'bootout', domain, str(path)], capture_output=True)
    subprocess.run(['launchctl', 'bootstrap', domain, str(path)], check=True)


def run_installer(main):
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(f'Installation failed: {error}. Check paths, permissions and launchctl/compiler diagnostics.') from error
