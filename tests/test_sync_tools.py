"""The three MCP tools that drive LAN sync: pair_device, import_from_app,
sync_status.

`sync_status()` gets the most attention, because its whole reason to exist is
answering "my data did not arrive" in one call — every failure mode it can
describe is exercised here.
"""
from __future__ import annotations

import gzip
import json
import stat

import pytest

from apple_health_mcp import (config, server, sync_pairing, sync_receiver,
                              sync_spool)
from test_sync_import import spool_batch
from test_sync_protocol import NDJSON_LINES
from test_sync_receiver import BATCH_LINES, call, ndjson, post_headers


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    export = tmp_path / "AppleHealthExport"
    export.mkdir()
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "health.duckdb")
    monkeypatch.setattr(config, "EXPORT_DIR", export)
    monkeypatch.setattr(config, "SYNC_SPOOL_DIR", None)
    monkeypatch.setattr(config, "SYNC_PAIRING_PATH", tmp_path / "pairing.json")
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(config, "SYNC_DISABLED", False)
    monkeypatch.setattr(sync_receiver, "_receiver", None)
    # Pin the LAN probe so diagnosis does not depend on the test machine's
    # interfaces (a CI runner or a VPN could otherwise add a note).
    monkeypatch.setattr(sync_receiver, "_default_route_address",
                        lambda: "192.168.1.14")
    monkeypatch.setattr(sync_receiver, "_interface_addresses",
                        lambda: ["127.0.0.1", "192.168.1.14"])
    server._schema_ready = False            # re-init schema against this DB
    return tmp_path


@pytest.fixture()
def listener(sandbox, monkeypatch):
    receiver = sync_receiver.SyncReceiver(bind_host="127.0.0.1", port=0)
    assert receiver.start(advertise=False), receiver.listen_error
    monkeypatch.setattr(sync_receiver, "_receiver", receiver)
    try:
        yield receiver
    finally:
        receiver.stop()


# --- pair_device -------------------------------------------------------------

def test_pair_device_creates_a_private_token_and_a_payload(sandbox, listener):
    result = server.pair_device()

    assert result["status"] == "paired"
    payload = json.loads(result["pairing_json"])
    assert set(payload) == {"v", "token", "service", "host", "port", "device_id"}
    assert payload["v"] == 1
    assert payload["service"] == "_healthsync._tcp"
    assert payload["host"].endswith(".local.")
    assert payload["port"] == listener.port
    assert len(payload["token"]) == 43           # 32 bytes, base64url, unpadded
    assert "=" not in payload["token"]

    path = config.sync_pairing_path()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sync_pairing.verify(payload["token"]) is True


def test_pair_device_is_safe_to_call_again(sandbox, listener):
    first = server.pair_device()
    again = server.pair_device()
    assert again["status"] == "existing"
    assert json.loads(again["pairing_json"])["token"] == \
        json.loads(first["pairing_json"])["token"]


def test_rotate_invalidates_the_phone_that_was_paired(sandbox, listener):
    old = json.loads(server.pair_device()["pairing_json"])["token"]
    rotated = server.pair_device(rotate=True)

    assert rotated["status"] == "rotated"
    assert sync_pairing.verify(old) is False
    assert sync_pairing.verify(
        json.loads(rotated["pairing_json"])["token"]) is True
    assert rotated["payload"]["device_id"] == \
        json.loads(server.pair_device()["pairing_json"])["device_id"]
    # The QR's "device_id" key carries the service id; the tool says so.
    assert rotated["service_id"] == rotated["payload"]["device_id"]


def test_pair_device_renders_a_qr(sandbox, listener):
    pytest.importorskip("segno")
    result = server.pair_device()
    assert result["qr_error"] is None
    lines = result["qr"].splitlines()
    assert len(lines) > 10
    assert len({len(line) for line in lines}) == 1        # rectangular
    assert set("".join(lines)) <= set(server._QR_BLOCKS)
    inverted = server.pair_device(invert=True)["qr"]
    assert inverted != result["qr"]


def test_pair_device_still_pairs_when_the_qr_cannot_be_drawn(sandbox, listener,
                                                             monkeypatch):
    def no_segno(_data):
        raise ImportError("No module named 'segno'")
    monkeypatch.setattr(server, "_qr_matrix", no_segno)

    result = server.pair_device()
    assert result["qr"] is None
    assert "segno" in result["qr_error"]
    assert json.loads(result["pairing_json"])["token"]     # pairing still worked


def test_pair_device_warns_when_the_listener_is_down(sandbox):
    result = server.pair_device()
    assert result["listener"]["port"] is None
    assert "no usable port" in result["warning"]


# --- import_from_app ---------------------------------------------------------

def test_import_from_app_imports_what_the_phone_delivered(sandbox, listener):
    server.pair_device()
    spool_batch(NDJSON_LINES, "tool-batch")

    result = server.import_from_app()
    assert result["status"] == "imported"
    assert result["added"]["records"] == 3
    assert result["added"]["workout_events"] == 5
    assert sync_spool.Spool().count(sync_spool.IMPORTED) == 1


