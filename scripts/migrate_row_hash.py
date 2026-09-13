#!/usr/bin/env python3
"""One-time migration: recompute every `row_hash` under the new identity rules.

`storage.py` changed how a row's identity is derived (see the "row identity"
block there):

  * a quantity's identity is now its *number*, normalized to
    `storage.VALUE_SIG_DIGITS` significant digits, instead of `value_str` —
    Apple's own XML rendering, which the iOS app cannot reproduce;
  * category rows (`value IS NULL`) keep `value_str`, which is their category
    name and genuinely is their identity;
  * timestamps are rendered as canonical UTC to the second instead of
    `cast(ts AS VARCHAR)`, which formats in the DuckDB *session* timezone and so
    hashed the same instant differently depending on where the import ran.

Every existing `row_hash` was computed the old way, so without this migration
the first delta the phone sends duplicates the entire overlapping history.

The database is ~1.5 GB / ~5M rows and is irreplaceable. Accordingly:

  * Nothing is ever written to the live file until a full copy has been
    migrated *and* verified. The live file is then MOVED aside to
    `health.duckdb.pre_rowhash_<stamp>.bak` and left there. Nothing is deleted,
    ever — not the original, not a stale working copy.
  * Recomputing the hash can COLLAPSE rows that used to be distinct (two
    records differing only in `value_str` formatting, or hashed under two
    session timezones). That is the point of the change, but it is reported per
    table with worked examples, and the migration REFUSES to swap if any table
    collapses by more than --max-collapse-pct unless --allow-collapse is given.
    A large collapse means the new expression is too lossy and you need to know
    that before the swap, not after.
  * Resumable: the working copy and a state file live in a work directory, and
    `--resume` picks up at the first table that had not finished.

Usage:

    uv run python scripts/migrate_row_hash.py --dry-run   # migrate + report only
    uv run python scripts/migrate_row_hash.py             # ... and swap it in
    uv run python scripts/migrate_row_hash.py --resume
    uv run python scripts/migrate_row_hash.py --allow-collapse

After a successful swap `data/import_state.json` is rewritten with its archive
fingerprint removed, so the next `reload_data` / `apple-health-import` re-reads
the newest export in full and rehashes it under the new rules.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import duckdb

from apple_health_mcp import config, import_pipeline, storage

# Tables keyed by a content hash. `activity_summary` (keyed by date) and
# `import_runs` (no key) are untouched, and are checked for survival instead.
MIGRATED_TABLES = ["records", "workouts", "workout_events", "sleep", "clinical"]

# Views over the tables being rebuilt. Dropped before the rename (DuckDB will
# not rename a table something else depends on) and recreated from
# storage.init_schema afterwards.
VIEWS = ["records_dedup", "workout_events_dedup"]

_INDEX_NAME = re.compile(r"CREATE INDEX IF NOT EXISTS\s+([A-Za-z0-9_]+)", re.I)

# Columns worth showing when rows collapse, so a surprising number can be
# eyeballed rather than merely believed. Purely diagnostic.
WITNESS_COLUMNS = {
    "records": ["type", "source_name", "unit", "value", "value_str"],
    "workouts": ["type", "source_name"],
    "workout_events": ["event_kind", "raw_event_type", "duration"],
    "sleep": ["source_name", "raw_value"],
    "clinical": ["type", "identifier"],
}

SUFFIX_OLD = "__pre_rowhash"
DEFAULT_MAX_COLLAPSE_PCT = 1.0
# Peak usage is the live file plus a working copy that briefly holds both the
# old and the new copy of the largest table.
FREE_SPACE_FACTOR = 3.0


# --- small helpers ------------------------------------------------------------

def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%SZ")


def _connect(path: Path, read_only: bool = False, retries: int = 8,
             delay: float = 0.5) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB file, retrying briefly on lock contention.

    Mirrors storage.connect(), but for an arbitrary path (the working copy is
    not config.DB_PATH).
    """
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return duckdb.connect(str(path), read_only=read_only)
        except (duckdb.IOException, duckdb.Error) as exc:
            last = exc
            time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT count(*) FROM duckdb_tables() WHERE table_name = ?", [name]
    ).fetchone()[0] > 0


def _columns(con, table: str) -> list[str]:
    return [r[1] for r in con.execute(
        f"PRAGMA table_info({_q(table)})").fetchall()]


