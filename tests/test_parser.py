"""Tests for the parser, normalizer, and storage against synthetic fixtures.

No real personal data is used — a tiny export.xml is built inline.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apple_health_mcp import config, normalize, parser, storage
from apple_health_mcp import import_pipeline

# A small but representative export.xml: overlapping HR from Watch + Phone (for
# dedup), steps, a sleep segment, a workout with WorkoutStatistics, and an
# ActivitySummary.
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [<!ELEMENT HealthData (ExportDate,Record*,Workout*,ActivitySummary*)>]>
<HealthData locale="en_US">
 <ExportDate value="2024-06-01 09:00:00 -0700"/>
 <Me HKCharacteristicTypeIdentifierBiologicalSex="HKBiologicalSexMale"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="500" startDate="2024-05-01 08:00:00 -0700" endDate="2024-05-01 08:10:00 -0700" creationDate="2024-05-01 08:10:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" unit="count" value="300" startDate="2024-05-01 09:00:00 -0700" endDate="2024-05-01 09:10:00 -0700" creationDate="2024-05-01 09:10:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Maksim's Apple Watch" unit="count/min" value="72" startDate="2024-05-01 08:00:00 -0700" endDate="2024-05-01 08:00:00 -0700" creationDate="2024-05-01 08:00:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="iPhone" unit="count/min" value="99" startDate="2024-05-01 08:00:00 -0700" endDate="2024-05-01 08:00:00 -0700" creationDate="2024-05-01 08:00:00 -0700"/>
 <Record type="HKQuantityTypeIdentifierBodyMass" sourceName="Withings" unit="lb" value="170" startDate="2024-05-02 07:00:00 -0700" endDate="2024-05-02 07:00:00 -0700" creationDate="2024-05-02 07:00:00 -0700"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Maksim's Apple Watch" value="HKCategoryValueSleepAnalysisAsleepDeep" startDate="2024-05-01 23:30:00 -0700" endDate="2024-05-02 00:30:00 -0700" creationDate="2024-05-02 07:00:00 -0700"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" sourceName="Maksim's Apple Watch" duration="30" durationUnit="min" startDate="2024-05-03 06:00:00 -0700" endDate="2024-05-03 06:30:00 -0700">
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="145" maximum="171" unit="count/min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" sum="320" unit="Cal"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="5.2" unit="km"/>
 </Workout>
 <ActivitySummary dateComponents="2024-05-01" activeEnergyBurned="540" activeEnergyBurnedGoal="500" activeEnergyBurnedUnit="Cal" appleExerciseTime="35" appleExerciseTimeGoal="30" appleStandHours="11" appleStandHoursGoal="12"/>
</HealthData>
"""


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Point config at a throwaway DB + export dir under tmp_path."""
    db = tmp_path / "health.duckdb"
    exp = tmp_path / "AppleHealthExport"
    exp.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "EXPORT_DIR", exp)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    return tmp_path


def _write_zip(folder: Path, xml: str, name: str = "export.zip") -> Path:
    archive = folder / name
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
    return archive


# --- normalizer ---------------------------------------------------------------

def test_normalize_type_aliases():
    assert normalize.normalize_type("HKQuantityTypeIdentifierStepCount") == "step_count"
    assert normalize.normalize_type("HKQuantityTypeIdentifierHeartRate") == "heart_rate"
    assert normalize.normalize_type("HKQuantityTypeIdentifierVO2Max") == "vo2max"


def test_normalize_type_generic_fallback():
    # Not in the alias table -> prefix strip + snake_case.
    assert normalize.normalize_type(
        "HKQuantityTypeIdentifierEnvironmentalAudioExposure"
    ) == "environmental_audio_exposure"


def test_parse_ts_timezone_aware():
    ts = normalize.parse_ts("2024-05-01 08:00:00 -0700")
    assert ts is not None and ts.tzinfo is not None
    assert ts.utcoffset().total_seconds() == -7 * 3600


def test_parse_ts_bad_input():
    assert normalize.parse_ts("not a date") is None
    assert normalize.parse_ts(None) is None


def test_parse_ts_fast_path_matches_strptime():
    """The ISO rewrite must agree with strptime on every offset shape."""
    for raw, offset_hours in [
        ("2024-01-15 08:30:00 -0800", -8),
        ("2024-05-01 08:00:00 +0300", 3),
        ("2024-05-01 08:00:00 +0000", 0),
        ("2024-05-01 08:00:00 +0530", 5.5),
        ("2024-12-31 23:59:59 -1200", -12),
    ]:
        fast = normalize.parse_ts(raw)
        slow = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S %z")
        assert fast == slow
        assert fast.utcoffset() == slow.utcoffset()
        assert fast.utcoffset().total_seconds() == offset_hours * 3600


def test_parse_ts_uses_fromisoformat_for_the_apple_shape(monkeypatch):
    """Apple's own format must never reach strptime — it is 36x slower.

    parse_ts runs three times per <Record>, ~7.7M times on a real export, so
    this is the difference between ~1 s and ~40 s of every full import.
    """
    class NoStrptime(datetime):
        @classmethod
        def strptime(cls, *args, **kwargs):
            raise AssertionError("strptime used for the Apple timestamp shape")

    monkeypatch.setattr(normalize, "datetime", NoStrptime)
    ts = normalize.parse_ts("2024-01-15 08:30:00 -0800")
    assert ts == datetime(2024, 1, 15, 8, 30,
                          tzinfo=timezone(timedelta(hours=-8)))


def test_parse_ts_other_shapes_still_parse():
    """Anything that is not Apple's exact shape keeps the old slow path."""
    # Plain ISO-8601 (the documented last-resort branch).
    assert normalize.parse_ts("2024-01-15T08:30:00+00:00") == datetime(
        2024, 1, 15, 8, 30, tzinfo=timezone.utc)
    # Naive: no offset at all.
    assert normalize.parse_ts("2024-01-15 08:30:00") == datetime(2024, 1, 15, 8, 30)
    assert normalize.parse_ts("2024-01-15") == datetime(2024, 1, 15)
    # Right length and a space, but the offset is junk -> None, not a crash.
    assert normalize.parse_ts("2024-01-15 08:30:00 XXXXX") is None
    # Right shape, impossible instant -> None.
    assert normalize.parse_ts("2024-13-45 08:30:00 -0800") is None
    # Surrounding whitespace is still tolerated.
    assert normalize.parse_ts("  2024-01-15 08:30:00 -0800  ") == datetime(
        2024, 1, 15, 8, 30, tzinfo=timezone(timedelta(hours=-8)))


