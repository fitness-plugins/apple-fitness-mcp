"""Row identity: numeric, timezone-stable, and the same from both ingest paths.

`storage.py` used to hash a record as

    md5(type | source_name | cast(start_ts AS VARCHAR) | cast(end_ts AS VARCHAR)
        | unit | value_str)

which pinned identity to two things neither ingestion path can be trusted to
reproduce:

  * `value_str` — Apple's own XML text for the number. The Readiness app is
    handed an `HKQuantity`; "72" vs "72.0" is a different row.
  * `cast(<TIMESTAMPTZ> AS VARCHAR)` — rendered in the DuckDB *session*
    timezone, so the same instant hashes differently depending on where the
    import ran.

These tests pin the replacement: a value normalized to
`storage.VALUE_SIG_DIGITS` significant digits (with `value_str` retained only
for the category types, which have no number), and timestamps rendered as
canonical UTC seconds. The headline case is
`test_delta_over_the_same_window_adds_nothing` — a full export followed by a
delta covering the same window must insert nothing at all. That test is the
reason the migration exists.

Synthetic fixtures only; no real data.
"""
from __future__ import annotations

import importlib.util
import json
import re
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from apple_health_mcp import config, parser, storage

ROOT = Path(__file__).resolve().parents[1]

# The export's real source name: a NON-BREAKING SPACE (U+00A0) inside it, as in
# the live data. `source_name` is part of the identity, so nothing anywhere may
# fold, trim or normalize it.
SOURCE = "Apple\u00a0Watch \u2014 Maksim"   # U+00A0, exactly as exported
PHONE = "iPhone"

# One structured interval workout, the category types (value IS NULL, value_str
# carries the category name), a sub-unit percentage, a float32-widened
# temperature, and two heart-rate samples that differ only by their timestamp.
FULL_XML = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE HealthData [<!ELEMENT HealthData (ExportDate,Record*,Workout*)>]>
<HealthData locale="en_US">
 <ExportDate value="2026-08-20 15:00:00 +0300"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" unit="count/min" value="72" startDate="2026-08-18 08:00:00 +0300" endDate="2026-08-18 08:00:00 +0300" creationDate="2026-08-18 08:00:05 +0300"/>
 <Record type="HKQuantityTypeIdentifierHeartRate" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" unit="count/min" value="72" startDate="2026-08-18 08:00:07 +0300" endDate="2026-08-18 08:00:07 +0300" creationDate="2026-08-18 08:00:09 +0300"/>
 <Record type="HKQuantityTypeIdentifierWalkingAsymmetryPercentage" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" unit="%" value="0.000241262" startDate="2026-08-18 09:00:00 +0300" endDate="2026-08-18 09:10:00 +0300" creationDate="2026-08-18 09:10:01 +0300"/>
 <Record type="HKQuantityTypeIdentifierAppleSleepingWristTemperature" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" unit="degC" value="35.7799987792969" startDate="2026-08-18 03:00:00 +0300" endDate="2026-08-18 03:00:00 +0300" creationDate="2026-08-18 07:00:00 +0300"/>
 <Record type="HKQuantityTypeIdentifierStepCount" sourceName="iPhone" sourceVersion="26.6" unit="count" value="500" startDate="2026-08-18 10:00:00 +0300" endDate="2026-08-18 10:10:00 +0300" creationDate="2026-08-18 10:10:00 +0300"/>
 <Record type="HKCategoryTypeIdentifierSleepAnalysis" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" value="HKCategoryValueSleepAnalysisAsleepCore" startDate="2026-08-18 01:00:00 +0300" endDate="2026-08-18 02:00:00 +0300" creationDate="2026-08-18 07:00:00 +0300"/>
 <Record type="HKCategoryTypeIdentifierAppleStandHour" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" value="HKCategoryValueAppleStandHourStood" startDate="2026-08-18 11:00:00 +0300" endDate="2026-08-18 12:00:00 +0300" creationDate="2026-08-18 12:00:00 +0300"/>
 <Workout workoutActivityType="HKWorkoutActivityTypeRunning" duration="50.48292259971301" durationUnit="min" sourceName="Apple&#160;Watch &#8212; Maksim" sourceVersion="26.6" creationDate="2026-08-20 14:21:35 +0300" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 14:21:28 +0300">
  <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
  <WorkoutActivity uuid="D98A8A68-6D91-4EE0-B873-0F3028FB6CA8" startDate="2026-08-20 13:30:59 +0300" endDate="2026-08-20 13:39:37 +0300" duration="8.641769770781199" durationUnit="min">
   <WorkoutEvent type="HKWorkoutEventTypeSegment" date="2026-08-20 13:30:59 +0300" duration="5.541540004809698" durationUnit="min"/>
   <WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" sum="1.50851" unit="km"/>
   <MetadataEntry key="WOIntervalStepKeyPath" value="0.0.0"/>
  </WorkoutActivity>
  <WorkoutActivity uuid="C8F129F7-4694-4619-BE8D-E05E23FD5B98" startDate="2026-08-20 13:39:37 +0300" endDate="2026-08-20 13:44:38 +0300" duration="5.021404461065928" durationUnit="min">
   <MetadataEntry key="WOIntervalStepKeyPath" value="1.0.0"/>
  </WorkoutActivity>
  <WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" average="170.5" maximum="197" unit="count/min"/>
 </Workout>
