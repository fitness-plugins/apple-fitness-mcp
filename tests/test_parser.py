"""Tests for the parser, normalizer, and storage against synthetic fixtures.

No real personal data is used — a tiny export.xml is built inline.
"""
from __future__ import annotations

import zipfile
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