# --- parser -------------------------------------------------------------------

def test_iter_export_counts(tmp_path):
    xml = tmp_path / "export.xml"
    xml.write_text(SAMPLE_XML)
    kinds = [k for k, _ in parser.iter_export(xml)]
    assert kinds.count("record") == 6
    assert kinds.count("workout") == 1
    assert kinds.count("sleep") == 1          # sleep is emitted in addition to record
    assert kinds.count("activity_summary") == 1


def test_workout_statistics_extracted(tmp_path):
    xml = tmp_path / "export.xml"
    xml.write_text(SAMPLE_XML)
    workout = next(p for k, p in parser.iter_export(xml) if k == "workout")
    assert workout["type"] == "running"
    assert workout["avg_hr"] == 145
    assert workout["max_hr"] == 171
    assert workout["energy"] == 320
    assert workout["distance"] == 5.2


def test_malformed_xml_stops_gracefully(tmp_path):
    truncated = SAMPLE_XML[: SAMPLE_XML.index("<Workout")] + "<Record type="
    xml = tmp_path / "export.xml"
    xml.write_text(truncated)
    # Should not raise; yields the records parsed before the corruption.
    kinds = [k for k, _ in parser.iter_export(xml)]
    assert kinds.count("record") >= 5


# --- storage / import ---------------------------------------------------------

def test_import_and_query(sandbox):
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    stats = import_pipeline.import_archive(archive)
    assert stats is not None
    assert stats["totals"]["records"] == 6
    assert stats["totals"]["workouts"] == 1
    assert stats["totals"]["sleep"] == 1

    con = storage.connect_readonly()
    try:
        steps = con.execute(
            "SELECT sum(value) FROM records WHERE type='step_count'"
        ).fetchone()[0]
        assert steps == 800
    finally:
        con.close()


