"""Tests for workout structure: <WorkoutActivity> repetitions + <WorkoutEvent>.

The XML below keeps the *exact element and attribute shapes* found in the real
Apple Health export (iOS 26.6 / watchOS 26.6), trimmed to a handful of rows:

  * <WorkoutActivity> — one repetition of a workout-builder session, carrying
    its own <WorkoutStatistics> and the WOIntervalStepKeyPath /
    WOIntervalStepSuccessful metadata.
  * <WorkoutEvent>    — segment / pause / resume / marker boundaries, both at
    workout level and nested inside a <WorkoutActivity>.
  * workout-level <MetadataEntry> repeated verbatim before and after the
    events, exactly as the real export does. We must NOT emit those as rows.

The second workout is an ordinary run with statistics but no structure at all —
it must still import cleanly and contribute zero event rows.
"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from apple_health_mcp import config, parser, storage
from apple_health_mcp import import_pipeline

SOURCE = "Maksim's Apple Watch"

EXPORT_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [<!ELEMENT HealthData (ExportDate,Record*,Workout*)>]>
<HealthData locale="en_US">
 <ExportDate value="2026-08-20 15:00:00 +0300"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="50.48292259971301" durationUnit="min" sourceName="Maksim's Apple Watch" sourceVersion="26.6" creationDate="2026-08-20 14:21:35 +0300" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <MetadataEntry key="HKAverageMETs" value="11.8638 kcal/hr&#183;kg"/>
  <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
  <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:36:31 +0300" duration="5.508859552939733" durationUnit="min"/>
  <WorkoutEvent type="HKWorkoutEventTypeMarker" date="2026-08-20 13:40:00 +0300"/>
  <WorkoutEvent type="HKWorkoutEventTypeMarker" date="2026-08-20 13:40:00 +0300"/>
  <WorkoutEvent type="HKWorkoutEventTypePause" date="2026-08-20 14:21:28 +0300"/>
  <WorkoutActivity uuid="D98A8A68-6D91-4EE0-B873-0F3028FB6CA8" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" duration="8.641769770781199" durationUnit="min">
   <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" sum="1.50851" unit="km"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" average="164.01" minimum="132" maximum="172" unit="count/min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningSpeed" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" average="10.6061" minimum="5.82575" maximum="12.5308" unit="km/hr"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningPower" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" average="228.103" minimum="129" maximum="269" unit="W"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" sum="114.175" unit="kcal"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierStepCount" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" sum="1341.73" unit="count"/>
   <MetadataEntry key="WOIntervalStepKeyPath" value="0.0.0"/>
   <MetadataEntry key="HKElevationAscended" value="262 cm"/>
   <MetadataEntry key="WOIntervalStepSuccessful" value="1"/>
  </WorkoutActivity>
  <WorkoutActivity uuid="C8F129F7-4694-4619-BE8D-E05E23FD5B98" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" duration="5.021404461065928" durationUnit="min">
   <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" sum="1.00116" unit="km"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" average="175.96" minimum="171" maximum="181" unit="count/min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningSpeed" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" average="12.1633" minimum="10.7874" maximum="12.9859" unit="km/hr"/>
   <MetadataEntry key="WOIntervalStepKeyPath" value="1.0.0"/>
   <MetadataEntry key="WOIntervalStepSuccessful" value="1"/>
  </WorkoutActivity>
  <WorkoutActivity uuid="9597C5B9-4ABE-443F-8861-B5A410A4CDC0" startDate="2026-08-20 13:44:38 +0300" endDate="2026-08-20 13:47:15 +0300" duration="2.605668034156163" durationUnit="min">
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-20 13:44:38 +0300" endDate="2026-08-20 13:47:15 +0300" sum="0.369414" unit="km"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:44:38 +0300" endDate="2026-08-20 13:47:15 +0300" average="165.82" minimum="157" maximum="180" unit="count/min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierRunningSpeed" startDate="2026-08-20 13:44:38 +0300" endDate="2026-08-20 13:47:15 +0300" average="9.03534" minimum="5.03666" maximum="12.1974" unit="km/hr"/>
   <MetadataEntry key="WOIntervalStepKeyPath" value="1.0.1"/>
   <MetadataEntry key="WOIntervalStepSuccessful" value="0"/>
  </WorkoutActivity>
  <WorkoutRoute sourceName="Maksim's Apple Watch" sourceVersion="26.6" creationDate="2026-08-20 14:21:33 +0300" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:26 +0300">
   <MetadataEntry key="HKMetadataKeySyncVersion" value="2"/>
   <FileReference path="/workout-routes/route_2026-08-20_2.21pm.gpx"/>
  </WorkoutRoute>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300" average="170.5" minimum="98" maximum="197" unit="count/min"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300" sum="8.7" unit="km"/>
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <MetadataEntry key="HKAverageMETs" value="11.8638 kcal/hr&#183;kg"/>
 </Workout>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="36.59287430047989" durationUnit="min" sourceName="Maksim's Apple Watch" sourceVersion="26.6" creationDate="2026-08-18 12:21:31 +0300" startDate="2026-08-18 11:44:50 +0300" endDate="2026-08-18 12:21:25 +0300">
  <MetadataEntry key="HKIndoorWorkout" value="0"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" startDate="2026-08-18 11:44:50 +0300" endDate="2026-08-18 12:21:25 +0300" average="179.281" minimum="128" maximum="204" unit="count/min"/>
  <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" startDate="2026-08-18 11:44:50 +0300" endDate="2026-08-18 12:21:25 +0300" sum="7.00056" unit="km"/>
 </Workout>
</HealthData>
"""