def _count(con, table: str) -> int:
    return con.execute(f"SELECT count(*) FROM {_q(table)}").fetchone()[0]


def _index_names() -> list[str]:
    out = []
    for stmt in storage.INDEXES:
        m = _INDEX_NAME.search(stmt)
        if m:
            out.append(m.group(1))
    return out


def _log(msg: str) -> None:
    print(f"[row-hash] {msg}", flush=True)


# --- state --------------------------------------------------------------------

class State:
    """Small JSON file recording how far the migration got.

    Written after every table so `--resume` can skip finished work. Keyed on the
    source file's size+mtime: if the live database changed since the working
    copy was taken, resuming would migrate stale data, so it is refused.
    """

    def __init__(self, path: Path):
        self.path = path
        self.data: dict = {}
        if path.exists():
            try:
                self.data = json.loads(path.read_text())
            except (ValueError, OSError):
                self.data = {}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, default=str))
        os.replace(tmp, self.path)

    @property
    def done(self) -> list[str]:
        return list(self.data.get("done_tables", []))

    def mark_done(self, table: str, stats: dict) -> None:
        done = self.data.setdefault("done_tables", [])
        if table not in done:
            done.append(table)
        self.data.setdefault("tables", {})[table] = stats
        self.save()


def _source_signature(src: Path) -> dict:
    st = src.stat()
    return {"path": str(src), "size": st.st_size, "mtime": int(st.st_mtime)}


# --- the migration ------------------------------------------------------------

def _drop_views_and_indexes(con) -> None:
    for v in VIEWS:
        con.execute(f"DROP VIEW IF EXISTS {_q(v)}")
    for idx in _index_names():
        con.execute(f"DROP INDEX IF EXISTS {_q(idx)}")


def _rebuild_sql(table: str, cols: list[str], hash_expr: str,
                 overrides: dict) -> str:
    """INSERT ... SELECT that rewrites one table under the new hash.

    Collapsed duplicates are resolved by keeping the row with the smallest OLD
    row_hash, so the choice is deterministic and a re-run picks the same
    survivor. The computed hash is aliased `__new_hash` rather than `row_hash`
    precisely so the window's `ORDER BY row_hash` still means the old column.
    """
    old = table + SUFFIX_OLD
    rest = [c for c in cols if c != "row_hash"]
    inner_rest = ", ".join(
        (f"{overrides[c]} AS {_q(c)}" if c in overrides else _q(c))
        for c in rest
    )
    outer_cols = ", ".join([_q("__new_hash")] + [_q(c) for c in rest])
    target_cols = ", ".join([_q("row_hash")] + [_q(c) for c in rest])
    return (
        f"INSERT INTO {_q(table)} ({target_cols})\n"
        f"SELECT {outer_cols} FROM (\n"
        f"  SELECT {hash_expr} AS {_q('__new_hash')}, {inner_rest},\n"
        f"         row_number() OVER (PARTITION BY {hash_expr} "
        f"ORDER BY {_q('row_hash')}) AS {_q('__rn')}\n"
        f"  FROM {_q(old)}\n"
        f") WHERE {_q('__rn')} = 1"
    )


def _collapse_examples(con, table: str, hash_expr: str, limit: int = 5
                       ) -> list[dict]:
    """A few groups of rows that the new hash merges, with what differed.

    Diagnostic only: any failure here is swallowed, it must never be the reason
    a verified migration does not happen.
    """
    old = table + SUFFIX_OLD
    witnesses = [c for c in WITNESS_COLUMNS.get(table, [])
                 if c in _columns(con, old)]
    if not witnesses:
        return []
    agg = ", ".join(
        f"min(cast({_q(c)} AS VARCHAR)) AS {_q(c + '_min')}, "
        f"max(cast({_q(c)} AS VARCHAR)) AS {_q(c + '_max')}"
        for c in witnesses
    )
    sql = (
        f"SELECT count(*) AS n, {agg} FROM {_q(old)} "
        f"GROUP BY {hash_expr} HAVING count(*) > 1 "
        f"ORDER BY n DESC LIMIT {int(limit)}"
    )
    try:
        rows = con.execute(sql).fetchall()
        names = [d[0] for d in con.description]
    except duckdb.Error as exc:          # pragma: no cover - diagnostic only
        _log(f"  (could not sample collapsed groups for {table}: {exc})")
        return []
    return [dict(zip(names, r)) for r in rows]


