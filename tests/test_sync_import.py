"""Importing spooled delta batches: real DuckDB, the real loader.

The load-bearing assertion here is the second one — a workout imported from
export.xml and the *same* workout arriving later as a delta must add zero rows.
That is the whole reason the wire format mirrors the parser's payloads.
"""
from __future__ import annotations

import gzip
import json

import pytest

from apple_health_mcp import (config, parser, storage, sync_import,
                              sync_spool)
from test_sync_protocol import EXPORT_XML, NDJSON_LINES


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
    return tmp_path


def spool_batch(lines, batch_id="batch-1") -> sync_spool.SpooledBatch:
    """Put a batch straight into the spool, as the receiver would have."""
    spool = sync_spool.Spool()
    body = gzip.compress("\n".join(
        line if isinstance(line, str) else json.dumps(line)
        for line in lines).encode("utf-8"))
    return spool.write(batch_id, body, {"lines": len(lines), "device_id": "test"})


def counts() -> dict:
    con = storage.connect_readonly()
    try:
        return storage.table_counts(con)
    finally:
        con.close()


def import_xml(tmp_path) -> dict:
    xml = tmp_path / "export.xml"
    xml.write_text(EXPORT_XML, encoding="utf-8")
    con = storage.connect()
    try:
        storage.init_schema(con)
        return storage.import_stream(con, parser.iter_export(xml), "export.xml")
    finally:
        con.close()


def numeric_row_identity() -> bool:
    """Has the Part 0 row-hash migration landed?

    Asked behaviourally rather than by inspecting the SQL: import one record
    twice, differing only in how the value was rendered as a string. Under the
    old hash that is two rows; under the migrated hash (numeric identity) it is
    one, which is exactly the property the delta path depends on.
    """
    payload = {"type": "heart_rate", "raw_type": "HKQuantityTypeIdentifierHeartRate",
               "source_name": "probe", "source_version": None, "device": None,
               "unit": "count/min", "value": 172.0, "value_str": "172",
               "start": None, "end": None, "created": None}
    con = storage.connect()
    try:
        storage.init_schema(con)
        before = storage.table_counts(con)["records"]
        storage.import_stream(con, [("record", payload),
                                    ("record", dict(payload, value_str="172.0"))],
                              "probe")
        added = storage.table_counts(con)["records"] - before
        con.execute("DELETE FROM records WHERE source_name = 'probe'")
        return added == 1
    finally:
        con.close()


def test_a_spooled_batch_becomes_rows_and_moves_to_imported(sandbox):
    spool_batch(NDJSON_LINES, "batch-fresh")
    result = sync_import.import_pending()

    assert result["status"] == "imported"
    assert result["batches"] == 1
    assert result["added"]["records"] == 3
    assert result["added"]["workouts"] == 1
    assert result["added"]["workout_events"] == 5
    assert result["added"]["sleep"] == 1
    spool = sync_spool.Spool()
    assert spool.count(sync_spool.PENDING) == 0
    assert spool.count(sync_spool.IMPORTED) == 1
    imported = spool.find("batch-fresh")
    assert imported.state == sync_spool.IMPORTED
    assert imported.meta["import_result"]["added"]["records"] == 3


def test_the_same_data_from_xml_then_from_the_phone_adds_nothing(sandbox):
    """Definition of done #1 and #2, for everything whose identity does not
    depend on Apple's string formatting of a number."""
    import_xml(sandbox)
    before = counts()

    spool_batch(NDJSON_LINES, "batch-same")
    result = sync_import.import_pending()
    after = counts()

    assert result["status"] == "imported"
    assert after["workouts"] == before["workouts"]
    assert after["workout_events"] == before["workout_events"]
    assert after["sleep"] == before["sleep"]
    assert result["added"]["workouts"] == 0
    assert result["added"]["workout_events"] == 0
    assert result["added"]["sleep"] == 0
    # The category record (sleep analysis) keeps value_str as its identity and
    # is sent verbatim, so it matches under the old hash too.
    if numeric_row_identity():
        assert after["records"] == before["records"], (
            "delta records duplicated their XML twins despite numeric identity")
    else:
        pytest.skip("Part 0 row-hash migration has not landed: quantity records "
                    "still hash Apple's value_str rendering, which the phone "
                    "does not send")