# The structured session above: 5 workout-level events, 3 repetitions, 2 events
# nested inside repetitions. The second workout contributes nothing.
N_EVENTS = 5
N_ACTIVITIES = 3
N_ACTIVITY_EVENTS = 2
N_WORKOUT_EVENT_ROWS = N_EVENTS + N_ACTIVITIES + N_ACTIVITY_EVENTS

# The two nested events above are byte-identical apart from which
# <WorkoutActivity> holds them -- the real export does this too, because a
# nested <WorkoutEvent> is a copy of every workout-level segment that overlaps
# the activity. Dedup must keep both.
ALT_SOURCE = "Apple Watch - Maksim"

_INTERVAL_BLOCK = EXPORT_XML[
    EXPORT_XML.index(' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="50.48'):
    EXPORT_XML.index(' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="36.59')
]

# The SAME session a second time under a different sourceName. `workouts` gets
# two rows for one physical run, and each carries its own full set of children.
DUPLICATED_EXPORT_XML = EXPORT_XML.replace(
    _INTERVAL_BLOCK,
    _INTERVAL_BLOCK + _INTERVAL_BLOCK.replace(
        f'sourceName="{SOURCE}"', f'sourceName="{ALT_SOURCE}"'),
)


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


def _events(xml_path) -> list[dict]:
    return [p for k, p in parser.iter_export(xml_path) if k == "workout_event"]


@pytest.fixture()
def xml_file(tmp_path) -> Path:
    p = tmp_path / "export.xml"
    p.write_text(EXPORT_XML, encoding="utf-8")
    return p


# --- parser -------------------------------------------------------------------

def test_structural_children_are_emitted(xml_file):
    kinds = [k for k, _ in parser.iter_export(xml_file)]
    assert kinds.count("workout") == 2
    assert kinds.count("workout_event") == N_WORKOUT_EVENT_ROWS


def test_event_kinds_and_types(xml_file):
    evs = _events(xml_file)
    kinds = [e["event_kind"] for e in evs]
    assert kinds.count("event") == N_EVENTS
    assert kinds.count("activity") == N_ACTIVITIES
    assert kinds.count("activity_event") == N_ACTIVITY_EVENTS

    top = [e for e in evs if e["event_kind"] == "event"]
    assert [e["event_type"] for e in top] == [
        "segment", "segment", "marker", "marker", "pause"]
    assert top[0]["raw_event_type"] == "HKWorkoutEventTypeSegment"
    # A segment carries a duration in minutes; end_ts is derived from it.
    assert top[0]["duration"] == pytest.approx(5.541540004809698)
    assert top[0]["duration_unit"] == "min"
    delta = top[0]["end"] - top[0]["start"]
    assert delta.total_seconds() == pytest.approx(5.541540004809698 * 60)
    # A marker has no duration, so no derived end.
    assert top[2]["duration"] is None and top[2]["end"] is None


