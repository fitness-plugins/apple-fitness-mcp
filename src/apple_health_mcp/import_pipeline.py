"""End-to-end import: locate an export archive, stream-parse it, load.

Called three ways:
  * by setup.sh during initial install (schema init + import if data present)
  * by the `reload_data` MCP tool when the user asks Claude to reload
  * manually:  python -m apple_health_mcp.import_pipeline [archive_or_dir]

Nothing is ever unpacked to disk. A real export archive is ~62 MB compressed
and ~1.70 GB unpacked, of which the importer reads exactly one member:
`apple_health_export/export.xml` (1.15 GB). The other 550 MB -- `export_cda.xml`
(456 MB, opened by nothing in this repo) and 157 GPX workout-route files -- used
to be written to a temp dir and deleted unread. `ET.iterparse` accepts a file
object and `ZipFile.open()` returns one, so the XML is streamed straight out of
the archive instead.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import sys
import time
import zipfile
import zlib
from pathlib import Path

from . import config, parser, storage

# The one member the importer reads. Matching on the *basename* is what keeps
# `export_cda.xml` out: it is a different basename, not a suffix of this one.
_EXPORT_XML_NAME = "export.xml"

# Tag on every fingerprint this module writes. Untagged values in an existing
# `import_state.json` are from the pre-CRC scheme -- see `_already_imported`.
_FP_SCHEME = "crc1"
_FP_STAT_SCHEME = "stat1"


def _find_export_xml(root: Path) -> Path | None:
    """Locate export.xml within an *already unzipped* export.

    No longer used by the import path (which never unzips), kept for callers
    that hand this module an unpacked export directory. Ignores
    `export_cda.xml`, prefers the shallowest match.
    """
    candidates = [p for p in root.rglob("export.xml")]
    if candidates:
        return min(candidates, key=lambda p: len(p.parts))
    return None


def _find_export_entry(zf: zipfile.ZipFile) -> str | None:
    """Name of the export.xml member inside an export archive, or None.

    Mirrors `_find_export_xml`: basename match (so `export_cda.xml` can never
    be selected), shallowest path wins.
    """
    best: tuple[int, str] | None = None
    for info in zf.infolist():
        if info.is_dir():
            continue
        name = info.filename
        if name.rsplit("/", 1)[-1] != _EXPORT_XML_NAME:
            continue
        depth = name.count("/")
        if best is None or depth < best[0]:
            best = (depth, name)
    return best[1] if best else None


def _legacy_fingerprint(archive: Path) -> str:
    """The pre-CRC scheme: md5 of name|size|mtime.

    Retained only so a state file written by an older version can still be
    recognized once, during migration. Never written any more.
    """
    st = archive.stat()
    return hashlib.md5(
        f"{archive.name}|{st.st_size}|{int(st.st_mtime)}".encode()
    ).hexdigest()


def _archive_fingerprint(archive: Path) -> str:
    """Content identity of an export archive, without decompressing a byte.

    A zip's central directory already carries a CRC-32 and the uncompressed
    size of every member, so `getinfo("...export.xml").CRC` is exact content
    identity for the cost of reading a few hundred bytes at the tail of the
    file. The old scheme hashed name|size|mtime, which meant copying the
    archive, re-syncing it, or receiving it over AirDrop changed the
    fingerprint and forced a full re-parse of millions of byte-identical
    records.

    CRC-32 is a 32-bit checksum, not a cryptographic hash, so it is paired with
    the uncompressed size: a false "already imported" would need a collision in
    both. (The size alone already differs for essentially every real re-export,
    since exports grow.)

    Falls back to the legacy stat-based value -- under its own scheme tag -- if
    the file cannot be read as a zip at all, so that a truncated download or a
    non-zip file still gets a stable fingerprint here and is reported properly
    by `import_archive` a moment later instead of raising from this function.
    """
    try:
        with zipfile.ZipFile(archive) as zf:
            entry = _find_export_entry(zf)
            if entry is not None:
                info = zf.getinfo(entry)
                return f"{_FP_SCHEME}:{info.CRC:08x}:{info.file_size}"
    except (zipfile.BadZipFile, OSError):
        pass
    return f"{_FP_STAT_SCHEME}:{_legacy_fingerprint(archive)}"


def _load_state() -> dict:
    if config.IMPORT_STATE_PATH.exists():
        try:
            return json.loads(config.IMPORT_STATE_PATH.read_text())
        except (ValueError, OSError):
            return {}
    return {}


def _save_state(state: dict) -> None:
    config.IMPORT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.IMPORT_STATE_PATH.write_text(json.dumps(state, indent=2))


def _already_imported(archive: Path, fp: str) -> bool:
    """Has `archive` already been imported, per `data/import_state.json`?

    Handles the migration off the old name|size|mtime fingerprint. Old states
    hold a bare md5 with no scheme tag; a new CRC fingerprint can never equal
    one, so a naive `==` would force one pointless full re-parse of every
    record on the first run after this upgrade. Instead an untagged stored
    value is compared against the *legacy* computation, which preserves the old
    semantics for exactly that one transition, and the state file is rewritten
    in the new scheme so the legacy path is never taken twice.

    Both failure directions were considered. A legacy state that does not match
    falls through to a real import: a spurious import costs time but nothing
    else -- `storage.import_stream` is idempotent through per-row hashes and
    inserts zero rows -- whereas a spurious skip would silently leave new data
    out of the database, which no later step would notice or repair. So where
    the two cannot both be avoided, re-import is the safe direction; the
    legacy comparison is what lets the common case avoid both.
    """
    stored = _load_state().get("last_fingerprint")
    if not stored:
        return False
    if stored == fp:
        return True
    if ":" in stored:
        # A tagged fingerprint from this or another scheme: a mismatch is a
        # real mismatch, not a migration.
        return False
    if stored != _legacy_fingerprint(archive):
        return False
    print(f"[apple-health] {archive.name} matches the previous import under "
          "the old (name|size|mtime) fingerprint; migrating the state file to "
          "the content (CRC-32) fingerprint without re-importing.")
    _record_import(archive, fp)
    return True


def _record_import(archive: Path, fp: str) -> None:
    state = _load_state()
    state["last_fingerprint"] = fp
    state["last_archive"] = archive.name
    _save_state(state)


def latest_archive(export_dir: Path | None = None) -> Path | None:
    """Newest .zip in the export folder, if any."""
    export_dir = export_dir or config.EXPORT_DIR
    if not export_dir.exists():
        return None
    zips = sorted(export_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def import_archive(archive: Path, *, force: bool = False) -> dict | None:
    """Import a single export .zip. Returns stats, or None if skipped.

    The XML is streamed out of the archive; nothing is extracted. If the
    compressed stream turns out to be corrupt part-way through, the rows
    already flushed stay in the database (they are hash-deduplicated, so a
    later successful import simply completes the set) and this returns None so
    the caller reports an error -- and, critically, the fingerprint is *not*
    recorded, so the next run retries the whole archive.
    """
    archive = Path(archive)
    fp = _archive_fingerprint(archive)
    if not force and _already_imported(archive, fp):
        print(f"[apple-health] Already imported {archive.name}; skipping.")
        return None

    print(f"[apple-health] Importing {archive.name} ...")
    try:
        zf = zipfile.ZipFile(archive)
    except (zipfile.BadZipFile, OSError) as exc:
        print(f"[apple-health] ERROR: {archive.name} is not a valid zip "
              f"({exc}).", file=sys.stderr)
        return None

    with zf:
        entry = _find_export_entry(zf)
        if entry is None:
            print(f"[apple-health] ERROR: no export.xml found inside "
                  f"{archive.name}.", file=sys.stderr)
            return None

        con = storage.connect()
        try:
            storage.init_schema(con)
            with zf.open(entry) as fh:
                stats = storage.import_stream(
                    con, parser.iter_export(fh), archive_name=archive.name
                )
        except (zipfile.BadZipFile, zlib.error, EOFError) as exc:
            print(f"[apple-health] ERROR: {archive.name} is corrupt; the "
                  f"compressed export.xml could not be read to the end "
                  f"({exc}). Rows parsed before the failure were kept; the "
                  "archive is not marked as imported.", file=sys.stderr)
            return None
        finally:
            con.close()

    _record_import(archive, fp)
    added = stats["added"]
    seen = stats["seen"]
    print(f"[apple-health] Done. Parsed: {seen['record']:,} records, "
          f"{seen['workout']:,} workouts, {seen['sleep']:,} sleep segments. "
          f"Added: records +{added['records']}, "
          f"workouts +{added['workouts']}, sleep +{added['sleep']}. "
          f"Totals: {stats['totals']}")
    return stats


@contextlib.contextmanager
def import_lock(timeout: float = 120.0, poll: float = 0.5):
    """Exclusive advisory lock guarding imports.

    Ensures two imports can never run against the database at once (e.g. an MCP
    `reload_data` call while a manual `apple-health-import` is running).
    Yields True if the lock was acquired within `timeout`, else False.
    """
    config.ensure_dirs()
    lock_path = config.STATE_DIR / ".import.lock"
    f = open(lock_path, "w")
    acquired = False
    deadline = time.time() + timeout
    try:
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(poll)
        yield acquired
    finally:
        if acquired:
            fcntl.flock(f, fcntl.LOCK_UN)
        f.close()


def _current_totals() -> dict:
    con = storage.connect_readonly()
    try:
        return storage.table_counts(con)
    finally:
        con.close()


def reload(target: str | None = None, *, force: bool = False) -> dict:
    """Import the newest export on demand and return a structured result.

    Designed to be called by the MCP `reload_data` tool. Serialized via
    `import_lock`. Never raises — always returns a status dict.

    The result carries `seen` (how many elements the parser produced) next to
    `added` (how many rows were actually new). Those two numbers side by side
    are the honest cost of the full-export path: a routine reload parses
    millions of records to insert a few thousand.
    """
    config.ensure_dirs()
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()

    with import_lock() as got:
        if not got:
            return {"status": "busy",
                    "message": "A background import is already running; try "
                               "again in a moment.",
                    "totals": _current_totals()}

        if target:
            tp = Path(target).expanduser()
            archive = latest_archive(tp) if tp.is_dir() else tp
        else:
            archive = latest_archive()

        if archive is None or not Path(archive).exists():
            return {"status": "empty",
                    "message": "No export .zip found in the drop-folder. "
                               "Export from the iPhone Health app and put the "
                               "export.zip into ~/Documents/AppleHealthExport "
                               "first.",
                    "export_dir": str(config.EXPORT_DIR),
                    "totals": _current_totals()}

        archive = Path(archive)
        fp = _archive_fingerprint(archive)
        if not force and _already_imported(archive, fp):
            return {"status": "already_current",
                    "message": "The newest export is already imported. Pass "
                               "force=true to re-import it anyway.",
                    "archive": archive.name,
                    "totals": _current_totals()}

        stats = import_archive(archive, force=True)
        if stats is None:
            return {"status": "error",
                    "message": f"Could not import {archive.name} — it may not be "
                               "a valid Health export (bad zip or missing "
                               "export.xml).",
                    "archive": archive.name,
                    "totals": _current_totals()}
        parsed = stats["seen"]["record"]
        inserted = stats["added"]["records"]
        return {"status": "imported",
                "message": f"Imported {archive.name}: parsed {parsed:,} "
                           f"records, inserted {inserted:,} new.",
                "archive": archive.name,
                "seen": stats["seen"],
                "added": stats["added"],
                "totals": stats["totals"]}


def run(target: str | None = None, *, force: bool = False) -> int:
    """Entry point. target may be a .zip, an export dir, or None (auto)."""
    config.ensure_dirs()
    # Always make sure the schema exists so an empty DB is still queryable.
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()

    if target:
        tp = Path(target).expanduser()
        if tp.is_dir():
            archive = latest_archive(tp)
        else:
            archive = tp
    else:
        archive = latest_archive()

    if archive is None:
        print("[apple-health] No export archive found in "
              f"{config.EXPORT_DIR}. Schema is initialized and ready; the "
              "database is currently empty. Run your iPhone Health export "
              "once and it will be imported automatically.")
        return 0

    import_archive(archive, force=force)
    return 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    force_flag = "--force" in sys.argv[1:]
    sys.exit(run(args[0] if args else None, force=force_flag))
