#!/bin/bash
# Biweekly recalibration drift check (Phase 4.3). Runs calibrate.py --check and
# surfaces a reminder ONLY when the baseline reference has drifted past tolerance
# (calibrate.py exits non-zero). Nothing here touches the live scoring path.
#
# Wired as a launchd job (launchd/com.applehealth.recalibration.plist) that fires
# every 14 days. Point HRV_HISTORY_CSV at a CSV (date,hrv) exported from the
# health DB; until that export exists the job logs "skip" and stays quiet.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
LOG_DIR="$REPO/logs"
LOG="$LOG_DIR/recalibration.log"
mkdir -p "$LOG_DIR"

HRV_CSV="${HRV_HISTORY_CSV:-$REPO/data/hrv_history.csv}"
REF="${CALIBRATION_REFERENCE:-$REPO/data/calibration_reference.json}"
TOL="${DRIFT_TOL:-0.10}"
ts="$(date '+%Y-%m-%d %H:%M:%S')"

if [ ! -f "$HRV_CSV" ]; then
  echo "$ts  skip: no HRV history CSV at $HRV_CSV" >> "$LOG"
  exit 0
fi

out="$(cd "$REPO" && uv run python scripts/calibrate.py --check \
        --hrv-history "$HRV_CSV" --reference "$REF" --tol "$TOL" 2>&1)"
code=$?
{
  echo "$ts  calibrate --check exit=$code"
  echo "$out"
} >> "$LOG"

if [ "$code" -ne 0 ]; then
  # Reminder fires only on drift. Notification is best-effort; the log is durable.
  /usr/bin/osascript -e 'display notification "HRV baseline drift detected — run calibrate.py fit and review suggested weights" with title "Readiness recalibration"' 2>/dev/null || true
  echo "$ts  REMINDER fired (drift exceeded tolerance $TOL)" >> "$LOG"
fi
exit 0
