"""Durable spool for delta batches pushed by the phone.

Receiving and importing are deliberately separate steps. The receiver
authenticates a batch, writes it here, **fsyncs**, and only then answers 200 —
so a crash between the acknowledgement and the import cannot lose data the
phone has already dropped from its HealthKit anchor. Nothing in this module
touches DuckDB.

Layout, under ``~/Documents/AppleHealthExport/deltas/``::

    pending/    received, not yet imported     <ts>__<batch_id>.ndjson.gz
                                               <ts>__<batch_id>.meta.json
    imported/   imported successfully (moved, with the result in the meta)
    failed/     could not be imported; kept, never discarded, with .error.txt

The name carries the receive timestamp first so a lexical sort is arrival
order, and the batch id last so idempotency is a glob rather than an index that
could disagree with the directory.
"""
from __future__ import annotations

import gzip
import io
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

from . import config

DATA_SUFFIX = ".ndjson.gz"
META_SUFFIX = ".meta.json"
ERROR_SUFFIX = ".error.txt"
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")
_MAX_ID = 80

PENDING, IMPORTED, FAILED = "pending", "imported", "failed"
STATES = (PENDING, IMPORTED, FAILED)


def safe_batch_id(batch_id: str) -> str:
    """Make a client-supplied id safe to put in a filename.

    The batch id comes off the network, so it can never be trusted to be a
    UUID: anything outside ``[A-Za-z0-9_.-]`` is replaced, and the result is
    truncated. Two ids that differ only outside that set collapse to one name,
    which is safe — the worst case is a batch reported as a duplicate.
    """
    cleaned = _UNSAFE.sub("-", (batch_id or "").strip())[:_MAX_ID]
    return cleaned or "unnamed"


def _stamp(when: datetime) -> str:
    return when.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%f")


def _fsync_dir(path: Path) -> None:
    """fsync a directory so a rename into it survives a power loss."""
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _write_durable(path: Path, data: bytes) -> None:
    """Write bytes so that after this returns they are on the platter."""
    tmp = path.parent / f".{path.name}.{os.getpid()}.part"
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    _fsync_dir(path.parent)