def _verify_table(con, table: str, hash_expr: str, overrides: dict,
                  sample: int) -> dict:
    """Recompute the hashes of the migrated table and compare to what is stored.

    Proves the projection put every value in the column the expression reads —
    a scrambled column order would produce different hashes here even though the
    INSERT itself succeeded.
    """
    scope = (f"(SELECT * FROM {_q(table)} LIMIT {int(sample)})"
             if sample else _q(table))
    checked = con.execute(f"SELECT count(*) FROM {scope} t").fetchone()[0]
    bad = con.execute(
        f"SELECT count(*) FROM {scope} t "
        f"WHERE t.{_q('row_hash')} IS DISTINCT FROM ({hash_expr})"
    ).fetchone()[0]
    bad_derived = {}
    for col, expr in overrides.items():
        bad_derived[col] = con.execute(
            f"SELECT count(*) FROM {scope} t "
            f"WHERE t.{_q(col)} IS DISTINCT FROM ({expr})"
        ).fetchone()[0]
    dupes = con.execute(
        f"SELECT count(*) FROM (SELECT {_q('row_hash')} FROM {_q(table)} "
        f"GROUP BY 1 HAVING count(*) > 1)"
    ).fetchone()[0]
    return {"checked": checked, "hash_mismatches": bad,
            "derived_mismatches": bad_derived, "duplicate_row_hashes": dupes}


def _orphan_events(con, events_table: str, workouts_table: str) -> int:
    """workout_events rows whose workout_hash matches no workouts row."""
    if not (_table_exists(con, events_table)
            and _table_exists(con, workouts_table)):
        return 0
    return con.execute(
        f"SELECT count(*) FROM {_q(events_table)} e "
        f"LEFT JOIN {_q(workouts_table)} w ON w.row_hash = e.workout_hash "
        f"WHERE w.row_hash IS NULL"
    ).fetchone()[0]


def _migrate_one(con, table: str, sample: int) -> dict:
    hash_expr = storage.HASH_EXPRESSIONS[table]
    overrides = storage.DERIVED_HASH_COLUMNS.get(table, {})
    old = table + SUFFIX_OLD

    if not _table_exists(con, old):
        if not _table_exists(con, table):
            raise RuntimeError(f"neither {table} nor {old} exists")
        con.execute(f"ALTER TABLE {_q(table)} RENAME TO {_q(old)}")
    # From here the rebuild is re-entrant: a half-written `table` is thrown away
    # and rebuilt from the untouched `old`.
    con.execute(f"DROP TABLE IF EXISTS {_q(table)}")
    con.execute(storage.SCHEMA)          # recreates just the missing table

    cols = _columns(con, old)
    before = _count(con, old)
    distinct_new = con.execute(
        f"SELECT count(DISTINCT {hash_expr}) FROM {_q(old)}").fetchone()[0]

    con.execute(_rebuild_sql(table, cols, hash_expr, overrides))
    after = _count(con, table)

    if after != distinct_new:
        raise RuntimeError(
            f"{table}: wrote {after} rows but the new hash has "
            f"{distinct_new} distinct values — refusing to continue")

    verify = _verify_table(con, table, hash_expr, overrides, sample)
    if verify["hash_mismatches"] or verify["duplicate_row_hashes"] or \
            any(verify["derived_mismatches"].values()):
        raise RuntimeError(f"{table}: verification failed: {verify}")

    collapsed = before - after
    examples = _collapse_examples(con, table, hash_expr) if collapsed else []
    return {"before": before, "after": after, "collapsed": collapsed,
            "collapsed_pct": (100.0 * collapsed / before) if before else 0.0,
            "verify": verify, "examples": examples}


# --- reporting ----------------------------------------------------------------

