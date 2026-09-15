"""HTTP receiver for phone-pushed HealthKit deltas, hosted in the MCP process.

Runs on a background thread inside the stdio MCP server. Two rules shape every
decision here:

1. **It must never take the server down.** A port that will not bind, a missing
   zeroconf, a Bonjour registration that fails — each degrades to "listener
   unavailable" reported through ``sync_status()``. Startup never raises.
2. **It must never write to stdout.** stdout is the MCP transport; a stray
   print corrupts the protocol. Everything goes to ``logs/sync.log`` (stderr if
   that cannot be opened), including the HTTP access log.

The receiver *only* authenticates and durably spools (see ``sync_spool``).
Importing is a separate step (``sync_import``), because a crash mid-import must
not lose a batch already acknowledged to the phone — HealthKit will not hand
those samples over a second time.

Wire contract v1
----------------
``GET  /v1/health`` -> ``{"ok":true,"v":1,"device_id":"...","paired":true}``
``POST /v1/batch``  headers ``Content-Type: application/x-ndjson``,
``Content-Encoding: gzip``, ``X-Health-Token``, ``X-Batch-Id``, ``X-Device-Id``,
``X-Batch-Lines``; body gzipped NDJSON; ->
``{"batch_id":"...","received_lines":N,"duplicate":false}``.
401 bad/missing token, 400 malformed, 413 too large. A ``batch_id`` already on
disk answers 200 with ``duplicate:true`` and rewrites nothing.
"""
from __future__ import annotations

import gzip
import ipaddress
import json
import logging
import socket
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Iterable, Optional

from . import config, sync_pairing, sync_protocol, sync_spool

_log = logging.getLogger("apple_health_mcp.sync")
_log_configured = False

GZIP_MAGIC = b"\x1f\x8b"
HEALTH_PATH = config.SYNC_API_PREFIX + "/health"
BATCH_PATH = config.SYNC_API_PREFIX + "/batch"


def _configure_logging() -> None:
    """File logging only — stdout belongs to the MCP stdio transport."""
    global _log_configured
    if _log_configured:
        return
    _log_configured = True
    _log.setLevel(logging.INFO)
    _log.propagate = False                     # never reach the root/stdout
    try:
        config.LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler: logging.Handler = logging.FileHandler(
            config.LOG_DIR / "sync.log", encoding="utf-8")
    except OSError:
        import sys
        handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    _log.addHandler(handler)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# The only ranges a phone on the same Wi-Fi can actually reach (RFC 1918).