def test_repetitions_carry_plan_position_and_per_rep_statistics(xml_file):
    acts = [e for e in _events(xml_file) if e["event_kind"] == "activity"]
    assert [a["step_key_path"] for a in acts] == ["0.0.0", "1.0.0", "1.0.1"]
    assert [a["step_index"] for a in acts] == [0, 1, 2]
    assert [(a["step_block"], a["step_repeat"], a["step_slot"]) for a in acts] == [
        (0, 0, 0), (1, 0, 0), (1, 0, 1)]
    assert [a["step_successful"] for a in acts] == [True, True, False]

    warmup, work, recovery = acts
    assert warmup["activity_uuid"] == "D98A8A68-6D91-4EE0-B873-0F3028FB6CA8"
    assert warmup["duration"] == pytest.approx(8.641769770781199)
    assert warmup["duration_unit"] == "min"
    assert warmup["distance"] == pytest.approx(1.50851)
    assert warmup["distance_unit"] == "km"
    assert (warmup["avg_hr"], warmup["min_hr"], warmup["max_hr"]) == (164.01, 132, 172)
    assert warmup["avg_speed"] == pytest.approx(10.6061)
    assert warmup["speed_unit"] == "km/hr"
    assert warmup["avg_power"] == pytest.approx(228.103)
    assert warmup["power_unit"] == "W"
    assert warmup["energy"] == pytest.approx(114.175)
    assert warmup["energy_unit"] == "kcal"
    assert warmup["step_count"] == pytest.approx(1341.73)
    assert (warmup["elevation_ascended"], warmup["elevation_unit"]) == (262.0, "cm")

    # The 1 km work rep is faster than the recovery that follows it.
    assert work["distance"] == pytest.approx(1.00116)
    assert work["avg_speed"] == pytest.approx(12.1633)
    assert recovery["avg_speed"] == pytest.approx(9.03534)
    assert work["avg_speed"] > recovery["avg_speed"]
    # The recovery rep has no power/energy statistic at all -> NULL columns.
    assert recovery["avg_power"] is None and recovery["energy"] is None
    # Statistics with no dedicated column survive in the raw JSON blob.
    import json
    blob = json.loads(warmup["stats_json"])
    assert blob["StepCount"]["sum"] == "1341.73"
    assert blob["HeartRate"]["unit"] == "count/min"


def test_every_payload_has_the_same_key_set(xml_file):
    """The Arrow columnar insert needs rectangular batches."""
    evs = _events(xml_file)
    assert len({frozenset(e) for e in evs}) == 1


def test_parent_identity_is_carried_on_every_child(xml_file):
    workouts = {}
    for kind, p in parser.iter_export(xml_file):
        if kind == "workout":
            workouts[p["start"]] = p
    for e in _events(xml_file):
        wk = workouts[e["workout_start"]]
        assert e["workout_type"] == wk["type"] == "running"
        assert e["workout_source_name"] == wk["source_name"] == SOURCE
        assert e["workout_end"] == wk["end"]


def test_workout_metadata_and_route_are_not_emitted_as_events(xml_file):
    """The export repeats <MetadataEntry> before and after the events, and
    <WorkoutRoute> is a GPS file reference — neither is workout structure."""
    evs = _events(xml_file)
    assert all(e["event_kind"] in ("event", "activity", "activity_event")
               for e in evs)
    assert all(e["raw_event_type"] is None
               or e["raw_event_type"].startswith("HKWorkoutEventType")
               for e in evs)


def test_workout_without_structure_still_parses(xml_file):
    """The 2026-08-18 run has statistics but no events or activities."""
    plain = [p for k, p in parser.iter_export(xml_file)
             if k == "workout" and p["start"].day == 18][0]
    assert plain["avg_hr"] == pytest.approx(179.281)
    assert not [e for e in _events(xml_file)
                if e["workout_start"] == plain["start"]]


def test_iter_workouts_only_matches_iter_export(xml_file):
    """The backfill path must see exactly what the full import path sees."""
    full = [(k, p) for k, p in parser.iter_export(xml_file)
            if k in ("workout", "workout_event")]
    only = list(parser.iter_workouts_only(xml_file))
    assert full == only