def _print_report(results: dict, orphans_before: int, orphans_after: int,
                  view_counts: dict) -> None:
    _log("")
    _log("per-table result")
    _log(f"  {'table':<16}{'before':>12}{'after':>12}{'collapsed':>12}"
         f"{'pct':>9}{'verified':>11}")
    for t in MIGRATED_TABLES:
        r = results.get(t)
        if not r:
            continue
        _log(f"  {t:<16}{r['before']:>12,}{r['after']:>12,}"
             f"{r['collapsed']:>12,}{r['collapsed_pct']:>8.3f}%"
             f"{r['verify']['checked']:>11,}")
    for t in MIGRATED_TABLES:
        r = results.get(t)
        if not r or not r["examples"]:
            continue
        _log("")
        _log(f"  {t}: rows the new hash merges (up to 5 groups, "
             f"min/max of each field across the group)")
        for ex in r["examples"]:
            n = ex.get("n")
            fields = ", ".join(
                f"{k[:-4]}={ex[k]!r}..{ex[k[:-4] + '_max']!r}" for k in ex
                if k.endswith("_min") and ex[k] != ex.get(k[:-4] + "_max"))
            same = ", ".join(
                f"{k[:-4]}={ex[k]!r}" for k in ex
                if k.endswith("_min") and ex[k] == ex.get(k[:-4] + "_max"))
            _log(f"    {n} rows -> 1 | differed: {fields or '(nothing shown)'}"
                 f" | shared: {same}")
    _log("")
    _log(f"  workout_events with no parent workout: {orphans_before} before, "
         f"{orphans_after} after")
    for v, n in view_counts.items():
        _log(f"  view {v}: {n:,} rows")


# --- import state -------------------------------------------------------------