# An allowlist rather than a blocklist: loopback (127/8), link-local
# (169.254/16), multicast (224/4), IANA-reserved Class E (240/4) and 0.0.0.0
# all fall outside it, and so does whatever a VPN or proxy TUN interface
# invents next. A VPN taking the default route is the realistic failure — a
# utun at 240.0.0.2 once got advertised to the phone as the Mac's address.
PRIVATE_LAN_NETWORKS = tuple(ipaddress.IPv4Network(net) for net in
                             ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"))


def is_private_lan(address: Optional[str]) -> bool:
    """True only for an IPv4 address in 10/8, 172.16/12 or 192.168/16."""
    try:
        ip = ipaddress.IPv4Address(address)
    except (ipaddress.AddressValueError, ValueError, TypeError):
        return False
    return any(ip in net for net in PRIVATE_LAN_NETWORKS)


def choose_lan_address(candidates: Iterable[Optional[str]],
                       default_route: Optional[str] = None) -> Optional[str]:
    """The address to hand the phone, or None when no candidate is usable.

    The default-route interface wins when it is itself a private address;
    otherwise the first usable candidate, in the order given. None is the
    honest answer when nothing qualifies — a wrong address sends the phone
    somewhere it can never connect, with no clue why.
    """
    if is_private_lan(default_route):
        return default_route
    return next((a for a in candidates if is_private_lan(a)), None)


def _default_route_address() -> Optional[str]:
    """Local address of the interface holding the default route.

    Connecting a UDP socket sends nothing; it just asks the routing table which
    local address would be used to reach the outside. TEST-NET-1 is used as the
    target so no real host is ever involved.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 9))
        return sock.getsockname()[0]
    except OSError:
        return None
    finally:
        sock.close()


def _interface_addresses() -> list[str]:
    """Every IPv4 address on every interface.

    Needed because the default route can belong to a VPN tunnel while the Wi-Fi
    the phone shares is on another interface. ``ifaddr`` ships with zeroconf;
    without it, fall back to whatever the hostname resolves to.
    """
    try:
        import ifaddr
        return [ip.ip for adapter in ifaddr.get_adapters()
                for ip in adapter.ips if isinstance(ip.ip, str)]
    except Exception:
        pass
    try:
        return socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return []


def lan_probe() -> tuple[Optional[str], list[str]]:
    """(chosen address or None, every IPv4 address seen — default route first)."""
    default = _default_route_address()
    seen = list(dict.fromkeys(([default] if default else [])
                              + _interface_addresses()))
    return choose_lan_address(seen, default_route=default), seen


def lan_address() -> Optional[str]:
    """The private LAN address the phone should connect to, or None."""
    return lan_probe()[0]


def no_lan_address_note(seen: Iterable[str]) -> str:
    return ("No private LAN address (10/8, 172.16/12, 192.168/16) on any "
            f"interface — saw {', '.join(seen) or 'none'}. The phone has "
            "nothing it can connect to: join the same Wi-Fi as the phone, or "
            "if a VPN/proxy owns the default route, check the Wi-Fi interface "
            "still has its address.")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "AppleHealthSync/1"
    sys_version = ""
    timeout = 60

    # --- plumbing -----------------------------------------------------------
    @property
    def receiver(self) -> "SyncReceiver":
        return self.server.receiver          # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:   # noqa: A003
        _log.info("%s %s", self.address_string(), fmt % args)

    def _respond(self, code: int, payload: dict, close: bool = False) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if close:
                self.close_connection = True
                self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
        except OSError as exc:                # client hung up mid-response
            _log.warning("could not answer %s: %s", self.path, exc)

    def _unauthorized(self) -> None:
        """Bare 401. Never says whether the token was absent, short or close."""
        self.receiver.note("unauthorized", remote=self.client_address[0],
                           path=self.path)
        self._respond(401, {"error": "unauthorized"}, close=True)

    def _authenticate(self) -> bool:
        return sync_pairing.verify(self.headers.get(config.SYNC_AUTH_HEADER))

    def _read_body(self, length: int) -> bytes:
        chunks = []
        remaining = length
        while remaining > 0:
            chunk = self.rfile.read(min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # --- routes -------------------------------------------------------------
    def do_GET(self) -> None:                                    # noqa: N802
        self.receiver.note("request", path=self.path)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != HEALTH_PATH:
            self._respond(404, {"error": "not found"}, close=True)
            return
        if not self._authenticate():
            self._unauthorized()
            return
        self.receiver.note("health")
        # "device_id" is the wire-contract key the app parses; its value is
        # this Mac's service id (the Bonjour TXT "did"), not the phone's id.
        self._respond(200, {"ok": True, "v": config.SYNC_PROTOCOL_VERSION,
                            "device_id": sync_pairing.service_id(),
                            "paired": True})

    def do_POST(self) -> None:                                   # noqa: N802
        self.receiver.note("request", path=self.path)
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path != BATCH_PATH:
            self._respond(404, {"error": "not found"}, close=True)
            return
        if not self._authenticate():
            self._unauthorized()
            return
        try:
            self._handle_batch()
        except Exception as exc:                       # never kill the thread
            _log.exception("batch handling failed")
            self.receiver.note("error", detail=repr(exc))
            self._respond(500, {"error": "internal error"}, close=True)

    def _bad(self, message: str) -> None:
        self.receiver.note("malformed", detail=message)
        self._respond(400, {"error": message}, close=True)

    def _handle_batch(self) -> None:
        receiver = self.receiver
        if (self.headers.get("Transfer-Encoding") or "").lower() == "chunked":
            # BaseHTTPRequestHandler cannot de-chunk, and a half-read body would
            # poison the connection. Say so explicitly rather than hanging.
            self._bad("chunked transfer-encoding is not supported; "
                      "send Content-Length")
            return
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError:
            self._bad("missing or invalid Content-Length")
            return
        if length < 0:
            self._bad("missing or invalid Content-Length")
            return
        if length > config.SYNC_MAX_BODY_BYTES:
            receiver.note("too_large", detail=f"{length} bytes")
            self._respond(413, {"error": "batch too large",
                                "max_bytes": config.SYNC_MAX_BODY_BYTES},
                          close=True)
            return

        batch_id = (self.headers.get("X-Batch-Id") or "").strip()
        if not batch_id:
            self._bad("missing X-Batch-Id header")
            return
        device_id = (self.headers.get("X-Device-Id") or "").strip() or None
        if device_id:
            try:
                sync_pairing.note_device(device_id)
            except OSError as exc:            # bookkeeping; never cost a batch
                _log.warning("could not record device id %s: %s", device_id, exc)

        # Idempotency before the body is even decompressed: a retry after an
        # ambiguous failure must be cheap and must not rewrite the spool file.
        existing = receiver.spool.find(batch_id)
        if existing is not None:
            self._read_body(length)           # drain so keep-alive survives
            receiver.note("duplicate", batch_id=batch_id)
            self._respond(200, {"batch_id": batch_id,
                                "received_lines": existing.meta.get("lines") or 0,
                                "duplicate": True})
            return

        body = self._read_body(length)
        if len(body) != length:
            self._bad(f"truncated body: got {len(body)} of {length} bytes")
            return

        encoding = (self.headers.get("Content-Encoding") or "").lower()
        gzipped = "gzip" in encoding or body[:2] == GZIP_MAGIC
        try:
            raw = (sync_spool.gunzip_limited(body,
                                             config.SYNC_MAX_DECOMPRESSED_BYTES)
                   if gzipped else body)
        except ValueError as exc:
            receiver.note("too_large", detail=str(exc))
            self._respond(413, {"error": str(exc)}, close=True)
            return
        except OSError as exc:                       # gzip.BadGzipFile is OSError
            self._bad(f"body is not valid gzip: {exc}")
            return
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            self._bad(f"body is not valid UTF-8: {exc}")
            return

        lines = 0
        for lineno, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except ValueError as exc:
                self._bad(f"line {lineno} is not valid JSON: {exc}")
                return
            problem = sync_protocol.check_object(obj)
            if problem:
                self._bad(f"line {lineno}: {problem}")
                return
            lines += 1

        header_lines = self.headers.get("X-Batch-Lines")
        declared: Optional[int] = None
        if header_lines is not None:
            try:
                declared = int(header_lines)
            except ValueError:
                self._bad(f"X-Batch-Lines is not an integer: {header_lines!r}")
                return
            if declared != lines:
                # A mismatch means the body was truncated in flight. Refusing it
                # is what keeps the phone's anchor from advancing over samples
                # that never landed.
                self._bad(f"X-Batch-Lines says {declared} but the body decodes "
                          f"to {lines} lines")
                return

        # Spool exactly what arrived when it arrived gzipped; compress it
        # ourselves only for a client that sent plain NDJSON. If this raises
        # (disk full, permissions) the request MUST NOT become a 200: do_POST
        # turns it into a 500, the phone leaves its anchor where it is, and the
        # samples come again on the next sync.
        stored = receiver.spool.write(
            batch_id,
            body if gzipped else gzip.compress(raw),
            {"batch_id": batch_id, "device_id": device_id, "lines": lines,
             "declared_lines": declared, "remote_addr": self.client_address[0],
             "content_encoding": encoding or None,
             "bytes_received": length, "bytes_decoded": len(raw)},
        )
        receiver.note("accepted", batch_id=batch_id, lines=lines,
                      summary=stored.summary())
        _log.info("spooled batch %s (%d lines) from %s", batch_id, lines,
                  self.client_address[0])
        self._respond(200, {"batch_id": batch_id, "received_lines": lines,
                            "duplicate": False})


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    receiver: "SyncReceiver"


class SyncReceiver:
    """Owns the listening socket, the Bonjour registration, and the counters."""

    def __init__(self, bind_host: Optional[str] = None,
                 port: Optional[int] = None,
                 spool: Optional[sync_spool.Spool] = None) -> None:
        self.bind_host = bind_host if bind_host is not None else config.SYNC_BIND_HOST
        self.requested_port = config.SYNC_PORT if port is None else port
        self.spool = spool or sync_spool.Spool()
        self.httpd: Optional[_Server] = None
        self.thread: Optional[threading.Thread] = None
        self.port: Optional[int] = None
        self.started_at: Optional[str] = None
        self.listen_error: Optional[str] = None
        self.bonjour_error: Optional[str] = None
        self.bonjour_name: Optional[str] = None
        self.bonjour_ready = False
        self._zeroconf = None
        self._service_info = None
        self._lock = threading.Lock()
        self.counters = {"requests": 0, "health_checks": 0, "accepted": 0,
                         "duplicates": 0, "unauthorized": 0, "malformed": 0,
                         "too_large": 0, "errors": 0}
        self.last: dict[str, Any] = {
            "request_at": None, "health_check_at": None, "batch": None,
            "unauthorized": None, "malformed": None, "error": None}

    # --- counters -----------------------------------------------------------
    def note(self, event: str, **detail: Any) -> None:
        """Record one event. Called from handler threads, so it takes the lock."""
        with self._lock:
            if event == "request":
                self.counters["requests"] += 1
                self.last["request_at"] = _now()
            elif event == "health":
                self.counters["health_checks"] += 1
                self.last["health_check_at"] = _now()
            elif event == "accepted":
                self.counters["accepted"] += 1
                self.last["batch"] = {"at": _now(), "duplicate": False, **detail}
            elif event == "duplicate":
                self.counters["duplicates"] += 1
                self.last["batch"] = {"at": _now(), "duplicate": True, **detail}
            elif event == "unauthorized":
                self.counters["unauthorized"] += 1
                self.last["unauthorized"] = {"at": _now(), **detail}
            elif event == "malformed":
                self.counters["malformed"] += 1
                self.last["malformed"] = {"at": _now(), **detail}
            elif event == "too_large":
                self.counters["too_large"] += 1
                self.last["malformed"] = {"at": _now(), "too_large": True, **detail}
            elif event == "error":
                self.counters["errors"] += 1
                self.last["error"] = {"at": _now(), **detail}

    # --- lifecycle ----------------------------------------------------------
    def start(self, advertise: bool = True) -> bool:
        """Bind and serve. Returns success; never raises, never blocks on Bonjour."""
        _configure_logging()
        if self.httpd is not None:
            return True
        if config.SYNC_DISABLED:
            self.listen_error = ("disabled by HEALTH_SYNC_DISABLED; unset it and "
                                 "restart Claude Desktop to receive from the phone")
            return False
        try:
            self.spool.ensure()
        except OSError as exc:
            _log.warning("spool directory unavailable: %s", exc)
        try:
            httpd = _Server((self.bind_host, self.requested_port), _Handler)
        except OSError as exc:
            self.listen_error = f"could not bind {self.bind_host}:{self.requested_port} ({exc})"
            _log.error(self.listen_error)
            return False
        httpd.receiver = self
        self.httpd = httpd
        self.port = httpd.server_address[1]
        self.started_at = _now()
        self.thread = threading.Thread(target=self._serve, name="health-sync-http",
                                       daemon=True)
        self.thread.start()
        _log.info("listening on %s:%s", self.bind_host, self.port)
        if advertise:
            # Bonjour probing takes a second or two and can fail on its own; it
            # must never delay or break the MCP server's startup.
            threading.Thread(target=self._advertise, name="health-sync-bonjour",
                             daemon=True).start()
        return True

    def _serve(self) -> None:
        try:
            self.httpd.serve_forever(poll_interval=0.5)   # type: ignore[union-attr]
        except Exception as exc:                          # pragma: no cover
            self.listen_error = f"listener stopped: {exc!r}"
            _log.exception("listener stopped")

    def _advertise(self) -> None:
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except Exception as exc:
            self.bonjour_error = (
                f"zeroconf is not installed ({exc}); run `uv sync`. The phone "
                "can still be paired by host and port from pair_device().")
            _log.warning(self.bonjour_error)
            return
        try:
            pairing = sync_pairing.ensure()
            address, seen = lan_probe()
            if address is None:
                # Advertising a loopback or reserved address would lead the
                # phone to a host it can never reach; better to say so.
                self.bonjour_error = no_lan_address_note(seen)
                _log.warning(self.bonjour_error)
                return
            short = socket.gethostname().split(".")[0] or "mac"
            name = f"Apple Health Sync on {short}.{config.SYNC_SERVICE_TYPE}{config.SYNC_SERVICE_DOMAIN}"
            info = ServiceInfo(
                f"{config.SYNC_SERVICE_TYPE}{config.SYNC_SERVICE_DOMAIN}",
                name,
                addresses=[socket.inet_aton(address)],
                port=int(self.port or 0),
                properties={"v": str(config.SYNC_PROTOCOL_VERSION),
                            "did": pairing["service_id"],
                            "path": config.SYNC_API_PREFIX},
                server=sync_pairing.hostname(),
            )
            zc = Zeroconf()
            zc.register_service(info, allow_name_change=True)
            self._zeroconf, self._service_info = zc, info
            self.bonjour_ready = True
            self.bonjour_name = info.name
            self.bonjour_error = None
            _log.info("advertising %s on %s:%s", info.name, address, self.port)
        except Exception as exc:
            self.bonjour_error = f"Bonjour registration failed: {exc!r}"
            _log.warning(self.bonjour_error)

    def stop(self) -> None:
        """Unregister, close the socket, join the thread. Safe to call twice."""
        if self._zeroconf is not None:
            try:
                if self._service_info is not None:
                    self._zeroconf.unregister_service(self._service_info)
                self._zeroconf.close()
            except Exception as exc:                      # pragma: no cover
                _log.warning("zeroconf shutdown: %r", exc)
            finally:
                self._zeroconf = self._service_info = None
                self.bonjour_ready = False
        httpd, self.httpd = self.httpd, None
        if httpd is not None:
            try:
                httpd.shutdown()
            except Exception as exc:                      # pragma: no cover
                _log.warning("listener shutdown: %r", exc)
            httpd.server_close()
        if self.thread is not None:
            self.thread.join(timeout=5)
            self.thread = None
        self.port = None
        _log.info("listener stopped")

    # --- reporting ----------------------------------------------------------
    @property
    def running(self) -> bool:
        return self.httpd is not None and bool(self.thread and self.thread.is_alive())

    def base_url(self, lan: Optional[str] = None) -> Optional[str]:
        """URL the phone should use; None when there is no reachable address.

        ``lan`` lets a caller that already probed the interfaces pass the
        result in, so the URL and the reported address cannot disagree.
        """
        if not self.running or self.port is None:
            return None
        if self.bind_host in ("", "0.0.0.0"):
            host = lan if lan is not None else lan_address()
            if host is None:
                return None
        else:
            host = self.bind_host
        return f"http://{host}:{self.port}{config.SYNC_API_PREFIX}"

    def status(self) -> dict:
        with self._lock:
            counters = dict(self.counters)
            last = {k: v for k, v in self.last.items()}
        lan, seen = lan_probe() if self.running else (None, [])
        return {
            "running": self.running,
            "bind_host": self.bind_host,
            "port": self.port,
            "url": self.base_url(lan),
            "lan_address": lan,
            "lan_error": (no_lan_address_note(seen)
                          if self.running and lan is None else None),
            "started_at": self.started_at,
            "error": self.listen_error,
            "bonjour": {
                "advertising": self.bonjour_ready,
                "service_type": config.SYNC_SERVICE_TYPE.rstrip("."),
                "domain": config.SYNC_SERVICE_DOMAIN,
                "name": self.bonjour_name,
                "txt": {"v": str(config.SYNC_PROTOCOL_VERSION),
                        "did": sync_pairing.service_id(),
                        "path": config.SYNC_API_PREFIX},
                "error": self.bonjour_error,
            },
            "counters": counters,
            "last": last,
        }


# --- process-wide singleton (server.py owns its lifecycle) --------------------
_receiver: Optional[SyncReceiver] = None
_start_error: Optional[str] = None


def get_receiver() -> Optional[SyncReceiver]:
    return _receiver


def start_receiver(**kwargs) -> Optional[SyncReceiver]:
    """Start the process-wide receiver. Never raises: a failure is reported
    through ``sync_status()``, it does not stop the MCP server from serving."""
    global _receiver, _start_error
    if _receiver is not None and _receiver.running:
        return _receiver
    try:
        receiver = SyncReceiver(**kwargs)
        receiver.start()
        _receiver = receiver
        _start_error = receiver.listen_error
    except Exception as exc:                              # pragma: no cover
        _configure_logging()
        _log.exception("receiver failed to start")
        _start_error = repr(exc)
    return _receiver


def stop_receiver() -> None:
    global _receiver
    receiver, _receiver = _receiver, None
    if receiver is not None:
        try:
            receiver.stop()
        except Exception:                                 # pragma: no cover
            _log.exception("receiver failed to stop")


def start_error() -> Optional[str]:
    return _start_error