@dataclass
class SpooledBatch:
    """One received batch on disk."""
    path: Path
    state: str
    meta: dict = field(default_factory=dict)

    @property
    def batch_id(self) -> str:
        return self.meta.get("batch_id") or self.path.name

    @property
    def received_at(self) -> Optional[str]:
        return self.meta.get("received_at")

    @property
    def meta_path(self) -> Path:
        return self.path.with_name(self.path.name[:-len(DATA_SUFFIX)] + META_SUFFIX)

    def size_bytes(self) -> int:
        try:
            return self.path.stat().st_size
        except OSError:
            return 0

    def iter_lines(self) -> Iterator[str]:
        """Stream the NDJSON back, decompressing as it goes (flat memory)."""
        with gzip.open(self.path, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                yield line

    def summary(self) -> dict:
        return {"batch_id": self.batch_id, "state": self.state,
                "received_at": self.received_at,
                "lines": self.meta.get("lines"),
                "device_id": self.meta.get("device_id"),
                "bytes": self.size_bytes(), "file": self.path.name}


class Spool:
    """The delta spool directory. Cheap to construct; resolves paths at call
    time so tests that redirect ``config.EXPORT_DIR`` are honoured."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = Path(root) if root else config.sync_spool_dir()

    # --- layout -------------------------------------------------------------
    def dir_for(self, state: str) -> Path:
        if state not in STATES:
            raise ValueError(f"unknown spool state {state!r}")
        return self.root / state

    def ensure(self) -> None:
        for state in STATES:
            self.dir_for(state).mkdir(parents=True, exist_ok=True)

    # --- writing ------------------------------------------------------------
    def find(self, batch_id: str) -> Optional[SpooledBatch]:
        """Locate an already-spooled batch in any state (idempotency check)."""
        pattern = f"*__{safe_batch_id(batch_id)}{DATA_SUFFIX}"
        for state in STATES:
            directory = self.dir_for(state)
            if not directory.exists():
                continue
            for path in sorted(directory.glob(pattern)):
                return self._load(path, state)
        return None

    def write(self, batch_id: str, body: bytes, meta: dict) -> SpooledBatch:
        """Durably store one batch. The data file is fsynced before the meta.

        Order matters: idempotency keys on the *data* file, so a crash between
        the two leaves a batch that is still importable and still recognised as
        received, never a phantom acknowledgement with no payload.
        """
        self.ensure()
        received = datetime.now(timezone.utc)
        name = f"{_stamp(received)}__{safe_batch_id(batch_id)}"
        data_path = self.dir_for(PENDING) / (name + DATA_SUFFIX)
        _write_durable(data_path, body)
        full_meta = dict(meta)
        full_meta.setdefault("batch_id", batch_id)
        full_meta.setdefault("received_at", received.isoformat(timespec="seconds"))
        full_meta.setdefault("bytes", len(body))
        _write_durable(data_path.with_name(name + META_SUFFIX),
                       json.dumps(full_meta, indent=2, sort_keys=True).encode("utf-8"))
        return SpooledBatch(path=data_path, state=PENDING, meta=full_meta)

    # --- reading ------------------------------------------------------------
    def _load(self, path: Path, state: str) -> SpooledBatch:
        meta: dict = {}
        meta_path = path.with_name(path.name[:-len(DATA_SUFFIX)] + META_SUFFIX)
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                meta = {}
        except (OSError, ValueError):
            meta = {}
        meta.setdefault("batch_id", path.name[:-len(DATA_SUFFIX)].split("__", 1)[-1])
        return SpooledBatch(path=path, state=state, meta=meta)

    def list(self, state: str) -> list[SpooledBatch]:
        directory = self.dir_for(state)
        if not directory.exists():
            return []
        return [self._load(p, state)
                for p in sorted(directory.glob(f"*{DATA_SUFFIX}"))]

    def pending(self) -> list[SpooledBatch]:
        """Pending batches in arrival order (filename starts with the stamp)."""
        return self.list(PENDING)

    def count(self, state: str) -> int:
        directory = self.dir_for(state)
        if not directory.exists():
            return 0
        return sum(1 for _ in directory.glob(f"*{DATA_SUFFIX}"))

    def latest(self) -> Optional[SpooledBatch]:
        """The most recently received batch, whatever state it is now in."""
        newest: Optional[tuple[str, Path, str]] = None
        for state in STATES:
            directory = self.dir_for(state)
            if not directory.exists():
                continue
            for path in directory.glob(f"*{DATA_SUFFIX}"):
                key = path.name
                if newest is None or key > newest[0]:
                    newest = (key, path, state)
        return self._load(newest[1], newest[2]) if newest else None

    # --- moving between states ---------------------------------------------
    def _move(self, batch: SpooledBatch, state: str, extra_meta: Optional[dict]
              ) -> SpooledBatch:
        self.ensure()
        target_dir = self.dir_for(state)
        target = target_dir / batch.path.name
        meta = dict(batch.meta)
        if extra_meta:
            meta.update(extra_meta)
        _write_durable(target.with_name(
            target.name[:-len(DATA_SUFFIX)] + META_SUFFIX),
            json.dumps(meta, indent=2, sort_keys=True).encode("utf-8"))
        try:
            os.replace(batch.path, target)
        except OSError:
            shutil.move(str(batch.path), str(target))    # different filesystem
        _fsync_dir(target_dir)
        old_meta = batch.meta_path
        try:
            old_meta.unlink(missing_ok=True)
        except OSError:
            pass
        _fsync_dir(batch.path.parent)
        return SpooledBatch(path=target, state=state, meta=meta)

    def mark_imported(self, batch: SpooledBatch, result: Optional[dict] = None
                      ) -> SpooledBatch:
        extra = {"imported_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds")}
        if result is not None:
            extra["import_result"] = result
        return self._move(batch, IMPORTED, extra)

    def mark_failed(self, batch: SpooledBatch, error: str) -> SpooledBatch:
        """Park an unimportable batch. Kept forever — never silently dropped.

        A batch that raises would otherwise block every later batch behind it,
        so it is moved aside with the traceback next to it and the import
        continues. The data is still on disk if the cause turns out to be a bug
        on this side.
        """
        moved = self._move(batch, FAILED, {
            "failed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": error[:2000]})
        try:
            moved.path.with_name(
                moved.path.name[:-len(DATA_SUFFIX)] + ERROR_SUFFIX
            ).write_text(error, encoding="utf-8")
        except OSError:
            pass
        return moved

    # --- reporting ----------------------------------------------------------
    def stats(self) -> dict:
        latest = self.latest()
        pending = self.pending()
        oldest_pending = pending[0].summary() if pending else None
        return {
            "dir": str(self.root),
            "exists": self.root.exists(),
            "pending": len(pending),
            "imported": self.count(IMPORTED),
            "failed": self.count(FAILED),
            "pending_lines": sum(b.meta.get("lines") or 0 for b in pending),
            "oldest_pending": oldest_pending,
            "last_batch": latest.summary() if latest else None,
        }


def gunzip_limited(body: bytes, max_bytes: int) -> bytes:
    """Decompress, refusing to materialise more than `max_bytes`.

    gzip is trivially bomb-able (a few KB expands to gigabytes), and this runs
    on a socket anyone on the LAN can reach, so the stream is read in chunks
    and abandoned the moment it exceeds the cap.
    """
    out = io.BytesIO()
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
        while True:
            chunk = gz.read(1 << 20)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(
                    f"decompressed body exceeds {max_bytes} bytes")
            out.write(chunk)
    return out.getvalue()
