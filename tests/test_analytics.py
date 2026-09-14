"""Tests for the intraday training-analytics helpers and the three tools built
on them (get_workout_detail, get_hr_zones, get_training_load).

Pure math is tested directly; the DB-backed tools run against a synthetic
export.xml (a single running workout with intraday HR/power/speed/stride/distance
records, plus resting_heart_rate and active_energy) imported into a sandbox DB.
No real personal data is used.
"""
from __future__ import annotations

import math
import zipfile
from datetime import datetime, timedelta

import pytest

from apple_health_mcp import analytics, config, import_pipeline

# --- synthetic export ---------------------------------------------------------

_WORKOUT_START = datetime(2024, 5, 3, 6, 0, 0)   # naive; -0700 appended below
_WORKOUT_SECS = 600                              # 10-minute run
_STEP = 30                                        # one sample every 30 s


def _ts(base: datetime, offset_s: int) -> str:
    return (base + timedelta(seconds=offset_s)).strftime("%Y-%m-%d %H:%M:%S -0700")


def _build_xml() -> str:
    rows: list[str] = []

    def rec(rtype, unit, value, ts):
        rows.append(
            f'<Record type="{rtype}" sourceName="Maksim\'s Apple Watch" '
            f'unit="{unit}" value="{value}" startDate="{ts}" endDate="{ts}" '
            f'creationDate="{ts}"/>'
        )

    n = _WORKOUT_SECS // _STEP  # 20 samples
    for i in range(n):
        ts = _ts(_WORKOUT_START, i * _STEP)
        hr = 130 + (40 * i / (n - 1))            # 130 -> 170 (drift, HR rises)
        rec("HKQuantityTypeIdentifierHeartRate", "count/min", round(hr, 1), ts)
        rec("HKQuantityTypeIdentifierRunningPower", "W", 200, ts)      # constant
        rec("HKQuantityTypeIdentifierRunningSpeed", "km/hr", 12, ts)   # constant
        rec("HKQuantityTypeIdentifierRunningStrideLength", "m", 1.2, ts)
        # 12 km/h for 30 s = 0.1 km per sample -> 2.0 km total.
        rec("HKQuantityTypeIdentifierDistanceWalkingRunning", "km", 0.1, ts)
        rec("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", 6, ts)

    # Resting HR skewed high (mean ~73) with a couple of genuinely low readings,
    # so a low percentile (~56) is clearly distinct from the mean.
    resting_vals = [55, 56, 74, 75, 76, 77, 78, 79, 80, 81]
    for i, v in enumerate(resting_vals):
        ts = _ts(datetime(2024, 4, 21, 7, 0, 0), i * 86400)
        rec("HKQuantityTypeIdentifierRestingHeartRate", "count/min", v, ts)

    # Two low resting-time HR samples (well below 60% of max) so scope='all'
    # zone totals include sub-threshold HR in Z1.
    rec("HKQuantityTypeIdentifierHeartRate", "count/min", 50,
        "2024-05-04 03:00:00 -0700")
    rec("HKQuantityTypeIdentifierHeartRate", "count/min", 50,
        "2024-05-04 03:00:30 -0700")

    # A non-workout day (2024-05-05) with lots of active energy -> energy proxy.
    for h in range(10):
        ts = f"2024-05-05 {10 + h:02d}:00:00 -0700"
        rec("HKQuantityTypeIdentifierActiveEnergyBurned", "kcal", 80, ts)  # 800

    workout = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        'sourceName="Maksim\'s Apple Watch" duration="10" durationUnit="min" '
        f'startDate="{_ts(_WORKOUT_START, 0)}" '
        f'endDate="{_ts(_WORKOUT_START, _WORKOUT_SECS)}">'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" '
        'average="150" maximum="180" unit="count/min"/>'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierActiveEnergyBurned" '
        'sum="120" unit="Cal"/>'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" '
        'sum="2.0" unit="km"/>'
        '</Workout>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<HealthData locale="en_US">\n'
        '<ExportDate value="2024-06-01 09:00:00 -0700"/>\n'
        + "\n".join(rows) + "\n" + workout + "\n</HealthData>\n"
    )


