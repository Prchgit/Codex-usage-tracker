"""Shared runtime defaults and validation, without initialization side effects."""
import math
import os
from pathlib import Path
from datetime import datetime, timezone

DEFAULT_PORT = 8767
PANEL_HOSTNAME = 'codex-usage-tracker.localhost'
DEFAULT_POLL_INTERVAL = 2.0
DEFAULT_INACTIVITY_SECONDS = 120
DEFAULT_HISTORY_LIMIT = 100
DEFAULT_HOOK_LOOKBACK_SECONDS = 600
DEFAULT_PROVIDER_TIMEOUT = 120
DEFAULT_PREVIEW_TTL_SECONDS = 3600
DEFAULT_ACCOUNT_USAGE_MAX_AGE = 120
DEFAULT_ACCOUNT_REFRESH_INTERVAL = 60
DEFAULT_LIMITS_REQUEST_TIMEOUT = 20
DEFAULT_DATABASE_TIMEOUT = 20
STATE_DATABASE_TIMEOUT = 0.2
TOKENS_PER_RATE_UNIT = 1_000_000
DEFAULT_RUNTIME_RELATIVE = '.local/share/codex-token-monitor'


def positive_number(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ValueError(f'{name} must be a finite positive number')
    return value


def runtime_directory(home=None):
    return Path(os.environ.get('CODEX_USAGE_TRACKER_RUNTIME_DIR') or (Path(home or Path.home()) / DEFAULT_RUNTIME_RELATIVE)).expanduser()


def parse_since(value):
    if value is None: return 0
    stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
    return stamp.timestamp()