def test_iter_workouts_only_accepts_a_file_object(tmp_path):
    """The backfill streams straight out of the .zip, never extracting it."""
    archive = _write_zip(tmp_path, EXPORT_XML, "stream.zip")
    with zipfile.ZipFile(archive) as zf:
        with zf.open("apple_health_export/export.xml") as fh:
            rows = list(parser.iter_workouts_only(fh))
    assert sum(1 for k, _ in rows if k == "workout") == 2
    assert sum(1 for k, _ in rows if k == "workout_event") == N_WORKOUT_EVENT_ROWS


# --- storage / import ---------------------------------------------------------

def test_events_are_imported_and_linked_to_their_workout(sandbox):
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    stats = import_pipeline.import_archive(archive)
    assert stats["totals"]["workouts"] == 2
    assert stats["totals"]["workout_events"] == N_WORKOUT_EVENT_ROWS

    con = storage.connect_readonly()
    try:
        # Every event row joins back to a real workout row: no orphans.
        orphans = con.execute(
            "SELECT count(*) FROM workout_events e "
            "LEFT JOIN workouts w ON w.row_hash = e.workout_hash "
            "WHERE w.row_hash IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        # All of them belong to the one structured session.
        assert con.execute(
            "SELECT count(DISTINCT workout_hash) FROM workout_events"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT count(DISTINCT e.workout_hash) FROM workout_events e "
            "JOIN workouts w ON w.row_hash = e.workout_hash "
            "WHERE w.type = 'running'"
        ).fetchone()[0] == 1
    finally:
        con.close()


def test_repetitions_come_back_in_order(sandbox):
    """The documented canonical query: the dedup VIEW, by workout_start_ts."""
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        rows = con.execute(
            "SELECT step_key_path, step_successful, duration, distance, avg_hr "
            "FROM workout_events_dedup "
            "WHERE event_kind = 'activity' "
            "AND workout_start_ts = '2026-08-20 13:30:59+03:00'::TIMESTAMPTZ "
            "ORDER BY step_index"
        ).fetchall()
        assert [r[0] for r in rows] == ["0.0.0", "1.0.0", "1.0.1"]
        assert [r[1] for r in rows] == [True, True, False]
        assert rows[1][3] == pytest.approx(1.00116)
        assert rows[1][4] == pytest.approx(175.96)
    finally:
        con.close()


def test_identical_sibling_events_are_all_kept(sandbox):
    """Two markers share one instant; document order keeps them distinct."""
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        assert con.execute(
            "SELECT count(*) FROM workout_events WHERE event_type = 'marker'"
        ).fetchone()[0] == 2
        assert con.execute(
            "SELECT count(DISTINCT row_hash) FROM workout_events"
        ).fetchone()[0] == N_WORKOUT_EVENT_ROWS
    finally:
        con.close()


def test_duplicate_source_copies_double_the_base_table(sandbox):
    """The base table is faithful: two workout rows -> two sets of children."""
    archive = _write_zip(config.EXPORT_DIR, DUPLICATED_EXPORT_XML)
    stats = import_pipeline.import_archive(archive)
    # One physical interval run, stored twice, plus the plain run.
    assert stats["totals"]["workouts"] == 3
    assert stats["totals"]["workout_events"] == 2 * N_WORKOUT_EVENT_ROWS

    con = storage.connect_readonly()
    try:
        assert con.execute(
            "SELECT count(*) FROM workout_events WHERE event_kind = 'activity'"
        ).fetchone()[0] == 2 * N_ACTIVITIES
        # Both copies share one start instant -- this is exactly why filtering
        # the BASE table on workout_start_ts over-counts the repetitions.
        assert con.execute(
            "SELECT count(DISTINCT workout_hash) FROM workout_events"
        ).fetchone()[0] == 2
    finally:
        con.close()


def test_dedup_view_collapses_duplicate_source_copies(sandbox):
    """The regression: one physical step must yield exactly one row.

    Before the view existed, the documented query
    `WHERE event_kind='activity' AND workout_start_ts = ?` returned
    2 * N_ACTIVITIES rows for an N_ACTIVITIES-step session.
    """
    archive = _write_zip(config.EXPORT_DIR, DUPLICATED_EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        rows = con.execute(
            "SELECT step_key_path, step_index, distance, avg_hr "
            "FROM workout_events_dedup "
            "WHERE event_kind = 'activity' "
            "AND workout_start_ts = '2026-08-20 13:30:59+03:00'::TIMESTAMPTZ "
            "ORDER BY step_index"
        ).fetchall()
        assert len(rows) == N_ACTIVITIES          # 3, not 6
        assert [r[0] for r in rows] == ["0.0.0", "1.0.0", "1.0.1"]
        assert [r[1] for r in rows] == [0, 1, 2]

        # Nothing else is over-collapsed either: the whole view equals exactly
        # one copy's worth of rows.
        assert con.execute(
            "SELECT count(*) FROM workout_events_dedup"
        ).fetchone()[0] == N_WORKOUT_EVENT_ROWS
        # ... and it is a strict subset of the base table.
        assert con.execute(
            "SELECT count(*) FROM workout_events_dedup d "
            "WHERE NOT EXISTS (SELECT 1 FROM workout_events e "
            "                  WHERE e.row_hash = d.row_hash)"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_dedup_view_keeps_nested_events_of_different_activities(sandbox):
    """Two <WorkoutActivity>s can hold the SAME nested <WorkoutEvent>.

    A nested event is a copy of every workout-level segment overlapping its
    activity, so identical (type, date, duration, step_index) tuples recur under
    different activities. `activity_uuid` is in the partition key for
    event_kind='activity_event' precisely so these stay distinct; without it
    they would collapse and the workout would lose structure.
    """
    archive = _write_zip(config.EXPORT_DIR, DUPLICATED_EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        rows = con.execute(
            "SELECT activity_uuid, step_index, start_ts, duration "
            "FROM workout_events_dedup WHERE event_kind = 'activity_event' "
            "ORDER BY activity_uuid"
        ).fetchall()
        assert len(rows) == N_ACTIVITY_EVENTS     # 2, not collapsed to 1
        # Identical in every respect except which activity owns them.
        assert rows[0][1] == rows[1][1]
        assert rows[0][2] == rows[1][2]
        assert rows[0][3] == rows[1][3]
        assert rows[0][0] != rows[1][0]
    finally:
        con.close()


def test_dedup_view_is_a_no_op_without_duplicates(sandbox):
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        assert con.execute(
            "SELECT count(*) FROM workout_events_dedup"
        ).fetchone()[0] == N_WORKOUT_EVENT_ROWS
    finally:
        con.close()


def test_deduped_rows_still_join_to_a_workout(sandbox):
    """The view keeps a real `workout_hash`, so the FK join still works."""
    archive = _write_zip(config.EXPORT_DIR, DUPLICATED_EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        assert con.execute(
            "SELECT count(*) FROM workout_events_dedup d "
            "LEFT JOIN workouts w ON w.row_hash = d.workout_hash "
            "WHERE w.row_hash IS NULL"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_workout_without_events_imports_cleanly(sandbox):
    """A workout with no structural children must not block the import."""
    plain_only = EXPORT_XML[:EXPORT_XML.index(" <Workout ")] + \
        EXPORT_XML[EXPORT_XML.index(' <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="36.59'):]
    archive = _write_zip(config.EXPORT_DIR, plain_only)
    stats = import_pipeline.import_archive(archive)
    assert stats["totals"]["workouts"] == 1
    assert stats["totals"]["workout_events"] == 0

    con = storage.connect_readonly()
    try:
        assert con.execute("SELECT count(*) FROM workout_events").fetchone()[0] == 0
    finally:
        con.close()


def test_reimport_inserts_nothing_new(sandbox):
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    import_pipeline.import_archive(archive)
    again = import_pipeline.import_archive(archive, force=True)
    assert again["added"]["workout_events"] == 0
    assert again["totals"]["workout_events"] == N_WORKOUT_EVENT_ROWS


def test_existing_tables_are_undisturbed(sandbox):
    """Adding workout_events must not perturb records / records_dedup."""
    archive = _write_zip(config.EXPORT_DIR, EXPORT_XML)
    import_pipeline.import_archive(archive)
    con = storage.connect_readonly()
    try:
        assert con.execute("SELECT count(*) FROM records").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM records_dedup").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM sleep").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM workouts").fetchone()[0] == 2
    finally:
        con.close()


def test_empty_database_has_the_table(sandbox):
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()
    con = storage.connect_readonly()
    try:
        assert con.execute("SELECT count(*) FROM workout_events").fetchone()[0] == 0
    finally:
        con.close()