def _build_long_xml() -> str:
    """~7 weeks of one active_energy record per day (700 kcal) so every day gets a
    steady energy-proxy load and ACWR has real 28-day history to seed from.
    00:00 -0700 -> 12:00 local (+05), keeping each record on its label date."""
    rows = []
    start = datetime(2024, 3, 1, 0, 0, 0)
    for i in range(51):                       # 2024-03-01 .. 2024-04-20
        ts = _ts(start, i * 86400)
        rows.append(
            '<Record type="HKQuantityTypeIdentifierActiveEnergyBurned" '
            f'sourceName="Maksim\'s Apple Watch" unit="kcal" value="700" '
            f'startDate="{ts}" endDate="{ts}" creationDate="{ts}"/>'
        )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<ExportDate value="2024-06-01 09:00:00 -0700"/>\n'
        + "\n".join(rows) + "\n</HealthData>\n"
    )


def _build_partial_xml() -> str:
    """A 2.5 km run at constant 12 km/h, so the final km is a 0.5 km partial."""
    rows = []

    def rec(rtype, unit, value, ts):
        rows.append(
            f'<Record type="{rtype}" sourceName="Maksim\'s Apple Watch" '
            f'unit="{unit}" value="{value}" startDate="{ts}" endDate="{ts}" '
            f'creationDate="{ts}"/>'
        )

    start = datetime(2024, 6, 1, 6, 0, 0)
    n = 25                                    # 25 * 0.1 km = 2.5 km
    for i in range(n):
        ts = _ts(start, i * 30)
        rec("HKQuantityTypeIdentifierHeartRate", "count/min", 150, ts)
        rec("HKQuantityTypeIdentifierRunningSpeed", "km/hr", 12, ts)
        rec("HKQuantityTypeIdentifierDistanceWalkingRunning", "km", 0.1, ts)
    workout = (
        '<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        'sourceName="Maksim\'s Apple Watch" duration="12.5" durationUnit="min" '
        f'startDate="{_ts(start, 0)}" endDate="{_ts(start, n * 30)}">'
        '<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" '
        'sum="2.5" unit="km"/>'
        '</Workout>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<ExportDate value="2024-06-01 09:00:00 -0700"/>\n'
        + "\n".join(rows) + "\n" + workout + "\n</HealthData>\n"
    )


def _install(tmp_path, monkeypatch, xml: str):
    db = tmp_path / "health.duckdb"
    exp = tmp_path / "AppleHealthExport"
    exp.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "EXPORT_DIR", exp)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    # The HR anchors resolve through `zones`, which reads the recalibration
    # reference first. Point it at a path that does not exist so these tests
    # keep exercising the observed-max fallback whatever the recalibration job
    # has left in data/ on the developer's machine.
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH",
                        tmp_path / "no_calibration.json")
    archive = exp / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
    import_pipeline.import_archive(archive)
    from apple_health_mcp import server
    server._schema_ready = False   # re-init schema against this sandbox DB
    return tmp_path


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_xml())


@pytest.fixture()
def long_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_long_xml())


@pytest.fixture()
def partial_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_partial_xml())


# --- pure math ----------------------------------------------------------------

def test_zone_bounds():
    b = analytics.zone_bounds(200)
    assert [z["zone"] for z in b] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    # Z1 spans everything below 60% (no sub-50% gap): floor is 0, top is 60%.
    assert b[0]["lo_bpm"] == 0 and b[0]["hi_bpm"] == 120
    assert b[4]["lo_bpm"] == 180 and b[4]["hi_bpm"] == 200


def test_zone_case_sql_thresholds():
    sql = analytics.zone_case_sql(200, "hr")
    # 90/80/70/60 % of 200 = 180/160/140/120.
    assert "180.0 THEN 'Z5'" in sql
    assert "120.0 THEN 'Z2'" in sql