def test_a_delta_synced_interval_session_matches_the_xml_one(sandbox):
    """The per-repetition rows the interval analytics read must be identical."""
    def dedup_rows():
        con = storage.connect_readonly()
        try:
            cur = con.execute(
                "SELECT event_kind, step_index, activity_uuid, step_key_path, "
                "step_successful, avg_hr, distance, elevation_ascended "
                "FROM workout_events_dedup ORDER BY event_kind, step_index, "
                "activity_uuid")
            return cur.fetchall()
        finally:
            con.close()

    import_xml(sandbox)
    from_xml = dedup_rows()
    spool_batch(NDJSON_LINES, "batch-intervals")
    sync_import.import_pending()

    assert dedup_rows() == from_xml
    assert any(row[0] == "activity" for row in from_xml)


def test_importing_the_same_batch_twice_is_a_no_op(sandbox):
    spool_batch(NDJSON_LINES, "batch-once")
    first = sync_import.import_pending()
    assert first["batches"] == 1

    second = sync_import.import_pending()
    assert second["status"] == "empty"
    assert second["added"]["records"] == 0
    # Re-spooling the identical content under a new id is caught row by row.
    spool_batch(NDJSON_LINES, "batch-again")
    third = sync_import.import_pending()
    assert third["status"] == "imported"
    assert third["added"] == {"records": 0, "workouts": 0, "workout_events": 0,
                              "sleep": 0, "clinical": 0, "activity_summary": 0}


def test_a_bad_line_does_not_cost_the_rest_of_the_batch(sandbox):
    """The batch was already acknowledged; the phone cannot resend it."""
    spool_batch([json.dumps(NDJSON_LINES[0]), "{ truncated",
                 json.dumps(NDJSON_LINES[3])], "batch-partial")
    result = sync_import.import_pending()

    assert result["status"] == "imported"
    assert result["added"]["records"] == 1
    assert result["added"]["workouts"] == 1
    detail = result["details"][0]
    assert detail["skipped_lines"] == 1
    assert detail["errors"] and "line 2" in detail["errors"][0]


def test_an_unreadable_batch_is_parked_not_lost_and_does_not_block_the_queue(
        sandbox):
    spool = sync_spool.Spool()
    spool.ensure()
    broken = spool.write("batch-broken", b"this is not gzip at all", {"lines": 1})
    spool_batch(NDJSON_LINES, "batch-good")

    result = sync_import.import_pending()

    assert result["status"] == "partial"
    assert result["batches"] == 1                     # the good one still landed
    assert result["added"]["workouts"] == 1
    assert [f["batch_id"] for f in result["failed"]] == ["batch-broken"]
    assert spool.count(sync_spool.FAILED) == 1
    assert spool.find("batch-broken").state == sync_spool.FAILED
    assert broken.path.exists() is False              # moved, never deleted
    failed_file = next((spool.root / sync_spool.FAILED).glob("*.ndjson.gz"))
    assert failed_file.read_bytes() == b"this is not gzip at all"
    assert failed_file.with_name(
        failed_file.name.replace(".ndjson.gz", ".error.txt")).exists()


def test_an_empty_spool_says_what_to_do(sandbox):
    result = sync_import.import_pending()
    assert result["status"] == "empty"
    assert "Readiness app" in result["message"]
    assert result["batches"] == 0


def test_wait_seconds_polls_the_spool_and_gives_up(sandbox):
    result = sync_import.import_pending(wait_seconds=1, poll=0.05)
    assert result["status"] == "empty"
    assert result["waited_seconds"] >= 1


def test_wait_seconds_returns_as_soon_as_a_batch_lands(sandbox):
    import threading

    def deliver():
        spool_batch(NDJSON_LINES, "batch-late")

    threading.Timer(0.2, deliver).start()
    result = sync_import.import_pending(wait_seconds=20, poll=0.05)
    assert result["status"] == "imported"
    assert result["waited_seconds"] < 19


def test_an_import_is_recorded_for_sync_status(sandbox):
    spool_batch(NDJSON_LINES, "batch-audit")
    sync_import.import_pending()
    con = storage.connect_readonly()
    try:
        last = sync_import.last_delta_import(con)
    finally:
        con.close()
    assert last["batch_id"] == "batch-audit"
