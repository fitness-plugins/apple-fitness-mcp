"""Pairing state for LAN delta sync: the shared token and the two identities.

One pre-shared token, generated here and carried to the phone by QR. Without it
anyone on the same Wi-Fi could write into a health database the user makes
training decisions from, so every request must present it and comparison is
constant-time.

The file lives in ``data/`` (git-ignored) with mode 0600. It is written
atomically — a half-written pairing file would lock the phone out with no way
to tell why.

Two ids live here and must not be confused:

* ``service_id`` — generated on this Mac; identifies the *sync service* and is
  what the Bonjour TXT ``did`` advertises. (It was once stored as
  ``device_id``, which read as the phone's id; old files are migrated on load.)
* ``last_seen_device_id`` — the *phone's* id, taken from the ``X-Device-Id``
  header of the last batch it pushed. Empty until a batch arrives.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import socket
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import config

TOKEN_BYTES = 32          # 256 bits; base64url-unpadded -> 43 characters
FILE_MODE = 0o600

# Read-modify-write of the pairing file happens from the MCP tool thread
# (pair_device) and from receiver threads (note_device). Without this lock a
# batch landing mid-rotation could write the old token back.
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def generate_token() -> str:
    """base64url of 32 random bytes, unpadded — the wire contract's token form."""
    return base64.urlsafe_b64encode(secrets.token_bytes(TOKEN_BYTES)).decode(
        "ascii").rstrip("=")


def fingerprint(token: Optional[str]) -> Optional[str]:
    """Short, non-reversible tag for a token, safe to show in status output."""
    if not token:
        return None
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]


def hostname() -> str:
    """The .local name the phone will resolve, e.g. ``Maksims-MacBook.local.``.

    Only the first label is kept: a machine whose hostname carries some other
    domain (``mac.lan``) is still reachable over Bonjour as
    ``mac.local.``, and appending to the full name would produce something that
    resolves nowhere. Never raises — this is called while building a status
    report.
    """
    try:
        name = socket.gethostname()
    except OSError:
        return "localhost.local."
    short = name.strip().rstrip(".").split(".")[0]
    return f"{short or 'localhost'}.local."


def load() -> Optional[dict]:
    """Read the pairing file, or None when this Mac has never been paired."""
    path = config.sync_pairing_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("token"):
        return None
    if "service_id" not in data and data.get("device_id"):
        data["service_id"] = data.pop("device_id")        # pre-rename file
    return data


def _write(data: dict) -> Path:
    """Atomic 0600 write: temp file in the same directory, fsync, replace."""
    path = config.sync_pairing_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    try:
        os.chmod(path, FILE_MODE)
    except OSError:
        pass
    return path


def ensure(rotate: bool = False) -> dict:
    """Return the pairing state, creating or rotating the token as asked.

    The service id is stable across rotations: it identifies the *Mac's sync
    service*, not the credential, and the Bonjour TXT record advertises it. The
    phone's last seen id survives a rotation too — it is still the same phone.
    """
    with _lock:
        current = load()
        if current and not rotate:
            return current
        prior = current or {}
        data = {
            "v": config.SYNC_PROTOCOL_VERSION,
            "token": generate_token(),
            "service_id": prior.get("service_id") or str(uuid.uuid4()),
            "last_seen_device_id": prior.get("last_seen_device_id"),
            "created_at": prior.get("created_at") or _now(),
            "rotated_at": _now() if current else None,
        }
        _write(data)
        return data


def note_device(device_id: str) -> None:
    """Remember the phone's id from an incoming batch's ``X-Device-Id``.

    Written only when it changes, so the file holding the token is not
    rewritten on every batch. Never touches the token or the service id.
    """
    with _lock:
        current = load()
        if not current or current.get("last_seen_device_id") == device_id:
            return
        current["last_seen_device_id"] = device_id
        _write(current)


def verify(token: Optional[str]) -> bool:
    """Constant-time token check. False when unpaired or on any mismatch.

    Both operands are hashed first so the comparison length cannot leak the
    token length, and `hmac.compare_digest` keeps the timing flat. Callers must
    answer a failure with a bare 401 — never with a hint about how close it was.
    """
    current = load()
    if not current or not token:
        return False
    expected = hashlib.sha256(current["token"].encode("utf-8")).digest()
    got = hashlib.sha256(token.encode("utf-8")).digest()
    return hmac.compare_digest(expected, got)


def is_paired() -> bool:
    return load() is not None


def service_id() -> Optional[str]:
    data = load()
    return data.get("service_id") if data else None


def pairing_payload(port: int, host: Optional[str] = None,
                    pairing: Optional[dict] = None) -> dict[str, Any]:
    """The QR contents. Key order is fixed so the JSON is byte-stable."""
    data = pairing or ensure()
    return {
        "v": config.SYNC_PROTOCOL_VERSION,
        "token": data["token"],
        "service": config.SYNC_SERVICE_TYPE.rstrip("."),
        "host": host or hostname(),
        "port": int(port),
        # Wire-contract key the app already parses; the value is the service
        # id (the Mac's), not a phone id.
        "device_id": data["service_id"],
    }


def pairing_json(port: int, host: Optional[str] = None,
                 pairing: Optional[dict] = None) -> str:
    """Single-line JSON, exactly what the QR encodes and the app parses."""
    return json.dumps(pairing_payload(port, host, pairing),
                      separators=(",", ":"), ensure_ascii=False)