def test_trimp_known_value():
    # HRr = (150-50)/(200-50) = 0.6667; 60*0.6667*0.64*e^(1.92*0.6667)
    hrr = 100 / 150
    expected = 60 * hrr * 0.64 * math.exp(1.92 * hrr)
    assert abs(analytics.trimp(60, 150, 50, 200) - round(expected, 1)) < 0.1


def test_trimp_degenerate():
    assert analytics.trimp(60, None, 50, 200) is None
    assert analytics.trimp(60, 150, 200, 200) is None   # max <= rest


def test_decoupling():
    assert analytics.decoupling(0.80, 0.84) == 5.0
    assert analytics.decoupling(None, 0.9) is None


def test_pace_and_cadence():
    assert analytics.pace_min_per_km(12) == 5.0        # 60/12
    assert analytics.pace_min_per_km(0) is None
    assert analytics.cadence_spm(12, 1.2) == 166.7     # (12*1000/60)/1.2
    assert analytics.cadence_spm(12, None) is None


def test_acwr_sweet_spot():
    dates = [str(datetime(2024, 1, 1).date() + timedelta(days=i))
             for i in range(30)]
    loads = [10.0] * 30
    series = analytics.acwr(dates, loads)
    last = series[-1]
    assert last["acwr"] == 1.0            # constant load -> ratio 1
    assert last["flag"] == "sweet_spot"
    assert series[0]["flag"] == "insufficient_history"


def test_acwr_min_history_date():
    from datetime import date
    dates = [str(date(2024, 1, 1) + timedelta(days=i)) for i in range(35)]
    loads = [10.0] * 35
    # Dataset began long before this window -> even day 0 has enough history,
    # so no day is flagged insufficient_history (the Fix-1 seeding behaviour).
    early = analytics.acwr(dates, loads, min_history_date=date(2020, 1, 1))
    assert all(a["flag"] != "insufficient_history" for a in early)
    assert early[-1]["acwr"] == 1.0
    # Dataset genuinely starts on day 0 -> first 27 days are insufficient.
    seeded = analytics.acwr(dates, loads, min_history_date=date(2024, 1, 1))
    assert seeded[0]["flag"] == "insufficient_history"
    assert seeded[27]["flag"] != "insufficient_history"


# --- tools --------------------------------------------------------------------

