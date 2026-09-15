"""Wire-contract tests for the LAN delta receiver.

These run a *real* listener on loopback and talk to it with urllib, because the
things most likely to break — auth, idempotency, the durability ordering, a
port that will not bind — only exist at the socket. No DuckDB is involved: the
receiver deliberately does nothing but authenticate and spool.
"""
from __future__ import annotations

import gzip
import json
import socket
import urllib.error
import urllib.request

import pytest

from apple_health_mcp import config, sync_pairing, sync_receiver, sync_spool

BATCH_LINES = [
    {"kind": "record", "raw_type": "HKQuantityTypeIdentifierHeartRate",
     "source_name": "Maksim's Apple Watch", "unit": "count/min", "value": 172.0,
     "value_str": None, "start": "2026-08-20T13:39:37+03:00",
     "end": "2026-08-20T13:39:37+03:00", "created": "2026-08-20T13:40:02+03:00"},
    {"kind": "workout", "raw_type": "HKWorkoutActivityTypeRunning",
     "source_name": "Maksim's Apple Watch", "duration": 50.4,
     "duration_unit": "min", "distance": 8.741, "distance_unit": "km",
     "energy": 666.3, "energy_unit": "kcal", "avg_hr": 171.0, "max_hr": 197.0,
     "start": "2026-08-20T13:30:59+03:00", "end": "2026-08-20T14:21:28+03:00"},
]


# The phone's own id. Deliberately unlike the pairing's service id: confusing
# the two is the bug these tests pin down.
PHONE_ID = "5C1D3E2A-PHONE-0000-0000-000000000001"


