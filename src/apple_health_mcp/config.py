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

# Where the biweekly recalibration job persists its derived reference values
# (scripts/recalibration_check.sh -> scripts/calibrate.py --check). Beside the
# HRV baseline stats this is the file that carries the *calibrated* HR anchors
# ("hr_max", "resting_hr"); zones.py prefers them over the raw observed maxima,
# because the single highest reading in an export is a sensor artefact.
#
# Unlike the other paths this one is NOT bound to STATE_DIR at import time.
# It is machine-local state written by a launchd job on a schedule, so a
# sandboxed test that redirected STATE_DIR but not this constant would read the
# developer's real file — and would then pass or fail depending on whether the
# biweekly job had happened to fire. `calibration_reference_path()` resolves it
# through STATE_DIR at call time instead, so redirecting STATE_DIR is enough.
CALIBRATION_REFERENCE_NAME = "calibration_reference.json"
# Explicit override; None means "derive from STATE_DIR". Set from the
# CALIBRATION_REFERENCE env var — the same one the shell job reads — and
# monkeypatchable directly by tests that want one specific file.
CALIBRATION_REFERENCE_PATH = (
    Path(os.environ["CALIBRATION_REFERENCE"])
    if os.environ.get("CALIBRATION_REFERENCE") else None)


def calibration_reference_path() -> Path:
    """Absolute path of the recalibration reference file, resolved now.

    Reads the module globals on every call, so both `CALIBRATION_REFERENCE_PATH`
    and `STATE_DIR` work as monkeypatch points.
    """
    return CALIBRATION_REFERENCE_PATH or (STATE_DIR / CALIBRATION_REFERENCE_NAME)


# Per-athlete absolute-bpm training zones (recovery / easy / grey / threshold /
# vo2max) used by zones.py alongside the %-of-max Z1-Z5 model. This is personal
# configuration, so it lives in a JSON file rather than in code; the shipped
# default sits in config/ at the repo root (NOT in data/, which is git-ignored).
# Overridable via HEALTH_ZONES_CONFIG (used by tests).
ZONES_CONFIG_DIR = PROJECT_ROOT / "config"
DEFAULT_ZONES_CONFIG_PATH = ZONES_CONFIG_DIR / "training_zones.json"
ZONES_CONFIG_PATH = Path(os.environ.get(
    "HEALTH_ZONES_CONFIG", DEFAULT_ZONES_CONFIG_PATH))

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


# --- LAN delta sync (iPhone pushes, this process receives) --------------------
# The Readiness iOS app runs HKAnchoredObjectQuery and POSTs only the samples
# added since its last successful sync. The receiver lives inside the MCP server
# process (see sync_receiver.py), so receiving and importing share one process
# and can never contend for DuckDB's single writer.
#
# Wire contract v1 (an iOS client is built against these exact strings):
#   Bonjour service  _healthsync._tcp  in  local.
#   TXT records      v=1, did=<device_id>, path=/v1
#   Auth header      X-Health-Token: <base64url of 32 random bytes, unpadded>
SYNC_PROTOCOL_VERSION = 1
SYNC_SERVICE_TYPE = "_healthsync._tcp."      # zeroconf wants the trailing dot
SYNC_SERVICE_DOMAIN = "local."
SYNC_API_PREFIX = "/v1"
SYNC_AUTH_HEADER = "X-Health-Token"

# Spool: batches land here the moment they are received, BEFORE any import.
# Deliberately a sibling of the export drop-folder — same place, same backups,
# and a human can see the raw NDJSON if a sync is ever in doubt. Resolved at
# call time (like calibration_reference_path) so a test that redirects
# EXPORT_DIR gets the redirected spool too.
SYNC_SPOOL_DIR_NAME = "deltas"
SYNC_SPOOL_DIR = (Path(os.environ["HEALTH_SYNC_SPOOL_DIR"])
                  if os.environ.get("HEALTH_SYNC_SPOOL_DIR") else None)


def sync_spool_dir() -> Path:
    """Absolute path of the delta spool folder, resolved now."""
    return SYNC_SPOOL_DIR or (EXPORT_DIR / SYNC_SPOOL_DIR_NAME)


# Pairing state (shared token + this Mac's stable device id). Written 0600 into
# data/, which is git-ignored. Resolved at call time for the same reason.
SYNC_PAIRING_NAME = "sync_pairing.json"
SYNC_PAIRING_PATH = (Path(os.environ["HEALTH_SYNC_PAIRING"])
                     if os.environ.get("HEALTH_SYNC_PAIRING") else None)


def sync_pairing_path() -> Path:
    """Absolute path of the pairing file (token + device id), resolved now."""
    return SYNC_PAIRING_PATH or (STATE_DIR / SYNC_PAIRING_NAME)


# Listener. Port 0 = ephemeral (the port travels in the Bonjour TXT record and
# in the pairing payload, so nothing needs a fixed number). Bind on all
# interfaces: the phone reaches the Mac over the LAN address, not loopback.
SYNC_BIND_HOST = os.environ.get("HEALTH_SYNC_BIND", "0.0.0.0")
SYNC_PORT = int(os.environ.get("HEALTH_SYNC_PORT", "0"))
# Set HEALTH_SYNC_DISABLED=1 to start the MCP server with no listener at all.
SYNC_DISABLED = os.environ.get("HEALTH_SYNC_DISABLED", "") not in ("", "0", "false", "no")

# Body limits. A day of anchored deltas is tens of KB; 32 MB compressed is a
# backfill of many months and still far below anything that could hurt. The
# decompressed cap exists because gzip is trivially bomb-able.
SYNC_MAX_BODY_BYTES = int(os.environ.get("HEALTH_SYNC_MAX_BODY", 32 * 1024 * 1024))
SYNC_MAX_DECOMPRESSED_BYTES = int(
    os.environ.get("HEALTH_SYNC_MAX_DECOMPRESSED", 512 * 1024 * 1024))