def test_import_is_idempotent(sandbox):
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    import_pipeline.import_archive(archive)
    # Re-import the exact same archive with force to bypass the fingerprint skip;
    # row hashes must prevent any duplicate rows.
    stats2 = import_pipeline.import_archive(archive, force=True)
    assert stats2["added"]["records"] == 0
    assert stats2["totals"]["records"] == 6


def test_dedup_view_prefers_watch(sandbox):
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        # Two HR rows at the same timestamp (Watch=72, iPhone=99); dedup keeps Watch.
        raw = con.execute(
            "SELECT count(*) FROM records WHERE type='heart_rate'"
        ).fetchone()[0]
        deduped = con.execute(
            "SELECT count(*) FROM records_dedup WHERE type='heart_rate'"
        ).fetchone()[0]
        kept = con.execute(
            "SELECT value FROM records_dedup WHERE type='heart_rate'"
        ).fetchone()[0]
        assert raw == 2
        assert deduped == 1
        assert kept == 72
    finally:
        con.close()


def test_reload_statuses(sandbox):
    # Empty folder -> "empty".
    assert import_pipeline.reload()["status"] == "empty"

    # Drop an export -> first reload imports it.
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    r1 = import_pipeline.reload()
    assert r1["status"] == "imported"
    assert r1["totals"]["records"] == 6

    # Same archive again -> recognized as already current (no dupes).
    r2 = import_pipeline.reload()
    assert r2["status"] == "already_current"
    assert r2["totals"]["records"] == 6

    # force=True re-imports but row hashes prevent duplicates.
    r3 = import_pipeline.reload(force=True)
    assert r3["status"] == "imported"
    assert r3["totals"]["records"] == 6


def test_reload_bad_zip_reports_error(sandbox):
    bad = config.EXPORT_DIR / "broken.zip"
    bad.write_bytes(b"not a zip file")
    r = import_pipeline.reload()
    assert r["status"] == "error"


def test_get_sleep_wake_day_and_filter_consistency(sandbox):
    from apple_health_mcp import server

    server._schema_ready = False  # re-init against this sandbox DB
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    import_pipeline.import_archive(archive)
    server._schema_ready = False

    # Sample's only sleep segment (deep, 2024-05-01 23:30 -> 2024-05-02 00:30)
    # is attributed to the WAKE day 2024-05-02, not the bedtime day.
    nights = server.get_sleep()["nights"]
    assert len(nights) == 1
    assert nights[0]["night"] == "2024-05-02"
    assert nights[0]["stages"]["deep"] == 1.0
    assert abs(nights[0]["hours_asleep"] - 1.0) < 1e-9

    # Filter and grouping use the SAME wake-day: asking for the wake day returns
    # the night; asking for the bedtime day returns nothing (the bug we fixed).
    assert len(server.get_sleep("2024-05-02", "2024-05-02")["nights"]) == 1
    assert server.get_sleep("2024-05-01", "2024-05-01")["nights"] == []


def test_get_heart_rate_raw(sandbox):
    from apple_health_mcp import server

    server._schema_ready = False  # re-init against this sandbox DB
    archive = _write_zip(config.EXPORT_DIR, SAMPLE_XML)
    import_pipeline.import_archive(archive)
    server._schema_ready = False

    # SAMPLE_XML has two HR readings at the same instant (Watch 72, iPhone 99);
    # the raw tool returns deduplicated per-sample rows sorted by time.
    r = server.get_heart_rate_raw()
    assert r["count"] == 1
    assert r["truncated"] is False
    assert r["readings"][0]["bpm"] == 72          # Watch wins the dedup
    assert "source_name" in r["readings"][0]

    # Date filter selects the reading's day; another day returns nothing.
    assert server.get_heart_rate_raw("2024-05-01", "2024-05-01")["count"] == 1
    assert server.get_heart_rate_raw("2024-05-02", "2024-05-02")["count"] == 0

    # limit caps the rows and flags truncation.
    capped = server.get_heart_rate_raw(limit=0)   # clamped to >=1
    assert capped["count"] == 1 and capped["truncated"] is False


def test_empty_database_is_queryable(sandbox):
    # No archive imported: schema must still initialize and queries return empty.
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()
    con = storage.connect_readonly()
    try:
        assert con.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM records_dedup").fetchone()[0] == 0
    finally:
        con.close()
