"""End-to-end import: locate an export archive, unzip, stream-parse, load.

Called three ways:
  * by setup.sh during initial install (schema init + import if data present)
  * by the `reload_data` MCP tool when the user asks Claude to reload
  * manually:  python -m apple_health_mcp.import_pipeline [archive_or_dir]
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import sys
import tempfile
import time
import zipfile
from pathlib import Path

from . import config, parser, storage


def _find_export_xml(root: Path) -> Path | None:
    """Locate export.xml within an unzipped export (ignore export_cda.xml)."""
    candidates = [p for p in root.rglob("export.xml")]
    if candidates:
        # Prefer the shallowest match.
        return min(candidates, key=lambda p: len(p.parts))
    return None


def _archive_fingerprint(archive: Path) -> str:
    st = archive.stat()
    return hashlib.md5(
        f"{archive.name}|{st.st_size}|{int(st.st_mtime)}".encode()
    ).hexdigest()


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


def latest_archive(export_dir: Path | None = None) -> Path | None:
    """Newest .zip in the export folder, if any."""
    export_dir = export_dir or config.EXPORT_DIR
    if not export_dir.exists():
        return None
    zips = sorted(export_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime)
    return zips[-1] if zips else None


def import_archive(archive: Path, *, force: bool = False) -> dict | None:
    """Import a single export .zip. Returns stats, or None if skipped."""
    archive = Path(archive)
    fp = _archive_fingerprint(archive)
    state = _load_state()
    if not force and state.get("last_fingerprint") == fp:
        print(f"[apple-health] Already imported {archive.name}; skipping.")
        return None

    print(f"[apple-health] Importing {archive.name} ...")
    with tempfile.TemporaryDirectory(prefix="ahealth_") as tmp:
        tmpdir = Path(tmp)
        try:
            with zipfile.ZipFile(archive) as zf:
                zf.extractall(tmpdir)
        except zipfile.BadZipFile as exc:
            print(f"[apple-health] ERROR: {archive.name} is not a valid zip "
                  f"({exc}).", file=sys.stderr)
            return None

        xml = _find_export_xml(tmpdir)
        if xml is None:
            print(f"[apple-health] ERROR: no export.xml found inside "
                  f"{archive.name}.", file=sys.stderr)
            return None

        con = storage.connect()
        try:
            storage.init_schema(con)
            stats = storage.import_stream(
                con, parser.iter_export(xml), archive_name=archive.name
            )
        finally:
            con.close()

    state["last_fingerprint"] = fp
    state["last_archive"] = archive.name
    _save_state(state)
    added = stats["added"]
    print(f"[apple-health] Done. Added: records +{added['records']}, "
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
                    "message": "No export .zip found in the watched folder. "
                               "Export from the iPhone Health app and save it "
                               "into iCloud Drive / AppleHealthExport first.",
                    "export_dir": str(config.EXPORT_DIR),
                    "totals": _current_totals()}

        archive = Path(archive)
        fp = _archive_fingerprint(archive)
        already = _load_state().get("last_fingerprint") == fp
        if already and not force:
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
        return {"status": "imported",
                "message": f"Imported {archive.name}.",
                "archive": archive.name,
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