def _invalidate_import_state(state_path: Path, stamp: str) -> str | None:
    """Drop the archive fingerprint so the next export is re-imported in full.

    Written here rather than in import_pipeline.py on purpose: this is a
    migration side effect, not import logic. `import_pipeline._load_state`
    tolerates any shape, and with `last_fingerprint` gone its
    `state.get("last_fingerprint") == fp` check can no longer match anything, so
    `reload()` stops answering "already_current" and re-imports.
    """
    data: dict = {}
    if state_path.exists():
        try:
            data = json.loads(state_path.read_text())
        except (ValueError, OSError):
            data = {}
    previous = data.pop("last_fingerprint", None)
    data["row_hash_migration"] = {
        "at": stamp,
        "invalidated_fingerprint": previous,
        "value_sig_digits": storage.VALUE_SIG_DIGITS,
        "note": ("row_hash expressions changed (numeric identity + canonical "
                 "UTC timestamps); the next full export must be re-imported so "
                 "every row is rehashed."),
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_name(state_path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    os.replace(tmp, state_path)
    return previous


# --- driver -------------------------------------------------------------------

def _preflight(src: Path, workdir: Path, skip_space_check: bool) -> dict:
    """Checkpoint the live file, prove the new SQL binds, take before-counts."""
    con = _connect(src)
    try:
        con.execute("CHECKPOINT")
        for table, expr in storage.HASH_EXPRESSIONS.items():
            con.execute(f"SELECT {expr} FROM {_q(table)} LIMIT 0")
        for table, overrides in storage.DERIVED_HASH_COLUMNS.items():
            for expr in overrides.values():
                con.execute(f"SELECT {expr} FROM {_q(table)} LIMIT 0")
        counts = storage.table_counts(con)
    finally:
        con.close()
    _log("new hash expressions bind against this DuckDB build "
         f"({duckdb.__version__}).")

    size = src.stat().st_size
    if not skip_space_check:
        free = shutil.disk_usage(workdir).free
        need = int(size * FREE_SPACE_FACTOR)
        if free < need:
            raise SystemExit(
                f"[row-hash] Only {free / 1e9:.1f} GB free at {workdir}; this "
                f"needs about {need / 1e9:.1f} GB (a working copy that briefly "
                f"holds each table twice, and the backup of the original is "
                f"kept). Free some space or pass --skip-space-check.")
    return counts


def migrate(src: Path, workdir: Path, *, dry_run: bool, resume: bool,
            allow_collapse: bool, max_collapse_pct: float,
            sample: int, skip_space_check: bool = False) -> int:
    stamp = _now_stamp()
    workdir.mkdir(parents=True, exist_ok=True)
    working = workdir / "working.duckdb"
    state = State(workdir / "state.json")

    signature = _source_signature(src)
    reuse = False
    if resume and working.exists():
        if state.data.get("source") == signature:
            reuse = True
            _log(f"resuming; {len(state.done)} table(s) already migrated: "
                 f"{', '.join(state.done) or '-'}")
        else:
            raise SystemExit(
                "[row-hash] --resume: the live database changed since the "
                "working copy was taken (size/mtime differ). Start a fresh run "
                "without --resume.")

    if not reuse:
        counts = _preflight(src, workdir, skip_space_check)
        if working.exists():
            # Never delete: an abandoned copy is moved aside, not removed.
            aside = workdir / f"working.duckdb.{stamp}.abandoned"
            os.replace(working, aside)
            _log(f"moved a previous working copy aside -> {aside.name}")
        for extra in (working.with_name(working.name + ".wal"),):
            if extra.exists():
                os.replace(extra, workdir / f"{extra.name}.{stamp}.abandoned")
        _log(f"copying {src} -> {working} "
             f"({src.stat().st_size / 1e9:.2f} GB) ...")
        shutil.copy2(src, working)
        # Recorded AFTER the copy on purpose: _preflight CHECKPOINTs the live
        # file, which moves its mtime. The pre-swap guard compares against this
        # signature, so it has to describe the file as it was copied.
        state.data = {"source": _source_signature(src), "started": stamp,
                      "before_counts": counts, "done_tables": [], "tables": {}}
        state.save()

    con = _connect(working)
    try:
        # Baseline that must be taken while both parent and child are pristine.
        if "orphans_before" not in state.data:
            state.data["orphans_before"] = _orphan_events(
                con, "workout_events", "workouts")
            state.save()
        orphans_before = state.data["orphans_before"]

        _drop_views_and_indexes(con)

        results: dict = dict(state.data.get("tables", {}))
        for table in MIGRATED_TABLES:
            if table in state.done and table in results:
                _log(f"{table}: already migrated, skipping")
                continue
            _log(f"{table}: rebuilding ...")
            t0 = time.time()
            stats = _migrate_one(con, table, sample)
            results[table] = stats
            state.mark_done(table, stats)
            _log(f"{table}: {stats['before']:,} -> {stats['after']:,} "
                 f"({stats['collapsed']:,} collapsed, "
                 f"{stats['collapsed_pct']:.3f}%) in {time.time() - t0:.1f}s")

        # Old copies are only dropped once every table verified, so a failure
        # anywhere above leaves the working copy fully recoverable.
        for table in MIGRATED_TABLES:
            con.execute(f"DROP TABLE IF EXISTS {_q(table + SUFFIX_OLD)}")

        storage.init_schema(con)         # indexes + records_dedup + wo_ev_dedup
        view_counts = {v: con.execute(
            f"SELECT count(*) FROM {_q(v)}").fetchone()[0] for v in VIEWS}
        orphans_after = _orphan_events(con, "workout_events", "workouts")
        after_counts = storage.table_counts(con)
        con.execute("CHECKPOINT")
    finally:
        con.close()

    _print_report(results, orphans_before, orphans_after, view_counts)

    report = {
        "stamp": stamp, "source": str(src), "working": str(working),
        "value_sig_digits": storage.VALUE_SIG_DIGITS,
        "hash_expressions": storage.HASH_EXPRESSIONS,
        "before_counts": state.data.get("before_counts"),
        "after_counts": after_counts,
        "tables": results,
        "orphans_before": orphans_before, "orphans_after": orphans_after,
        "view_counts": view_counts,
    }
    (workdir / "report.json").write_text(json.dumps(report, indent=2,
                                                    default=str))
    _log(f"report written to {workdir / 'report.json'}")

    # --- gates ---------------------------------------------------------------
    problems: list[str] = []
    if orphans_after > orphans_before:
        problems.append(
            f"workout_events orphans rose {orphans_before} -> {orphans_after}: "
            "the child rows no longer join to their parent workouts, which "
            "means the two hash expressions disagree")
    for table, r in results.items():
        if r["collapsed_pct"] > max_collapse_pct:
            problems.append(
                f"{table} collapsed {r['collapsed']:,} of {r['before']:,} rows "
                f"({r['collapsed_pct']:.3f}%), over the "
                f"{max_collapse_pct:.3f}% threshold")
    hard = [p for p in problems if "orphans rose" in p]
    soft = [p for p in problems if p not in hard]

    if hard:
        for p in hard:
            _log(f"ABORT: {p}")
        _log(f"The live database was NOT touched. The migrated copy is at "
             f"{working} for inspection.")
        return 2
    if soft and not allow_collapse:
        for p in soft:
            _log(f"ABORT: {p}")
        _log("A collapse this large means the new identity expression is "
             "losing real rows. Inspect the examples above and the working "
             "copy, then re-run with --allow-collapse if it is genuinely what "
             "you want.")
        _log(f"The live database was NOT touched. The migrated copy is at "
             f"{working}.")
        return 3
    if soft:
        for p in soft:
            _log(f"WARNING (allowed by --allow-collapse): {p}")

    if dry_run:
        _log("--dry-run: stopping before the swap. The migrated copy is at "
             f"{working}; re-run with --resume (without --dry-run) to swap it "
             "in.")
        return 0

    # --- swap ----------------------------------------------------------------
    backup = src.with_name(f"{src.name}.pre_rowhash_{stamp}.bak")
    src_wal = src.with_name(src.name + ".wal")
    work_wal = working.with_name(working.name + ".wal")
    if work_wal.exists():
        raise SystemExit(
            f"[row-hash] {work_wal} still exists after CHECKPOINT — refusing "
            "to swap in a database with an outstanding write-ahead log.")
    # Last line of defence: the import lock is held for the whole run, but if
    # anything wrote to the live file since the copy was taken, swapping would
    # silently discard those rows.
    if _source_signature(src) != state.data.get("source"):
        _log("ABORT: the live database changed while the migration ran "
             "(size/mtime differ from the copy). Nothing was swapped; the "
             f"migrated copy is at {working}. Quit Claude Desktop so nothing "
             "else holds the database, then run this again from scratch "
             "(without --resume).")
        return 5
    if src_wal.exists():
        os.replace(src_wal, src.with_name(
            f"{src_wal.name}.pre_rowhash_{stamp}.bak"))
    os.replace(src, backup)
    try:
        os.replace(working, src)
    except OSError:
        os.replace(backup, src)
        raise
    _log(f"swapped in. The previous database is kept at {backup.name} "
         "(nothing was deleted).")

    previous_fp = _invalidate_import_state(config.IMPORT_STATE_PATH, stamp)
    _log(f"import_state.json fingerprint cleared (was {previous_fp!r}); the "
         "next reload_data / apple-health-import will re-read the newest "
         "export in full.")
    state.data["swapped_at"] = stamp
    state.data["backup"] = str(backup)
    state.save()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", default=None,
                    help="database to migrate (default: config.DB_PATH)")
    ap.add_argument("--workdir", default=None,
                    help="where the working copy and state live "
                         "(default: <db dir>/row_hash_migration)")
    ap.add_argument("--dry-run", action="store_true",
                    help="migrate and verify a copy, report, do not swap")
    ap.add_argument("--resume", action="store_true",
                    help="continue an interrupted run from its working copy")
    ap.add_argument("--allow-collapse", action="store_true",
                    help="proceed even if a table collapses by more than "
                         "--max-collapse-pct")
    ap.add_argument("--max-collapse-pct", type=float,
                    default=DEFAULT_MAX_COLLAPSE_PCT,
                    help=f"per-table collapse ceiling in percent "
                         f"(default {DEFAULT_MAX_COLLAPSE_PCT})")
    ap.add_argument("--skip-space-check", action="store_true",
                    help="do not require free space for the working copy")
    ap.add_argument("--verify-sample", type=int, default=0,
                    help="recompute and compare only this many rows per table "
                         "(default 0 = every row)")
    args = ap.parse_args()

    src = Path(args.db).expanduser() if args.db else config.DB_PATH
    if not src.exists():
        _log(f"no database at {src}")
        return 1
    workdir = (Path(args.workdir).expanduser() if args.workdir
               else src.parent / "row_hash_migration")

    _log(f"database: {src}")
    _log(f"workdir : {workdir}")
    _log(f"identity: {storage.VALUE_SIG_DIGITS} significant digits, "
         f"timestamps as canonical UTC seconds")
    try:
        # Held for the whole run, not just the swap: an import that landed
        # between the copy and the swap would be thrown away by it.
        with import_pipeline.import_lock() as got:
            if not got:
                _log("an import is already running (import_lock is held). "
                     "Nothing was touched; try again when it finishes.")
                return 4
            return migrate(src, workdir, dry_run=args.dry_run,
                           resume=args.resume,
                           allow_collapse=args.allow_collapse,
                           max_collapse_pct=args.max_collapse_pct,
                           sample=max(0, args.verify_sample),
                           skip_space_check=args.skip_space_check)
    except SystemExit:
        raise
    except Exception as exc:                     # noqa: BLE001
        _log(f"FAILED: {exc.__class__.__name__}: {exc}")
        _log("The live database was not swapped. Re-run with --resume once the "
             "cause is fixed; finished tables are not redone.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