def test_import_from_app_on_an_empty_spool_explains_itself(sandbox):
    result = server.import_from_app()
    assert result["status"] == "empty"
    assert result["paired"] is False
    assert result["listener_running"] is False
    assert "pair_device" in result["hint"]


# --- sync_status -------------------------------------------------------------

def test_sync_status_on_a_fresh_install_says_pair_and_start(sandbox):
    status = server.sync_status()

    assert status["pairing"]["paired"] is False
    assert status["listener"]["running"] is False
    assert any("pair_device()" in note for note in status["diagnosis"])
    assert any("NOT running" in note for note in status["diagnosis"])


def test_sync_status_reports_the_bound_port_and_pairing(sandbox, listener):
    server.pair_device()
    status = server.sync_status()

    assert status["listener"]["running"] is True
    assert status["listener"]["port"] == listener.port
    assert status["listener"]["bonjour"]["service_type"] == "_healthsync._tcp"
    assert status["pairing"]["paired"] is True
    assert status["pairing"]["token_fingerprint"]
    # The secret itself must never come back out of a read-only status call.
    assert sync_pairing.load()["token"] not in json.dumps(status)


def test_sync_status_counts_pending_batches_and_says_to_import(sandbox, listener):
    server.pair_device()
    spool_batch(NDJSON_LINES, "waiting-batch")

    status = server.sync_status()
    assert status["spool"]["pending"] == 1
    assert status["spool"]["last_batch"]["batch_id"] == "waiting-batch"
    assert any("import_from_app()" in note for note in status["diagnosis"])


def test_sync_status_diagnoses_a_phone_holding_an_old_token(sandbox, listener):
    """The failure the user will actually hit after a rotate."""
    server.pair_device()
    listener.note("request", path="/v1/batch")
    listener.note("unauthorized", remote="192.168.1.20", path="/v1/batch")

    status = server.sync_status()
    assert status["listener"]["counters"]["unauthorized"] == 1
    assert any("old token" in note for note in status["diagnosis"])
    assert status["listener"]["last"]["unauthorized"]["remote"] == "192.168.1.20"


def test_sync_status_diagnoses_a_phone_that_never_reached_the_mac(sandbox,
                                                                  listener):
    server.pair_device()
    status = server.sync_status()
    assert any("Local Network" in note for note in status["diagnosis"])


def test_sync_status_shows_the_newest_sample_per_metric(sandbox, listener):
    server.pair_device()
    spool_batch(NDJSON_LINES, "status-batch")
    server.import_from_app()
    # As if the batch had arrived over the wire with Bonjour up, so the
    # diagnosis has nothing left to complain about.
    listener.bonjour_ready = True
    listener.note("request", path="/v1/batch")
    listener.note("accepted", batch_id="status-batch", lines=3)

    status = server.sync_status()
    by_metric = {row["metric"]: row for row in status["data"]["latest_per_type"]}
    assert by_metric["heart_rate"]["last_sample"].startswith("2026-08-20")
    assert by_metric["heart_rate"]["rows_total"] == 1
    assert status["data"]["latest_workouts"].startswith("2026-08-20")
    assert status["data"]["last_delta_import"]["batch_id"] == "status-batch"
    assert status["spool"]["pending"] == 0
    assert status["spool"]["imported"] == 1
    assert any("healthy" in note for note in status["diagnosis"])


def test_sync_status_surfaces_a_failed_batch(sandbox, listener):
    server.pair_device()
    spool = sync_spool.Spool()
    spool.ensure()
    spool.write("doomed", b"not gzip", {"lines": 1})
    server.import_from_app()

    status = server.sync_status()
    assert status["spool"]["failed"] == 1
    assert any("failed to import" in note for note in status["diagnosis"])


def test_sync_status_says_when_there_is_no_usable_lan_address(sandbox, listener,
                                                             monkeypatch):
    server.pair_device()
    monkeypatch.setattr(sync_receiver, "_default_route_address",
                        lambda: "240.0.0.2")
    monkeypatch.setattr(sync_receiver, "_interface_addresses",
                        lambda: ["127.0.0.1", "240.0.0.2"])

    status = server.sync_status()
    assert status["listener"]["lan_address"] is None
    assert any("No private LAN address" in note for note in status["diagnosis"])


def test_sync_status_separates_the_service_id_from_the_phone_id(sandbox,
                                                               listener):
    server.pair_device()
    before = server.sync_status()["pairing"]
    assert before["service_id"]
    assert before["last_seen_device_id"] is None
    assert "device_id" not in before
    assert any("No batch has arrived" in note
               for note in server.sync_status()["diagnosis"])

    pairing = sync_pairing.load()
    status_code, _ = call(listener, "/batch",
                          data=gzip.compress(ndjson(BATCH_LINES)),
                          headers=post_headers(pairing, "ident-batch", 2,
                                               **{"X-Device-Id": "phone-X"}))
    assert status_code == 200

    after = server.sync_status()
    assert after["pairing"]["last_seen_device_id"] == "phone-X"
    assert after["pairing"]["service_id"] == before["service_id"]
    assert not any("No batch has arrived" in note for note in after["diagnosis"])