</HealthData>
"""

N_RECORDS = 7
N_SLEEP = 1
N_WORKOUTS = 1
# 1 workout-level event + 2 repetitions + 1 event nested in a repetition.
N_WORKOUT_EVENTS = 4


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


@pytest.fixture(scope="module")
def migrate_mod():
    spec = importlib.util.spec_from_file_location(
        "migrate_row_hash", ROOT / "scripts" / "migrate_row_hash.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


# --- helpers ------------------------------------------------------------------

def _export_events(tmp_path: Path, xml: str = FULL_XML) -> list:
    """Everything the XML full-export path yields, materialized."""
    p = tmp_path / "export.xml"
    p.write_text(xml, encoding="utf-8")
    return list(parser.iter_export(p))


def _import(events, *, session_tz: str | None = None) -> dict:
    con = storage.connect()
    try:
        storage.init_schema(con)
        if session_tz is not None:
            try:
                con.execute(f"SET TimeZone='{session_tz}'")
            except Exception as exc:                     # pragma: no cover
                pytest.skip(f"no session timezone support here: {exc}")
        return storage.import_stream(con, events)
    finally:
        con.close()


def _query(sql: str, params=None):
    con = storage.connect_readonly()
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def _as_delta(events: list) -> list:
    """What the phone would send for exactly the same samples.

    Models every way the HealthKit path legitimately differs from the XML path
    while describing the same sample:

      * the value arrives as a number, not as Apple's text, and carries the
        last-bit noise a unit conversion leaves behind — so `value_str` is
        whatever the adapter happens to render, here deliberately unlike the
        XML's;
      * `HKSample.startDate` is an NSDate with sub-second precision, and the
        offset it is expressed in is the phone's, not the one frozen into the
        export;
      * `sourceVersion` / `device` are not identity and may be anything.

    Category samples (`value is None`) keep `value_str` verbatim: it is the
    category name, and it *is* their identity.
    """
    out = []
    for kind, payload in events:
        p = dict(payload)
        for key in ("start", "end", "created", "workout_start", "workout_end"):
            if isinstance(p.get(key), datetime):
                # Same instant, expressed in a different offset, carrying a
                # different sub-second component. Replacing rather than adding
                # microseconds keeps the delta inside the same whole second no
                # matter what fractional part the fixture happens to produce.
                p[key] = p[key].astimezone(timezone.utc).replace(
                    microsecond=437)
        if kind == "record":
            if p.get("value") is not None:
                p["value"] = p["value"] * (1.0 + 1e-13)
                p["value_str"] = f"{p['value']:.12f}"
            p["source_version"] = "27.0"
            p["device"] = None
        if kind == "workout_event" and p.get("duration") is not None:
            p["duration"] = p["duration"] * (1.0 + 1e-13)
        out.append((kind, p))
    return out


def _float32(x: float) -> float:
    """The double you get when a float32 sensor value is widened."""
    return struct.unpack("f", struct.pack("f", x))[0]


def _record(**kw) -> tuple:
    """A `record` payload with sane defaults; override what the test is about."""
    base = datetime(2026, 8, 18, 8, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    p = {
        "type": "heart_rate", "raw_type": "HKQuantityTypeIdentifierHeartRate",
        "source_name": SOURCE, "source_version": "26.6", "device": None,
        "unit": "count/min", "value": 72.0, "value_str": "72",
        "start": base, "end": base, "created": base,
    }
    p.update(kw)
    return ("record", p)


# --- the expressions themselves ----------------------------------------------

# The pre-type-pin rendering of a TIMESTAMPTZ, i.e. `utc_ts_expr` as it stood
# before the CAST was added. Used to prove the CAST changed no existing hash.
_UNPINNED_TS = re.compile(
    r"strftime\(\(CAST\((\w+) AS TIMESTAMP WITH TIME ZONE\) "
    r"AT TIME ZONE 'UTC'\), '([^']*)'\)")


def _unpin(expr: str) -> tuple[str, int]:
    """Rewrite a hash expression back to its pre-CAST form."""
    return _UNPINNED_TS.subn(r"strftime((\1 AT TIME ZONE 'UTC'), '\2')", expr)


def test_the_type_pin_changed_no_existing_hash(sandbox, tmp_path):
    """The CAST in `utc_ts_expr` must be an identity on real, typed rows.

    `utc_ts_expr` gained an explicit CAST so that an all-NULL timestamp column
    (which Arrow types as `null`, and for which `NULL AT TIME ZONE 'UTC'` binds
    to TIME WITH TIME ZONE) stops crashing the insert. That expression is row
    identity for the whole database: if the CAST altered so much as one hash,
    every stored row_hash would silently become wrong and the migration's
    meaning would change with it.

    So compute both renderings side by side over populated tables and require
    byte equality — asserted on values, not by reading the SQL.
    """
    _import(_export_events(tmp_path))

    con = storage.connect_readonly()
    try:
        checked = 0
        for table, expr in storage.HASH_EXPRESSIONS.items():
            derived = storage.DERIVED_HASH_COLUMNS.get(table, {})
            for name, e in [("row_hash", expr)] + sorted(derived.items()):
                old, n = _unpin(e)
                if n == 0:
                    continue           # carries no timestamp (clinical)
                assert old != e, f"{table}.{name}: rewrite was a no-op"
                rows = con.execute(
                    f'SELECT {e}, {old} FROM "{table}"').fetchall()
                assert rows, f"{table} is empty — this proves nothing"
                for new_h, old_h in rows:
                    assert new_h == old_h, (
                        f"{table}.{name}: the CAST changed a hash "
                        f"({new_h} != {old_h})")
                checked += len(rows)
        # Guard against the whole loop silently skipping.
        assert checked >= N_RECORDS + N_SLEEP + N_WORKOUTS
    finally:
        con.close()


def test_a_batch_whose_timestamps_are_all_null_still_imports(sandbox):
    """An all-NULL timestamp column must hash, not raise.

    pyarrow types a column that is NULL for the entire batch as `null`, so
    DuckDB sees an untyped NULL. Before `utc_ts_expr` pinned the type this
    raised BinderException ('strftime(TIME WITH TIME ZONE, ...)') and took the
    whole insert with it. Reachable for real: a small delta batch in which no
    row carries a timestamp.
    """
    r = _import([
        _record(start=None, end=None, created=None, value=172.0,
                value_str="172"),
        _record(start=None, end=None, created=None, value=172.0,
                value_str="172.0"),
    ])
    assert r["seen"]["record"] == 2
    # NULL start/end hash to '' on both rows, and the value is numeric
    # identity, so the two renderings of 172 are one row.
    assert r["totals"]["records"] == 1
    assert _query("SELECT start_ts FROM records")[0][0] is None


def test_hash_expressions_bind_against_the_shipped_duckdb(sandbox):
    """Every hash expression must PARSE AND BIND in the build the server ships.

    The repo has been bitten once by SQL that ran in a container and failed in
    the shipped DuckDB (`hours` as a column alias). These expressions use
    `AT TIME ZONE`, `strftime` and `printf`; this is the test that says so out
    loud, against the real engine, before five million rows depend on it.
    """
    con = storage.connect()
    try:
        storage.init_schema(con)
        for table, expr in storage.HASH_EXPRESSIONS.items():
            con.execute(f'SELECT {expr} FROM "{table}" LIMIT 0')
        for table, derived in storage.DERIVED_HASH_COLUMNS.items():
            for expr in derived.values():
                con.execute(f'SELECT {expr} FROM "{table}" LIMIT 0')
    finally:
        con.close()


def test_value_str_is_no_longer_the_identity_of_a_quantity(sandbox):
    """Two renderings of one number are one row."""
    r = _import([
        _record(value=72.0, value_str="72"),
        _record(value=72.0, value_str="72.0"),
        _record(value=72.0, value_str="7.2000000e+01"),
    ])
    assert r["seen"]["record"] == 3
    assert r["totals"]["records"] == 1


def test_duplicates_within_one_batch_are_ignored_not_an_error(sandbox):
    """Colliding rows inside a SINGLE Arrow batch must be dropped, not raise.

    `_flush` inserts a whole 50k-row batch in one statement and leans on
    `ON CONFLICT DO NOTHING` for idempotency. The new hash makes a collision
    *within* one export possible where the old one did not (one sample written
    with two different `value_str` renderings), so this behaviour moves from
    incidental to load-bearing. If DuckDB ever raises "Duplicate key violates
    primary key constraint" here, `_flush` needs a
    `QUALIFY row_number() OVER (PARTITION BY <hash>) = 1` ahead of the
    ON CONFLICT — and this is the test that will say so.
    """
    r = _import([_record(value=72.0, value_str="72")] * 3)
    assert r["seen"]["record"] == 3
    assert r["totals"]["records"] == 1


def test_different_numbers_are_still_different_rows(sandbox):
    _import([_record(value=72.0, value_str="72"),
             _record(value=73.0, value_str="73")])
    assert _query("SELECT count(*) FROM records")[0][0] == 2


def test_category_rows_keep_value_str_as_their_identity(sandbox):
    """`value IS NULL` rows — 75.8k of them live — hash on Apple's string.

    Sleep stages and stand hours have no number at all; `value_str` carries the
    category name. Two stages of one night start at the same instant far more
    often than not, so dropping it would merge them.
    """
    night = datetime(2026, 8, 18, 1, 0, 0, tzinfo=timezone(timedelta(hours=3)))
    common = {"type": "sleep_analysis", "unit": None, "value": None,
              "start": night, "end": night + timedelta(hours=1)}
    _import([
        _record(value_str="HKCategoryValueSleepAnalysisAsleepCore", **common),
        _record(value_str="HKCategoryValueSleepAnalysisAsleepDeep", **common),
        _record(value_str="HKCategoryValueSleepAnalysisAsleepCore", **common),
    ])
    assert _query("SELECT count(*) FROM records")[0][0] == 2


def test_sub_unit_magnitudes_stay_distinct(sandbox):
    """Significant digits, not decimal places.

    `walking_asymmetry_percentage` lives around 1e-4. Rounding to a fixed
    number of DECIMAL places would flatten a whole metric into one bucket;
    rounding to significant digits keeps neighbours apart at any magnitude.
    """
    kw = {"type": "walking_asymmetry_percentage", "unit": "%"}
    _import([
        _record(value=0.000241262, value_str="0.000241262", **kw),
        _record(value=0.000241263, value_str="0.000241263", **kw),
        _record(value=0.000241264, value_str="0.000241264", **kw),
    ])
    assert _query("SELECT count(*) FROM records")[0][0] == 3


def test_float32_widening_noise_is_absorbed(sandbox):
    """A value that made a round trip through float32 is still the same row.

    This is the reason the precision sits at 6 significant digits and not
    higher: float32 carries ~7.2 decimal digits, so the noise it introduces has
    to fall *below* the cut or the two paths disagree.
    """
    kw = {"type": "sleeping_wrist_temperature", "unit": "degC"}
    exact = 35.7799987792969
    assert _float32(exact) != exact          # the divergence is real
    _import([
        _record(value=exact, value_str="35.7799987792969", **kw),
        _record(value=_float32(exact), value_str="35.779998779296875", **kw),
        _record(value=35.78, value_str="35.78", **kw),
    ])
    assert _query("SELECT count(*) FROM records")[0][0] == 1


def test_sub_second_precision_does_not_split_a_sample(sandbox):
    """HKSample dates carry sub-seconds; the export only ever carries seconds."""
    base = datetime(2026, 8, 18, 8, 0, 0, tzinfo=timezone.utc)
    _import([
        _record(start=base, end=base),
        _record(start=base + timedelta(microseconds=437),
                end=base + timedelta(microseconds=437)),
    ])
    assert _query("SELECT count(*) FROM records")[0][0] == 1


def test_same_instant_in_a_different_offset_is_one_row(sandbox):
    msk = timezone(timedelta(hours=3))
    base = datetime(2026, 8, 18, 8, 0, 0, tzinfo=msk)
    _import([_record(start=base, end=base),
             _record(start=base.astimezone(timezone.utc),
                     end=base.astimezone(timezone.utc))])
    assert _query("SELECT count(*) FROM records")[0][0] == 1


# --- timezone stability -------------------------------------------------------

def test_hash_survives_a_different_session_timezone(sandbox, tmp_path):
    """Import the same export twice under two session timezones: nothing added.

    Under the old `cast(start_ts AS VARCHAR)` this was the silent doubling bug —
    the instant is stored correctly, but its TEXT (and so the hash) followed
    whatever `SET TimeZone` was in force.
    """
    events = _export_events(tmp_path)
    first = _import(events, session_tz="UTC")
    assert first["added"]["records"] == N_RECORDS

    second = _import(_export_events(tmp_path), session_tz="Pacific/Kiritimati")
    assert second["seen"]["record"] == N_RECORDS      # the rows really were fed
    assert second["added"]["records"] == 0
    assert second["added"]["workouts"] == 0
    assert second["added"]["workout_events"] == 0
    assert second["added"]["sleep"] == 0


def test_the_old_rendering_really_was_session_dependent(sandbox, tmp_path):
    """Pin the defect itself, so the fix cannot be quietly reverted."""
    _import(_export_events(tmp_path), session_tz="UTC")
    con = storage.connect_readonly()
    try:
        try:
            con.execute("SET TimeZone='UTC'")
        except Exception as exc:                        # pragma: no cover
            pytest.skip(f"no session timezone support here: {exc}")
        old_a, new_a = con.execute(
            "SELECT cast(start_ts AS VARCHAR), "
            f"{storage.utc_ts_expr('start_ts')} "
            "FROM records ORDER BY start_ts LIMIT 1").fetchone()
        con.execute("SET TimeZone='Pacific/Kiritimati'")
        old_b, new_b = con.execute(
            "SELECT cast(start_ts AS VARCHAR), "
            f"{storage.utc_ts_expr('start_ts')} "
            "FROM records ORDER BY start_ts LIMIT 1").fetchone()
    finally:
        con.close()
    assert old_a != old_b, "expected the bare cast to follow the session TZ"
    assert new_a == new_b


# --- the acceptance test ------------------------------------------------------

def test_delta_over_the_same_window_adds_nothing(sandbox, tmp_path):
    """Full export, then a delta covering the same window -> zero rows added.

    This is the whole reason row identity changed. The delta is the same
    samples re-expressed the way HealthKit hands them over: numbers instead of
    Apple's text, sub-second NSDate instants, a different UTC offset, and
    source metadata that is not part of the identity.
    """
    events = _export_events(tmp_path)
    first = _import(events)
    assert first["added"] == {
        "records": N_RECORDS, "workouts": N_WORKOUTS,
        "workout_events": N_WORKOUT_EVENTS, "sleep": N_SLEEP,
        "clinical": 0, "activity_summary": 0,
    }

    delta = _as_delta(events)
    # The delta must genuinely carry the rows, or "zero added" is meaningless.
    kinds = [k for k, _ in delta]
    assert kinds.count("record") == N_RECORDS
    assert kinds.count("workout_event") == N_WORKOUT_EVENTS

    second = _import(delta)
    assert second["added"] == {t: 0 for t in second["added"]}, second["added"]
    assert second["totals"] == first["totals"]


def test_delta_workout_structure_still_links_to_its_parent(sandbox, tmp_path):
    """`workout_events.workout_hash` must keep joining to `workouts.row_hash`.

    Both are the same expression over the same identity fields; if only one of
    them had been migrated the children would orphan silently and the
    per-repetition analytics would go blank.
    """
    events = _export_events(tmp_path)
    _import(events)
    _import(_as_delta(events))
    orphans = _query(
        "SELECT count(*) FROM workout_events e "
        "LEFT JOIN workouts w ON w.row_hash = e.workout_hash "
        "WHERE w.row_hash IS NULL")[0][0]
    assert orphans == 0
    assert _query("SELECT count(*) FROM workout_events_dedup")[0][0] == \
        N_WORKOUT_EVENTS


def test_non_breaking_space_in_source_name_is_not_folded(sandbox, tmp_path):
    """`source_name` is in the hash and the export really contains U+00A0."""
    _import(_export_events(tmp_path))
    names = {r[0] for r in _query("SELECT DISTINCT source_name FROM records")}
    assert "\u00a0" in SOURCE         # the fixture really carries U+00A0
    assert SOURCE in names            # ... and it survived the round trip


# --- the migration ------------------------------------------------------------

T0 = datetime(2026, 8, 18, 8, 0, 0, tzinfo=timezone(timedelta(hours=3)))
T1 = T0 + timedelta(hours=1)

_REC_INSERT = (
    "INSERT INTO records (row_hash, type, raw_type, source_name, "
    "source_version, device, unit, value, value_str, start_ts, end_ts, "
    "created_ts, source_priority) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
)


def _legacy_database(fingerprint: str = "deadbeef") -> None:
    """A database in the shape the migration has to fix.

    The stored `row_hash` values are stale on purpose — the migration recomputes
    identity from the columns and never reads the old hash, so their exact form
    does not matter, only that they are wrong. Two records differ *only* in
    `value_str`, which is precisely the pair the new expression must merge.

    The third record is the control that must NOT merge, and it sits at its own
    instant (T1) rather than sharing T0 with the pair. Two heart-rate samples
    from one source stamped at the identical second is not a thing the watch
    produces; and because `records_dedup` partitions on
    `(type, start_ts, end_ts)` and ignores `value` entirely, a control sharing
    T0 is not a control at all — the view would fold it into the survivor and
    report 1 row whether or not the migration behaved.
    """
    con = storage.connect()
    try:
        storage.init_schema(con)
        rows = [
            ("old-rec-0", 72.0, "72", T0),
            ("old-rec-1", 72.0, "72.0", T0),  # collapses into old-rec-0
            ("old-rec-2", 99.0, "99", T1),    # survives: different instant
        ]
        for row_hash, value, value_str, ts in rows:
            con.execute(_REC_INSERT, (
                row_hash, "heart_rate", "HKQuantityTypeIdentifierHeartRate",
                SOURCE, "26.6", None, "count/min", value, value_str,
                ts, ts, ts, 30))
        con.execute(
            "INSERT INTO workouts (row_hash, type, raw_type, source_name, "
            "device, duration, duration_unit, start_ts, end_ts) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("old-wk-0", "running", "HKWorkoutActivityTypeRunning", SOURCE,
             None, 60.0, "min", T0, T1))
        for i, kind in enumerate(("event", "activity")):
            con.execute(
                "INSERT INTO workout_events (row_hash, workout_hash, "
                "workout_type, workout_source_name, workout_start_ts, "
                "workout_end_ts, event_kind, raw_event_type, step_index, "
                "start_ts, end_ts, duration, duration_unit) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"old-ev-{i}", "old-wk-0", "running", SOURCE, T0, T1, kind,
                 "HKWorkoutEventTypeSegment", i, T0, T1, 5.541540004809698,
                 "min"))
    finally:
        con.close()
    config.IMPORT_STATE_PATH.write_text(json.dumps(
        {"last_fingerprint": fingerprint, "last_archive": "export.zip"}))


def _run_migration(mod, tmp_path, **kw):
    opts = dict(dry_run=False, resume=False, allow_collapse=True,
                max_collapse_pct=1.0, sample=0, skip_space_check=True)
    opts.update(kw)
    return mod.migrate(config.DB_PATH, tmp_path / "wd", **opts)


def test_migration_refuses_a_collapse_over_the_threshold(sandbox, tmp_path,
                                                         migrate_mod):
    """A big collapse is a red flag, not a success. Nothing may be swapped."""
    _legacy_database()
    rc = _run_migration(migrate_mod, tmp_path, allow_collapse=False,
                        max_collapse_pct=1.0)
    assert rc == 3
    # The live database is untouched: still three rows, still the stale hashes.
    assert _query("SELECT count(*) FROM records")[0][0] == 3
    assert _query("SELECT row_hash FROM records WHERE value_str = '72.0'"
                  )[0][0] == "old-rec-1"
    # And the import fingerprint has NOT been invalidated.
    state = json.loads(config.IMPORT_STATE_PATH.read_text())
    assert state["last_fingerprint"] == "deadbeef"
    # The migrated copy is left behind for inspection.
    assert (tmp_path / "wd" / "working.duckdb").exists()
    report = json.loads((tmp_path / "wd" / "report.json").read_text())
    assert report["tables"]["records"]["collapsed"] == 1


def test_migration_dry_run_leaves_the_live_database_alone(sandbox, tmp_path,
                                                          migrate_mod):
    _legacy_database()
    assert _run_migration(migrate_mod, tmp_path, dry_run=True) == 0
    assert _query("SELECT count(*) FROM records")[0][0] == 3
    assert json.loads(config.IMPORT_STATE_PATH.read_text())[
        "last_fingerprint"] == "deadbeef"


def test_migration_rehashes_collapses_and_invalidates_import_state(
        sandbox, tmp_path, migrate_mod):
    _legacy_database()
    assert _run_migration(migrate_mod, tmp_path) == 0

    # The original is moved aside, never deleted.
    backups = list(config.DB_PATH.parent.glob(
        config.DB_PATH.name + ".pre_rowhash_*.bak"))
    assert len(backups) == 1
    assert config.DB_PATH.exists()

    # Every row now hashes to what the current expression says it should.
    for table, expr in storage.HASH_EXPRESSIONS.items():
        wrong = _query(
            f'SELECT count(*) FROM "{table}" t '
            f"WHERE t.row_hash IS DISTINCT FROM ({expr})")[0][0]
        assert wrong == 0, table
    parent = storage.DERIVED_HASH_COLUMNS["workout_events"]["workout_hash"]
    assert _query("SELECT count(*) FROM workout_events t "
                  f"WHERE t.workout_hash IS DISTINCT FROM ({parent})"
                  )[0][0] == 0

    # The two rows that differed only in value_str are now one.
    assert _query("SELECT count(*) FROM records")[0][0] == 2

    # Parent and child were rehashed with the SAME expression, so the join
    # still holds. Migrating only one of the two tables would break it here.
    assert _query(
        "SELECT count(*) FROM workout_events e "
        "LEFT JOIN workouts w ON w.row_hash = e.workout_hash "
        "WHERE w.row_hash IS NULL")[0][0] == 0

    # Both views survive the table rebuild.
    assert _query("SELECT count(*) FROM records_dedup")[0][0] == 2
    assert _query("SELECT count(*) FROM workout_events_dedup")[0][0] == 2

    # The next export must be re-read in full under the new hash.
    state = json.loads(config.IMPORT_STATE_PATH.read_text())
    assert "last_fingerprint" not in state
    assert state["row_hash_migration"]["invalidated_fingerprint"] == "deadbeef"
    assert state["row_hash_migration"]["value_sig_digits"] == \
        storage.VALUE_SIG_DIGITS
    assert state["last_archive"] == "export.zip"


def test_migrated_database_accepts_a_delta_without_duplicating(
        sandbox, tmp_path, migrate_mod):
    """End to end: legacy DB -> migrate -> import the same rows as a delta."""
    _legacy_database()
    assert _run_migration(migrate_mod, tmp_path) == 0
    before = _query("SELECT count(*) FROM records")[0][0]

    delta = _import([
        # Same two samples, arriving the way the app would send them.
        _record(value=72.0 * (1 + 1e-13), value_str="72.000000000000",
                start=T0.astimezone(timezone.utc) + timedelta(microseconds=9),
                end=T0.astimezone(timezone.utc) + timedelta(microseconds=9)),
        _record(value=99.0, value_str="99.0", start=T1, end=T1),
    ])
    assert delta["added"]["records"] == 0
    assert _query("SELECT count(*) FROM records")[0][0] == before


def test_migration_resumes_from_a_dry_run(sandbox, tmp_path, migrate_mod):
    """`--resume` picks up the verified working copy instead of redoing it."""
    _legacy_database()
    assert _run_migration(migrate_mod, tmp_path, dry_run=True) == 0
    assert _run_migration(migrate_mod, tmp_path, resume=True) == 0
    assert _query("SELECT count(*) FROM records")[0][0] == 2
    assert "last_fingerprint" not in json.loads(
        config.IMPORT_STATE_PATH.read_text())
