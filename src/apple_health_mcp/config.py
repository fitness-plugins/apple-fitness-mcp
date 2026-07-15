"""Central configuration and path resolution.

Everything here resolves to absolute paths so the code behaves identically
whether it is launched from the shell, from a manual import, or from Claude
Desktop (which starts the server with an unpredictable working directory).
"""
from __future__ import annotations

import os
from pathlib import Path

# Project root = two levels up from this file (src/apple_health_mcp/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# DuckDB database file. Overridable for tests via HEALTH_DB.
DB_PATH = Path(os.environ.get("HEALTH_DB", PROJECT_ROOT / "data" / "health.duckdb"))

# Folder the Health export archive is dropped into. Overridable via
# HEALTH_EXPORT_DIR.
DEFAULT_EXPORT_DIR = Path.home() / "Documents" / "AppleHealthExport"
EXPORT_DIR = Path(os.environ.get("HEALTH_EXPORT_DIR", DEFAULT_EXPORT_DIR))

# Where the import pipeline records the last archive it processed, so a reload
# can skip re-importing an archive it already handled.
STATE_DIR = PROJECT_ROOT / "data"
IMPORT_STATE_PATH = STATE_DIR / "import_state.json"
LOG_DIR = PROJECT_ROOT / "logs"

# Deduplication: when the same metric is reported by multiple devices for
# overlapping time windows, prefer higher-priority sources. Higher number wins.
# Apple Watch generally has better sensors than the phone for HR/energy/etc.
SOURCE_PRIORITY = {
    "watch": 30,
    "apple watch": 30,
    "phone": 20,
    "iphone": 20,
    "ipad": 10,
}
DEFAULT_SOURCE_PRIORITY = 5


def source_priority(source_name: str | None) -> int:
    """Map a raw HealthKit sourceName to a dedup priority (higher wins)."""
    if not source_name:
        return DEFAULT_SOURCE_PRIORITY
    low = source_name.lower()
    for key, prio in SOURCE_PRIORITY.items():
        if key in low:
            return prio
    return DEFAULT_SOURCE_PRIORITY


def ensure_dirs() -> None:
    """Create the directories the pipeline writes to."""
    for d in (DB_PATH.parent, STATE_DIR, LOG_DIR, EXPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