def ndjson(lines) -> bytes:
    return "\n".join(json.dumps(line) for line in lines).encode("utf-8")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Redirect every path the receiver touches into tmp_path."""
    export = tmp_path / "AppleHealthExport"
    export.mkdir()
    monkeypatch.setattr(config, "EXPORT_DIR", export)
    monkeypatch.setattr(config, "SYNC_SPOOL_DIR", None)   # derive from EXPORT_DIR
    monkeypatch.setattr(config, "SYNC_PAIRING_PATH", tmp_path / "pairing.json")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "SYNC_DISABLED", False)
    return tmp_path


@pytest.fixture()
def live(sandbox):
    """A paired receiver on loopback, torn down with the test."""
    pairing = sync_pairing.ensure()
    receiver = sync_receiver.SyncReceiver(bind_host="127.0.0.1", port=0)
    assert receiver.start(advertise=False), receiver.listen_error
    try:
        yield receiver, pairing
    finally:
        receiver.stop()


def call(receiver, path, data=None, headers=None):
    url = f"http://127.0.0.1:{receiver.port}{config.SYNC_API_PREFIX}{path}"
    request = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def post_headers(pairing, batch_id, lines=None, **extra):
    headers = {"X-Health-Token": pairing["token"],
               "Content-Type": "application/x-ndjson",
               "Content-Encoding": "gzip",
               "X-Batch-Id": batch_id,
               "X-Device-Id": PHONE_ID}
    if lines is not None:
        headers["X-Batch-Lines"] = str(lines)
    headers.update(extra)
    return headers


# --- authentication ----------------------------------------------------------

def test_health_endpoint_answers_the_paired_device(live):
    receiver, pairing = live
    status, body = call(receiver, "/health",
                        headers={"X-Health-Token": pairing["token"]})
    assert status == 200
    # Wire key stays "device_id"; the value is the Mac's service id.
    assert body == {"ok": True, "v": 1, "device_id": pairing["service_id"],
                    "paired": True}


@pytest.mark.parametrize("headers", [{}, {"X-Health-Token": ""},
                                     {"X-Health-Token": "not-the-token"}])
def test_an_unpaired_device_is_rejected_without_a_hint(live, headers):
    receiver, _ = live
    status, body = call(receiver, "/health", headers=headers)
    assert status == 401
    # A bare error: nothing about length, prefix or how close the guess was.
    assert body == {"error": "unauthorized"}


def test_an_unauthenticated_batch_is_never_spooled(live):
    receiver, pairing = live
    body = gzip.compress(ndjson(BATCH_LINES))
    headers = post_headers(pairing, "intruder", 2)
    headers["X-Health-Token"] = "wrong"
    status, _ = call(receiver, "/batch", data=body, headers=headers)
    assert status == 401
    assert receiver.spool.count(sync_spool.PENDING) == 0
    assert receiver.spool.find("intruder") is None


# --- the batch endpoint ------------------------------------------------------

def test_a_batch_is_on_disk_before_the_200_comes_back(live):
    """Receive and import are separate steps: the acknowledgement means the
    bytes are durable, not that they are imported. The phone advances its
    HealthKit anchor on this 200 and can never resend."""
    receiver, pairing = live
    body = gzip.compress(ndjson(BATCH_LINES))
    status, answer = call(receiver, "/batch", data=body,
                          headers=post_headers(pairing, "batch-1", 2))
    assert status == 200
    assert answer == {"batch_id": "batch-1", "received_lines": 2,
                      "duplicate": False}
    spooled = receiver.spool.find("batch-1")
    assert spooled is not None and spooled.state == sync_spool.PENDING
    assert [json.loads(line) for line in spooled.iter_lines()] == BATCH_LINES
    assert spooled.meta["device_id"] == PHONE_ID
    assert spooled.meta["lines"] == 2


def test_a_repeated_batch_id_is_idempotent_and_does_not_rewrite(live):
    """A retry after an ambiguous failure must be a no-op, not a duplicate."""
    receiver, pairing = live
    body = gzip.compress(ndjson(BATCH_LINES))
    headers = post_headers(pairing, "batch-repeat", 2)
    assert call(receiver, "/batch", data=body, headers=headers)[0] == 200
    first = receiver.spool.find("batch-repeat")
    stamp = first.path.stat().st_mtime_ns

    status, answer = call(receiver, "/batch", data=body, headers=headers)
    assert status == 200
    assert answer == {"batch_id": "batch-repeat", "received_lines": 2,
                      "duplicate": True}
    assert receiver.spool.count(sync_spool.PENDING) == 1
    assert receiver.spool.find("batch-repeat").path.stat().st_mtime_ns == stamp


def test_an_already_imported_batch_id_is_still_a_duplicate(live):
    """Idempotency spans states — the phone must not resend into imported/."""
    receiver, pairing = live
    body = gzip.compress(ndjson(BATCH_LINES))
    headers = post_headers(pairing, "batch-moved", 2)
    call(receiver, "/batch", data=body, headers=headers)
    receiver.spool.mark_imported(receiver.spool.pending()[0], {"added": {}})

    status, answer = call(receiver, "/batch", data=body, headers=headers)
    assert status == 200 and answer["duplicate"] is True
    assert receiver.spool.count(sync_spool.PENDING) == 0


def test_plain_ndjson_without_gzip_is_accepted(live):
    receiver, pairing = live
    headers = post_headers(pairing, "batch-plain", 2)
    headers["Content-Encoding"] = "identity"
    status, answer = call(receiver, "/batch", data=ndjson(BATCH_LINES),
                          headers=headers)
    assert status == 200 and answer["received_lines"] == 2
    stored = receiver.spool.find("batch-plain")
    assert [json.loads(line) for line in stored.iter_lines()] == BATCH_LINES


@pytest.mark.parametrize("mutate,expected", [
    ({"X-Batch-Id": ""}, "missing X-Batch-Id header"),
    ({"X-Batch-Lines": "9"}, "X-Batch-Lines says 9 but the body decodes to 2 lines"),
    ({"X-Batch-Lines": "many"}, "X-Batch-Lines is not an integer: 'many'"),
])
def test_malformed_headers_are_400(live, mutate, expected):
    receiver, pairing = live
    headers = post_headers(pairing, "batch-bad-header", 2)
    headers.update(mutate)
    status, body = call(receiver, "/batch", data=gzip.compress(ndjson(BATCH_LINES)),
                        headers=headers)
    assert status == 400
    assert body["error"] == expected
    assert receiver.spool.count(sync_spool.PENDING) == 0


@pytest.mark.parametrize("payload,fragment", [
    (b'{"kind":"record"}\nnot json\n', "line 2 is not valid JSON"),
    (b'{"no_kind":1}\n', "missing 'kind' discriminator"),
    (b'"just a string"\n', "must be a JSON object"),
])
def test_a_malformed_body_is_400_and_spools_nothing(live, payload, fragment):
    receiver, pairing = live
    status, body = call(receiver, "/batch", data=gzip.compress(payload),
                        headers=post_headers(pairing, "batch-bad-body"))
    assert status == 400 and fragment in body["error"]
    assert receiver.spool.count(sync_spool.PENDING) == 0


def test_a_body_that_is_not_gzip_is_400(live):
    receiver, pairing = live
    status, body = call(receiver, "/batch", data=b"definitely not gzip",
                        headers=post_headers(pairing, "batch-not-gzip"))
    assert status == 400 and "not valid gzip" in body["error"]


def test_an_oversized_batch_is_413(live, monkeypatch):
    receiver, pairing = live
    monkeypatch.setattr(config, "SYNC_MAX_BODY_BYTES", 16)
    status, body = call(receiver, "/batch", data=gzip.compress(ndjson(BATCH_LINES)),
                        headers=post_headers(pairing, "batch-huge", 2))
    assert status == 413 and body["max_bytes"] == 16
    assert receiver.spool.count(sync_spool.PENDING) == 0


def test_a_gzip_bomb_is_413_not_a_memory_fire(live, monkeypatch):
    receiver, pairing = live
    monkeypatch.setattr(config, "SYNC_MAX_DECOMPRESSED_BYTES", 1024)
    bomb = gzip.compress(b'{"kind":"record"}\n' * 5000)
    status, _ = call(receiver, "/batch", data=bomb,
                     headers=post_headers(pairing, "batch-bomb"))
    assert status == 413
    assert receiver.spool.count(sync_spool.PENDING) == 0


def test_an_unknown_path_is_404(live):
    receiver, pairing = live
    status, _ = call(receiver, "/nope",
                     headers={"X-Health-Token": pairing["token"]})
    assert status == 404


# --- status, and failing safely ---------------------------------------------

def test_status_reports_what_happened(live):
    receiver, pairing = live
    call(receiver, "/health", headers={"X-Health-Token": pairing["token"]})
    call(receiver, "/health", headers={"X-Health-Token": "wrong"})
    call(receiver, "/batch", data=gzip.compress(ndjson(BATCH_LINES)),
         headers=post_headers(pairing, "batch-status", 2))

    status = receiver.status()
    assert status["running"] is True and status["port"] == receiver.port
    assert status["url"].endswith(f":{receiver.port}/v1")
    counters = status["counters"]
    assert counters["health_checks"] == 1
    assert counters["unauthorized"] == 1
    assert counters["accepted"] == 1
    assert status["last"]["batch"]["batch_id"] == "batch-status"
    assert status["last"]["unauthorized"]["at"] is not None


def test_a_port_that_will_not_bind_degrades_instead_of_raising(sandbox):
    """Startup must never take the MCP server down with it."""
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen(1)
    port = blocker.getsockname()[1]
    try:
        receiver = sync_receiver.SyncReceiver(bind_host="127.0.0.1", port=port)
        assert receiver.start(advertise=False) is False
        assert receiver.running is False
        status = receiver.status()
        assert status["running"] is False
        assert "could not bind" in status["error"]
    finally:
        blocker.close()


def test_stop_is_idempotent(live):
    receiver, _ = live
    receiver.stop()
    assert receiver.running is False
    receiver.stop()


def test_a_disabled_listener_says_so(sandbox, monkeypatch):
    monkeypatch.setattr(config, "SYNC_DISABLED", True)
    receiver = sync_receiver.SyncReceiver(bind_host="127.0.0.1", port=0)
    assert receiver.start(advertise=False) is False
    assert "HEALTH_SYNC_DISABLED" in receiver.status()["error"]


# --- identities: the Mac's service id vs the phone's device id --------------

def test_a_batch_records_the_phone_id_and_leaves_the_service_id(live):
    receiver, pairing = live
    assert pairing["last_seen_device_id"] is None
    status, _ = call(receiver, "/batch", data=gzip.compress(ndjson(BATCH_LINES)),
                     headers=post_headers(pairing, "batch-ident", 2,
                                          **{"X-Device-Id": "phone-X"}))
    assert status == 200

    stored = sync_pairing.load()
    assert stored["last_seen_device_id"] == "phone-X"
    assert stored["service_id"] == pairing["service_id"]
    assert stored["token"] == pairing["token"]
    assert "device_id" not in stored


def test_a_batch_without_a_device_header_records_nothing(live):
    receiver, pairing = live
    headers = post_headers(pairing, "batch-anon", 2)
    del headers["X-Device-Id"]
    assert call(receiver, "/batch", data=gzip.compress(ndjson(BATCH_LINES)),
                headers=headers)[0] == 200
    assert sync_pairing.load()["last_seen_device_id"] is None


def test_rotation_keeps_both_ids(sandbox):
    first = sync_pairing.ensure()
    sync_pairing.note_device("phone-X")
    rotated = sync_pairing.ensure(rotate=True)
    assert rotated["token"] != first["token"]
    assert rotated["service_id"] == first["service_id"]
    assert rotated["last_seen_device_id"] == "phone-X"


def test_a_pre_rename_pairing_file_is_read_as_service_id(sandbox):
    """Files written before the rename carry the service id as device_id."""
    config.sync_pairing_path().write_text(json.dumps(
        {"v": 1, "token": "t" * 43, "device_id": "svc-legacy",
         "created_at": "2026-08-01T00:00:00+00:00", "rotated_at": None}))
    assert sync_pairing.service_id() == "svc-legacy"
    assert sync_pairing.pairing_payload(8765)["device_id"] == "svc-legacy"

    sync_pairing.note_device("phone-X")
    stored = json.loads(config.sync_pairing_path().read_text())
    assert stored["service_id"] == "svc-legacy"
    assert stored["last_seen_device_id"] == "phone-X"
    assert "device_id" not in stored


# --- choosing the LAN address the phone is told to use ----------------------

def test_lan_address_skips_loopback_and_class_e():
    assert sync_receiver.choose_lan_address(
        ["127.0.0.1", "240.0.0.2", "192.168.1.14"]) == "192.168.1.14"


def test_lan_address_is_none_without_a_private_candidate():
    assert sync_receiver.choose_lan_address(
        ["127.0.0.1", "240.0.0.2", "169.254.10.1", "8.8.8.8"]) is None
    assert sync_receiver.choose_lan_address([]) is None


@pytest.mark.parametrize("address", [
    "127.0.0.1", "127.5.5.5",          # loopback
    "169.254.1.1",                     # link-local
    "224.0.0.251", "239.1.1.1",        # multicast
    "240.0.0.2", "255.255.255.255",    # Class E, reserved
    "0.0.0.0",
    "100.64.0.1", "8.8.8.8",           # CGNAT / public: not the Wi-Fi
    "172.32.0.1", "11.0.0.1",          # just outside the private ranges
    "fe80::1", "not an ip", "", None,
])
def test_non_private_addresses_are_rejected(address):
    assert sync_receiver.is_private_lan(address) is False


@pytest.mark.parametrize("address", [
    "10.0.0.1", "10.255.255.254", "172.16.0.1", "172.20.10.2",
    "172.31.255.254", "192.168.0.1", "192.168.255.254",
])
def test_private_addresses_are_accepted(address):
    assert sync_receiver.is_private_lan(address) is True


def test_the_default_route_interface_wins_among_several():
    candidates = ["10.0.0.5", "192.168.1.14"]
    assert sync_receiver.choose_lan_address(
        candidates, default_route="192.168.1.14") == "192.168.1.14"
    # A VPN owning the default route is not a reason to give up on the Wi-Fi.
    assert sync_receiver.choose_lan_address(
        candidates, default_route="240.0.0.2") == "10.0.0.5"


def test_the_probe_sees_past_a_vpn_default_route(monkeypatch):
    """The real-world case: a TUN at 240.0.0.2 holds the default route."""
    monkeypatch.setattr(sync_receiver, "_default_route_address",
                        lambda: "240.0.0.2")
    monkeypatch.setattr(sync_receiver, "_interface_addresses",
                        lambda: ["127.0.0.1", "172.20.10.2", "240.0.0.2"])
    assert sync_receiver.lan_probe() == (
        "172.20.10.2", ["240.0.0.2", "127.0.0.1", "172.20.10.2"])


def test_status_reports_no_address_rather_than_a_bad_one(live, monkeypatch):
    receiver, _ = live
    monkeypatch.setattr(sync_receiver, "_default_route_address",
                        lambda: "240.0.0.2")
    monkeypatch.setattr(sync_receiver, "_interface_addresses",
                        lambda: ["127.0.0.1", "240.0.0.2"])
    monkeypatch.setattr(receiver, "bind_host", "0.0.0.0")   # as in production

    status = receiver.status()
    assert status["lan_address"] is None
    assert status["url"] is None
    assert "No private LAN address" in status["lan_error"]
    assert "240.0.0.2" in status["lan_error"]