def test_get_workout_detail(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail(bin_seconds=30)

    # Summary reflects the workout and reports the max_hr actually used.
    assert d["summary"]["type"] == "running"
    assert d["summary"]["distance_km"] == 2.0
    assert d["summary"]["max_hr_used"] == 180        # from workouts.max_hr
    assert "estimated" in d["summary"]["max_hr_source"]

    # Series is non-empty with derived pace + cadence.
    pts = d["series"]["points"]
    assert len(pts) >= 10
    assert pts[0]["hr"] is not None
    assert pts[0]["pace_min_per_km"] == 5.0          # 12 km/h
    assert abs(pts[0]["cadence_spm"] - 166.7) < 0.5

    # HR zones cover roughly the workout duration (~9.5 min of 30 s samples).
    assert 8.0 <= d["hr_zones"]["total_minutes"] <= 10.5
    assert sum(z["seconds"] for z in d["hr_zones"]["zones"]) > 0

    # Rising HR against constant power -> positive drift on the power basis.
    assert d["decoupling"]["basis"] == "hr_to_power"
    assert d["decoupling"]["drift_pct"] > 0

    # 2 km of distance -> two per-km splits.
    assert len(d["splits"]) == 2


def test_get_workout_detail_no_match(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail(type="swimming")
    assert "error" in d


def test_get_hr_zones_scopes(sandbox):
    from apple_health_mcp import server
    w = server.get_hr_zones("2024-05-01", "2024-05-31", scope="workouts")
    assert w["max_hr_used"] == 180
    assert w["total_minutes"] > 0
    # All in-workout HR is well above 60% of 180 (108 bpm) -> nothing in Z1.
    z1w = next(z for z in w["zones"] if z["zone"] == "Z1")
    assert z1w["seconds"] == 0.0
    assert abs(sum(z["share"] for z in w["zones"]) - 1.0) < 0.05

    # scope='all' also picks up the two low resting-time HR samples (~50 bpm),
    # which land in Z1 (no sub-threshold drop) and add time beyond the workout.
    a = server.get_hr_zones("2024-05-01", "2024-05-31", scope="all")
    z1a = next(z for z in a["zones"] if z["zone"] == "Z1")
    assert z1a["seconds"] > 0
    assert a["total_minutes"] >= w["total_minutes"]


def test_zone_definitions_aligned(sandbox):
    from apple_health_mcp import server
    wd = server.get_workout_detail()["hr_zones"]["zones"]
    hz = server.get_hr_zones("2024-05-01", "2024-05-31", scope="all")["zones"]
    # Both tools resolve the same max_hr and must report identical zone bounds.
    key = lambda zs: [(z["zone"], z["lo_bpm"], z["hi_bpm"]) for z in zs]
    assert key(wd) == key(hz)
    assert wd[0]["zone"] == "Z1" and wd[0]["lo_bpm"] == 0   # Z1 = below 60%


def test_get_training_load(sandbox):
    from apple_health_mcp import server
    t = server.get_training_load("2024-05-01", "2024-05-31")
    # resting_hr is a low percentile (~56 here), well below the ~73 mean.
    assert 50 <= t["resting_hr_used"] <= 62
    assert "p10" in t["resting_hr_source"]
    assert t["max_hr_used"] == 180

    days = {d["date"]: d for d in t["daily"]}
    # Workout day uses TRIMP (06:00 local, stable across session TZ).
    assert days["2024-05-03"]["method"] == "trimp"
    assert days["2024-05-03"]["load"] > 0
    # The high-energy non-workout day shows up as an energy proxy. Its exact
    # calendar date depends on the session timezone, so assert on the method.
    proxy = [d for d in t["daily"]
             if d["method"] == "energy_proxy" and d["load"] > 0]
    assert proxy

    # ACWR series is contiguous and flags each day.
    assert t["acwr"] and all("flag" in a for a in t["acwr"])
    assert t["latest_acwr"] is not None
    # daily/acwr are emitted only for the display range (not the seeding lookback).
    assert all("2024-05-01" <= d["date"] <= "2024-05-31" for d in t["daily"])


def test_acwr_seeded_from_history(long_sandbox):
    from apple_health_mcp import server
    # Query a short sub-range that starts >28 days into the fixture's history.
    t = server.get_training_load("2024-04-15", "2024-04-20")
    assert t["acwr"]
    first = t["acwr"][0]
    # Chronic is seeded from the 28 days BEFORE the display start, so the first
    # day is not flagged insufficient and its baseline reflects real prior load
    # (steady 57/day -> weekly ~399), not a near-zero day-one value.
    assert first["flag"] != "insufficient_history"
    assert first["chronic_28d_weekly"] > 300
    assert first["acwr"] == pytest.approx(1.0, abs=0.1)


def test_partial_split_pace(partial_sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail()
    splits = d["splits"]
    assert len(splits) == 3                     # 2 full km + a 0.5 km partial
    first, last = splits[0], splits[-1]
    # Full km unchanged: ~5.0 min/km at 12 km/h, no partial marker.
    assert "partial" not in first
    assert first["pace_min_per_km"] == pytest.approx(5.0, abs=0.3)
    # Trailing partial: flagged, real distance, pace scaled by distance (not the
    # raw elapsed-minutes-as-a-full-km that would read misleadingly fast).
    assert last["partial"] is True
    assert last["distance_km"] == pytest.approx(0.5, abs=0.05)
    assert last["pace_min_per_km"] > last["duration_sec"] / 60.0
