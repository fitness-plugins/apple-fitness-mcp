#!/usr/bin/env python3
"""Populate `workout_events` on an existing database — workouts only.

The `workout_events` table (interval repetitions and segment boundaries) was
added after the main database was already built. That database holds ~5 million
<Record> rows; a full `reload_data(force=true)` would re-parse and re-hash every
one of them just to write a few thousand event rows.

This script does the targeted thing instead: stream the export .zip, parse ONLY
<Workout> elements (`parser.iter_workouts_only`), and insert into
`workout_events`. No other table is read or written. On a 1.15 GB export the
whole pass is seconds, not minutes, and memory stays flat — the export.xml is
never extracted to disk, it is read straight out of the archive.

Idempotent and safe to re-run: rows carry a content-derived `row_hash` primary
key and are inserted with ON CONFLICT DO NOTHING, so a second run adds nothing.
Nothing ever needs clearing first — the table is only ever added to, and the
per-source duplicates it faithfully records are collapsed at read time by the
`workout_events_dedup` view, which is what callers should query.

    uv run python scripts/backfill_workout_events.py            # newest export
    uv run python scripts/backfill_workout_events.py path/to/export.zip
    uv run python scripts/backfill_workout_events.py --dry-run  # parse, no write
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

from apple_health_mcp import config, import_pipeline, parser, storage


def _export_xml_name(zf: zipfile.ZipFile) -> str | None:
    """The export.xml member inside the archive (never export_cda.xml)."""
    names = [n for n in zf.namelist()
             if n.endswith("export.xml") and not n.endswith("export_cda.xml")]
    if not names:
        return None
    # Prefer the shallowest match, mirroring import_pipeline._find_export_xml.
    return min(names, key=lambda n: n.count("/"))


def _link_report(con) -> dict:
    """How the freshly-written events line up with the `workouts` table."""
    total = con.execute("SELECT count(*) FROM workout_events").fetchone()[0]
    deduped = con.execute(
        "SELECT count(*) FROM workout_events_dedup").fetchone()[0]
    reps = con.execute(
        "SELECT count(*) FROM workout_events_dedup "
        "WHERE event_kind = 'activity'").fetchone()[0]
    orphans = con.execute(
        "SELECT count(*) FROM workout_events e "
        "LEFT JOIN workouts w ON w.row_hash = e.workout_hash "
        "WHERE w.row_hash IS NULL"
    ).fetchone()[0]
    linked_workouts = con.execute(
        "SELECT count(DISTINCT e.workout_hash) FROM workout_events e "
        "JOIN workouts w ON w.row_hash = e.workout_hash"
    ).fetchone()[0]
    interval_sessions = con.execute(
        "SELECT count(DISTINCT workout_hash) FROM workout_events "
        "WHERE event_kind = 'activity'"
    ).fetchone()[0]
    return {"total": total, "deduped": deduped, "reps": reps,
            "orphans": orphans,
            "linked_workouts": linked_workouts,
            "interval_sessions": interval_sessions}


def backfill(archive: Path, *, dry_run: bool = False) -> int:
    archive = Path(archive)
    if not archive.exists():
        print(f"[backfill] ERROR: {archive} does not exist.", file=sys.stderr)
        return 1

    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        print(f"[backfill] ERROR: {archive.name} is not a valid zip ({exc}).",
              file=sys.stderr)
        return 1

    with zf:
        member = _export_xml_name(zf)
        if member is None:
            print(f"[backfill] ERROR: no export.xml inside {archive.name}.",
                  file=sys.stderr)
            return 1
        print(f"[backfill] Reading {member} from {archive.name} "
              f"(streamed, not extracted) ...")

        if dry_run:
            workouts = events = 0
            kinds: dict[str, int] = {}
            with zf.open(member) as fh:
                for kind, payload in parser.iter_workouts_only(fh):
                    if kind == "workout":
                        workouts += 1
                    else:
                        events += 1
                        k = payload["event_kind"]
                        kinds[k] = kinds.get(k, 0) + 1
            print(f"[backfill] DRY RUN: {workouts} workouts, {events} events "
                  f"{kinds}. Nothing written.")
            return 0

        with import_pipeline.import_lock() as got:
            if not got:
                print("[backfill] ERROR: an import is already running; try "
                      "again in a moment.", file=sys.stderr)
                return 1

            con = storage.connect()
            try:
                storage.init_schema(con)   # creates workout_events if missing
                before = con.execute(
                    "SELECT count(*) FROM workout_events").fetchone()[0]

                buf: list[tuple] = []
                workouts = events = 0
                with zf.open(member) as fh:
                    for kind, payload in parser.iter_workouts_only(fh):
                        if kind == "workout":
                            workouts += 1
                            continue
                        events += 1
                        buf.append(storage.workout_event_row(payload))
                        if len(buf) >= storage.BATCH:
                            storage.flush_workout_events(con, buf)
                            buf = []
                storage.flush_workout_events(con, buf)

                report = _link_report(con)
            finally:
                con.close()

    added = report["total"] - before
    print(f"[backfill] Parsed {workouts} workouts -> {events} structural rows; "
          f"inserted {added} new (table now {report['total']}).")
    print(f"[backfill] Linked to {report['linked_workouts']} workout rows; "
          f"{report['interval_sessions']} of them are structured interval "
          f"sessions (have <WorkoutActivity> repetitions).")
    print(f"[backfill] workout_events_dedup: {report['deduped']} rows "
          f"({report['reps']} interval repetitions). The base table holds "
          f"{report['total']} — the difference is the same session exported "
          f"repeatedly under different source names. Query the VIEW.")
    if report["orphans"]:
        print(f"[backfill] WARNING: {report['orphans']} event rows do not match "
              "any row in `workouts`. Their workout_hash is recomputed with the "
              "same expression as workouts.row_hash, so a mismatch means the "
              "parent workouts were imported from a different export, or the "
              "database's session timezone changed since that import (the hash "
              "renders TIMESTAMPTZ as text). Re-run `apple-health-import` on "
              "this same archive, then re-run this script.", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("archive", nargs="?", default=None,
                    help="export .zip (default: newest in the export folder)")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and count only; write nothing")
    args = ap.parse_args()

    config.ensure_dirs()
    if args.archive:
        archive = Path(args.archive).expanduser()
    else:
        archive = import_pipeline.latest_archive()
        if archive is None:
            print(f"[backfill] No export .zip found in {config.EXPORT_DIR}.",
                  file=sys.stderr)
            return 1
    return backfill(archive, dry_run=args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
