"""Import spooled delta batches into DuckDB — the second half of the sync.

Kept apart from the receiver on purpose. The receiver's only job is to make a
batch durable and say 200; this module decides when that batch becomes rows.
If the process dies here, the batch is still in ``deltas/pending`` and the next
call picks it up — the phone's HealthKit anchor has already moved on, so a lost
batch is lost data with no way to notice.

Everything goes through ``storage.import_stream`` — the same loader the full
XML export uses, fed by the thin ``sync_protocol`` adapter. There is one set of
dedup rules in this project, not two.
"""
from __future__ import annotations

import time
import traceback
from typing import Optional

from . import config, import_pipeline, storage, sync_protocol, sync_spool

# Upper bound on `import_from_app(wait_seconds=...)`: the tool blocks an MCP
# call, and a client that waits minutes for a tool looks hung.
MAX_WAIT_SECONDS = 300
POLL_SECONDS = 0.5

ARCHIVE_PREFIX = "delta:"


def wait_for_pending(spool: sync_spool.Spool, wait_seconds: float,
                     poll: float = POLL_SECONDS) -> float:
    """Poll the spool until something is pending. Returns seconds waited.

    This is what lets a conversation say "open the app now" and answer with the
    result instead of a shrug.
    """
    deadline = time.monotonic() + max(0.0, min(wait_seconds, MAX_WAIT_SECONDS))
    started = time.monotonic()
    while True:
        if spool.pending():
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(poll)
    return round(time.monotonic() - started, 2)


def _empty_added() -> dict:
    return {"records": 0, "workouts": 0, "workout_events": 0, "sleep": 0,
            "clinical": 0, "activity_summary": 0}


def import_pending(wait_seconds: float = 0,
                   spool: Optional[sync_spool.Spool] = None,
                   poll: float = POLL_SECONDS) -> dict:
    """Import every pending batch, oldest first. Never raises.

    Each batch is imported on its own so that one unreadable batch cannot block
    the ones behind it: it is moved to ``deltas/failed/`` with the traceback
    beside it (kept, never discarded) and the rest continue.
    """
    config.ensure_dirs()
    spool = spool or sync_spool.Spool()
    try:
        spool.ensure()
    except OSError as exc:
        return {"status": "error", "message": f"Spool unavailable: {exc}",
                "spool_dir": str(spool.root)}

    waited = wait_for_pending(spool, wait_seconds, poll) if wait_seconds else 0.0
    pending = spool.pending()
    if not pending:
        return {"status": "empty", "waited_seconds": waited,
                "message": ("No batches waiting. The phone only delivers while "
                            "Claude Desktop is running — open the Readiness app "
                            "in the foreground and call this again, or check "
                            "sync_status() to see whether the listener is up "
                            "and the device is paired."),
                "spool_dir": str(spool.root),
                "added": _empty_added(), "batches": 0, "details": []}

    with import_pipeline.import_lock() as got:
        if not got:
            return {"status": "busy", "waited_seconds": waited,
                    "message": ("Another import is running (a full export "
                                "reload?); the batches stay spooled — try "
                                "again in a moment."),
                    "pending": len(pending), "added": _empty_added(),
                    "batches": 0, "details": []}

        added = _empty_added()
        details: list[dict] = []
        failed: list[dict] = []
        totals: dict = {}
        # Re-read inside the lock: another import (or a manual reload) may have
        # taken these batches while we were waiting for it.
        pending = spool.pending()
        con = storage.connect()
        try:
            storage.init_schema(con)
            for batch in pending:
                if not batch.path.exists():
                    continue
                adapter = sync_protocol.BatchAdapter()
                try:
                    stats = storage.import_stream(
                        con, adapter.iter_events(batch.iter_lines()),
                        archive_name=f"{ARCHIVE_PREFIX}{batch.batch_id}")
                except Exception:
                    error = traceback.format_exc()
                    try:
                        name = spool.mark_failed(batch, error).path.name
                    except OSError as exc:      # e.g. taken by another import
                        name = f"{batch.path.name} (could not be parked: {exc})"
                    failed.append({"batch_id": batch.batch_id, "file": name,
                                   "error": error.strip().splitlines()[-1]})
                    continue
                for table, count in stats["added"].items():
                    added[table] = added.get(table, 0) + count
                totals = stats["totals"]
                summary = adapter.summary()
                result = {"batch_id": batch.batch_id,
                          "received_at": batch.received_at,
                          "added": stats["added"], **summary}
                spool.mark_imported(batch, result)
                details.append(result)
        finally:
            con.close()

    if details and failed:
        status = "partial"
    elif details:
        status = "imported"
    elif failed:
        status = "error"
    else:
        # Everything vanished between the two listings: another import took it.
        return {"status": "empty", "waited_seconds": waited, "batches": 0,
                "added": added, "details": [], "failed": [],
                "message": "Another import had already taken these batches.",
                "spool_dir": str(spool.root),
                "pending_after": spool.count(sync_spool.PENDING)}
    if details:
        message = (f"Imported {len(details)} batch(es) from the phone: "
                   f"+{added.get('records', 0)} records, "
                   f"+{added.get('workouts', 0)} workouts, "
                   f"+{added.get('workout_events', 0)} workout steps, "
                   f"+{added.get('sleep', 0)} sleep segments.")
    else:
        message = "No batch could be imported."
    if failed:
        message += (f" {len(failed)} batch(es) could not be imported and were "
                    f"moved to {spool.dir_for(sync_spool.FAILED)} — kept, not "
                    "deleted, so nothing the phone sent is lost.")
    return {"status": status, "waited_seconds": waited,
            "batches": len(details), "added": added, "totals": totals,
            "details": details, "failed": failed,
            "pending_after": spool.count(sync_spool.PENDING),
            "spool_dir": str(spool.root), "message": message}


def last_delta_import(con) -> Optional[dict]:
    """Most recent delta batch recorded in `import_runs`, for sync_status.

    Reads the audit trail `storage.import_stream` writes anyway, so the answer
    survives a restart (the in-memory counters do not).
    """
    rows = con.execute(
        "SELECT archive_name, imported_at FROM import_runs "
        "WHERE archive_name LIKE ? ORDER BY imported_at DESC LIMIT 1",
        (ARCHIVE_PREFIX + "%",)).fetchall()
    if not rows:
        return None
    name, when = rows[0]
    return {"batch_id": name[len(ARCHIVE_PREFIX):],
            "imported_at": when.isoformat() if hasattr(when, "isoformat") else str(when)}
