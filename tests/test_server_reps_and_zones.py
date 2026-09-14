"""Tests for the `server.py` wiring of `zones` and the `workout_events` table.

Covers what `get_workout_detail` / `get_hr_zones` gained:

* HR anchors resolved through `zones` (calibrated value beats the observed max).
* Both zone models emitted side by side, with the percentage model unchanged.
* The `reps` section: decoding, role labelling, pace, and the no-structure case.
* Aerobic decoupling suppressed on an interval session.
* `include` filtering, with the default preserving today's response.

Two halves. The first needs no database: `_build_reps`, `_rep_role` and
`_include_sections` are pure and take plain row dicts. The second imports a
synthetic export (an interval session stored TWICE under different source names,
exactly as the real export does, plus one ordinary run) into a sandbox DB.
No real personal data is used.
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime, timedelta, timezone

import pytest

from apple_health_mcp import config, import_pipeline, storage, zones

TZ = timezone(timedelta(hours=3))          # the export's own +0300


# --- pure helpers: rows in, labelled steps out --------------------------------

def _row(index, key_path, block, repeat, slot, minutes, km, avg_hr,
         successful=True):
    """One `workout_events` activity row, shaped as `_q` returns it."""
    start = datetime(2026, 8, 20, 13, 0, 0, tzinfo=TZ) + timedelta(minutes=index * 6)
    return {
        "step_index": index, "step_key_path": key_path,
        "step_block": block, "step_repeat": repeat, "step_slot": slot,
        "step_successful": successful,
        "start_ts": start, "end_ts": start + timedelta(minutes=minutes),
        "duration": minutes, "duration_unit": "min",
        "distance": km, "distance_unit": "km",
        "avg_hr": avg_hr, "min_hr": avg_hr - 8, "max_hr": avg_hr + 6,
        "avg_speed": None, "speed_unit": None,
    }


def _five_by_one_k():
    """Warm-up, 5 x (1000 m work + recovery), cool-down — the real session."""
    rows = [_row(0, "0.0.0", 0, 0, 0, 12.0, 2.2, 148)]
    i = 1
    for k in range(5):
        rows.append(_row(i, f"1.{k}.0", 1, k, 0, 3.8, 1.0, 182)); i += 1
        rows.append(_row(i, f"1.{k}.1", 1, k, 1, 2.5, 0.30, 150)); i += 1
    rows.append(_row(i, "2.0.0", 2, 0, 0, 10.0, 1.7, 142))
    return rows


def test_roles_label_warmup_reps_recoveries_and_cooldown():
    from apple_health_mcp import server
    reps = server._build_reps(_five_by_one_k(), zones.training_zone_bounds())
    assert [s["role"] for s in reps["steps"]] == (
        ["warmup"] + ["work", "recovery"] * 5 + ["cooldown"])
    assert reps["roles"] == {"warmup": 1, "work": 5, "recovery": 5, "cooldown": 1}
    assert reps["structured"] is True
    assert reps["interval_structure"] is True
    # step_slot 0 is the work interval and 1 the recovery -- not the reverse.
    work = [s for s in reps["steps"] if s["role"] == "work"]
    assert all(s["slot"] == 0 for s in work)
    assert all(s["slot"] == 1 for s in reps["steps"] if s["role"] == "recovery")


def test_pace_comes_from_distance_and_duration():
    from apple_health_mcp import server
    reps = server._build_reps(_five_by_one_k(), zones.training_zone_bounds())
    work = [s for s in reps["steps"] if s["role"] == "work"]
    assert all(s["pace_min_per_km"] == 3.8 for s in work)      # 3.8 min / 1.0 km
    # The recoveries are far slower, and are not averaged into the rep pace.
    rec = [s for s in reps["steps"] if s["role"] == "recovery"]
    assert all(s["pace_min_per_km"] == pytest.approx(8.33, abs=0.01) for s in rec)
    assert reps["work_summary"]["avg_pace_min_per_km"] == 3.8
    assert reps["work_summary"]["count"] == 5
    assert reps["work_summary"]["distance_km"] == 5.0


def test_each_step_carries_its_training_zone():
    from apple_health_mcp import server
    reps = server._build_reps(_five_by_one_k(), zones.training_zone_bounds())
    by_path = {s["key_path"]: s["training_zone"] for s in reps["steps"]}
    assert by_path["0.0.0"] == "recovery"      # 148 bpm warm-up
    assert by_path["1.0.0"] == "threshold"     # 182 bpm rep -- NOT "grey"
    assert by_path["1.0.1"] == "easy"          # 150 bpm jog recovery
    assert reps["work_summary"]["training_zone_counts"] == {"threshold": 5}


def test_a_repeat_block_beats_its_position_in_the_session():
    """A session whose only blocks are 0 and 1 must not lose its reps.

    Warm-up is block 0 and cool-down the last block, but a repeat block can also
    be first or last; the slot then decides, or a 2-block session would report a
    cool-down and no work at all.
    """
    from apple_health_mcp import server
    rows = [_row(0, "0.0.0", 0, 0, 0, 8.6, 1.5, 164),
            _row(1, "1.0.0", 1, 0, 0, 5.0, 1.0, 176),
            _row(2, "1.0.1", 1, 0, 1, 2.6, 0.37, 166, successful=False)]
    reps = server._build_reps(rows, zones.training_zone_bounds())
    assert [s["role"] for s in reps["steps"]] == ["warmup", "work", "recovery"]
    assert reps["steps"][2]["successful"] is False


def test_a_single_step_is_not_mislabelled_as_a_warmup():
    from apple_health_mcp import server
    reps = server._build_reps([_row(0, "0.0.0", 0, 0, 0, 30.0, 6.0, 160)],
                              zones.training_zone_bounds())
    assert reps["steps"][0]["role"] == "work"
    assert reps["interval_structure"] is False   # one effort is not an interval


def test_steps_without_a_key_path_still_decode():
    from apple_health_mcp import server
    reps = server._build_reps([_row(0, None, None, None, None, 5.0, 1.0, 170)],
                              zones.training_zone_bounds())
    assert reps["steps"][0]["role"] == "step"
    assert reps["steps"][0]["training_zone"] == "grey"


def test_units_are_normalised_before_the_pace_division():
    from apple_health_mcp import server
    row = _row(0, "1.0.0", 1, 0, 0, 300.0, 1000.0, 180)
    row["duration_unit"], row["distance_unit"] = "s", "m"
    step = server._build_reps([row], zones.training_zone_bounds())["steps"][0]
    assert (step["duration_min"], step["distance_km"]) == (5.0, 1.0)
    assert step["pace_min_per_km"] == 5.0


def test_missing_quantities_degrade_to_null_not_a_wrong_pace():
    from apple_health_mcp import server
    no_dist = _row(0, "1.0.0", 1, 0, 0, 5.0, None, 180)
    no_dist["distance"] = None
    step = server._build_reps([no_dist], zones.training_zone_bounds())["steps"][0]
    assert step["distance_km"] is None and step["pace_min_per_km"] is None
    # A missing duration quantity falls back to the step's own timestamps.
    no_dur = _row(0, "1.0.0", 1, 0, 0, 4.0, 1.0, 180)
    no_dur["duration"] = None
    step = server._build_reps([no_dur], zones.training_zone_bounds())["steps"][0]
    assert step["duration_min"] == 4.0


def test_no_structure_is_an_explicit_marker_not_an_error():
    from apple_health_mcp import server
    reps = server._build_reps([], zones.training_zone_bounds())
    assert reps["structured"] is False
    assert reps["interval_structure"] is False
    assert reps["count"] == 0 and reps["steps"] == [] and reps["roles"] == {}
    assert reps["work_summary"] is None
    assert "normal case" in reps["note"]


@pytest.mark.parametrize("arg,expected", [
    (None, set(("series", "zones", "splits", "decoupling", "reps"))),
    ([], set(("series", "zones", "splits", "decoupling", "reps"))),
    (["all"], set(("series", "zones", "splits", "decoupling", "reps"))),
    (["zones"], {"zones"}),
    (["zones", "reps"], {"zones", "reps"}),
    ("zones,reps", {"zones", "reps"}),
    (["ZONES"], {"zones"}),
])
def test_include_parsing(arg, expected):
    from apple_health_mcp import server
    wanted, err = server._include_sections(arg)
    assert err is None and wanted == expected


def test_include_rejects_an_unknown_section():
    from apple_health_mcp import server
    wanted, err = server._include_sections(["zones", "bogus"])
    assert wanted == set()
    assert "bogus" in err and "series" in err


# --- synthetic export ---------------------------------------------------------

_A_START = datetime(2026, 8, 20, 13, 0, 0, tzinfo=TZ)    # structured intervals
_B_START = datetime(2026, 8, 18, 11, 0, 0, tzinfo=TZ)    # ordinary steady run


def _t(base: datetime, seconds: int) -> str:
    return (base + timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S %z")


def _rec(rows, rtype, unit, value, ts):
    rows.append(
        f'<Record type="{rtype}" sourceName="Maksim\'s Apple Watch" '
        f'unit="{unit}" value="{value}" startDate="{ts}" endDate="{ts}" '
        f'creationDate="{ts}"/>')


def _hr_stat(avg, lo, hi, start, end):
    return (f'<WorkoutStatistics type="HKQuantityTypeIdentifierHeartRate" '
            f'startDate="{start}" endDate="{end}" average="{avg}" '
            f'minimum="{lo}" maximum="{hi}" unit="count/min"/>')


def _activity(uuid, key_path, t0, t1, minutes, km, avg_hr, ok):
    """One <WorkoutActivity>: a single repetition of a structured session."""
    return (
        f'<WorkoutActivity uuid="{uuid}" startDate="{t0}" endDate="{t1}" '
        f'duration="{minutes}" durationUnit="min">'
        f'<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" '
        f'startDate="{t0}" endDate="{t1}" sum="{km}" unit="km"/>'
        + _hr_stat(avg_hr, avg_hr - 8, avg_hr + 6, t0, t1) +
        f'<MetadataEntry key="WOIntervalStepKeyPath" value="{key_path}"/>'
        f'<MetadataEntry key="WOIntervalStepSuccessful" value="{1 if ok else 0}"/>'
        '</WorkoutActivity>')


# key_path, start offset (s), end offset (s), duration (min), km, avg hr, ok
_STEPS = [
    ("0.0.0",    0,  300, 5.0, 1.0, 172, True),   # warm-up   (block 0)
    ("1.0.0",  300,  540, 4.0, 1.0, 182, True),   # work rep 1
    ("1.0.1",  540,  660, 2.0, 0.3, 150, True),   # recovery 1
    ("1.1.0",  660,  900, 4.0, 1.0, 184, True),   # work rep 2
    ("1.1.1",  900, 1020, 2.0, 0.3, 148, False),  # recovery 2 (failed step)
    ("2.0.0", 1020, 1200, 3.0, 0.6, 145, True),   # cool-down (last block)
]


def _interval_workout(source_name: str) -> str:
    """The structured session. Emitted twice under two source names, because
    the real export stores one session 2-3 times and the events hang off
    whichever copy -- the reps must not triple."""
    t0, t1 = _t(_A_START, 0), _t(_A_START, 1200)
    acts = "".join(
        _activity(f"UUID-{i}", kp, _t(_A_START, s), _t(_A_START, e),
                  mins, km, hr, ok)
        for i, (kp, s, e, mins, km, hr, ok) in enumerate(_STEPS))
    return (
        f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        f'duration="20.0" durationUnit="min" sourceName="{source_name}" '
        f'creationDate="{t1}" startDate="{t0}" endDate="{t1}">'
        + acts
        + _hr_stat(177, 140, 205, t0, t1)
        + f'<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" '
          f'startDate="{t0}" endDate="{t1}" sum="4.2" unit="km"/>'
        + '</Workout>')


def _build_xml() -> str:
    rows: list[str] = []

    # Workout A intraday HR: 20 samples at 172 bpm (grey), then 20 at 182
    # (threshold). Each sample is weighted by the gap to the next (30 s), and
    # the very last sample has no successor -> grey 20*30 s, threshold 19*30 s.
    for i in range(20):
        _rec(rows, "HKQuantityTypeIdentifierHeartRate", "count/min", 172,
             _t(_A_START, i * 30))
    for i in range(20):
        _rec(rows, "HKQuantityTypeIdentifierHeartRate", "count/min", 182,
             _t(_A_START, 600 + i * 30))

    # Workout B: HR drifting 130 -> 170 against constant power, so aerobic
    # decoupling is computable and positive on the hr_to_power basis.
    for i in range(20):
        ts = _t(_B_START, i * 30)
        _rec(rows, "HKQuantityTypeIdentifierHeartRate", "count/min",
             round(130 + 40 * i / 19, 1), ts)
        _rec(rows, "HKQuantityTypeIdentifierRunningPower", "W", 200, ts)
        _rec(rows, "HKQuantityTypeIdentifierRunningSpeed", "km/hr", 12, ts)
        _rec(rows, "HKQuantityTypeIdentifierDistanceWalkingRunning", "km", 0.1, ts)

    b0, b1 = _t(_B_START, 0), _t(_B_START, 600)
    plain = (
        f'<Workout workoutActivityType="HKWorkoutActivityTypeRunning" '
        f'duration="10.0" durationUnit="min" sourceName="Maksim\'s Apple Watch" '
        f'creationDate="{b1}" startDate="{b0}" endDate="{b1}">'
        + _hr_stat(150, 130, 185, b0, b1)
        + f'<WorkoutStatistics type="HKQuantityTypeIdentifierDistanceWalkingRunning" '
          f'startDate="{b0}" endDate="{b1}" sum="2.0" unit="km"/>'
        + '</Workout>')

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<ExportDate value="2026-08-21 09:00:00 +0300"/>\n'
        + "\n".join(rows) + "\n" + plain + "\n"
        + _interval_workout("Maksim's Apple Watch") + "\n"
        + _interval_workout("Apple Watch") + "\n"     # the same session again
        + "</HealthData>\n")


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    db = tmp_path / "health.duckdb"
    exp = tmp_path / "AppleHealthExport"
    exp.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "EXPORT_DIR", exp)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    # The HR anchors now read this file first. Point it at a path that does not
    # exist so the tests exercise the observed-max fallback deterministically,
    # whatever the recalibration job has written on the developer's machine.
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH",
                        tmp_path / "no_calibration.json")
    archive = exp / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("apple_health_export/export.xml", _build_xml())
    import_pipeline.import_archive(archive)
    from apple_health_mcp import server
    server._schema_ready = False        # re-init schema against this sandbox DB
    return tmp_path


def _plain_workout_id() -> str:
    """The ordinary run: the older of the two sessions."""
    con = storage.connect_readonly()
    try:
        return con.execute(
            "SELECT row_hash FROM workouts ORDER BY start_ts LIMIT 1"
        ).fetchone()[0]
    finally:
        con.close()


# --- HR anchors ---------------------------------------------------------------

def test_anchors_fall_back_to_the_observed_max(sandbox):
    """Too few runs for a functional HRmax -> the observed max, as before."""
    from apple_health_mcp import server
    d = server.get_workout_detail()
    assert d["summary"]["max_hr_used"] == 205
    assert isinstance(d["summary"]["max_hr_used"], int)   # unchanged JSON shape
    assert "estimated" in d["summary"]["max_hr_source"]
    assert server.get_hr_zones("2026-08-01", "2026-08-31")["max_hr_used"] == 205


def test_a_calibrated_anchor_beats_the_observed_max(sandbox, monkeypatch):
    """The single highest reading is a sensor artefact; the calibrated value wins
    -- and every tool must agree on it, not just one of them."""
    from apple_health_mcp import server
    ref = sandbox / "calibration_reference.json"
    ref.write_text(json.dumps({"hr_max": 209, "resting_hr": 48}))
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", ref)

    d = server.get_workout_detail(include=["zones"])
    z = server.get_hr_zones("2026-08-01", "2026-08-31")
    load = server.get_training_load("2026-08-01", "2026-08-31")
    assert d["summary"]["max_hr_used"] == 209
    assert z["max_hr_used"] == 209 and load["max_hr_used"] == 209
    assert "calibrated" in z["max_hr_source"]
    assert load["resting_hr_used"] == 48
    assert "calibrated" in load["resting_hr_source"]


# --- both zone models ---------------------------------------------------------

def test_workout_detail_reports_both_zone_models(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail()

    # The percentage model keeps its shape and its place: Z1..Z5, lo/hi bpm.
    pct = d["hr_zones"]
    assert [z["zone"] for z in pct["zones"]] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert pct["zones"][0]["lo_bpm"] == 0
    assert set(pct["zones"][0]) >= {"zone", "lo_bpm", "hi_bpm", "seconds",
                                    "minutes", "share"}

    # ...and at 205 bpm max it puts BOTH the 172 and the 182 samples in Z4,
    # which is exactly the question the training model exists to answer.
    by_pct = pct["minutes_by_zone"]
    assert by_pct["Z4"] == pytest.approx(19.5, abs=0.6)

    trn = d["training_zones"]
    assert trn["model"] == "training"
    assert [z["zone"] for z in trn["zones"]] == [
        "recovery", "easy", "grey", "threshold", "vo2max"]
    minutes = trn["minutes_by_zone"]
    # 20 samples at 172 bpm weighted 30 s each; 19 weighted at 182 (the last
    # sample has no successor). Threshold time is readable without any maths.
    assert minutes["grey"] == pytest.approx(10.0, abs=0.1)
    assert minutes["threshold"] == pytest.approx(9.5, abs=0.1)
    assert minutes["easy"] == 0.0 and minutes["vo2max"] == 0.0
    # Both models tile the same samples, so they must total the same time.
    assert trn["total_seconds"] == pytest.approx(pct["total_seconds"], abs=0.1)


def test_hr_zones_reports_both_models_over_a_period(sandbox):
    from apple_health_mcp import server
    z = server.get_hr_zones("2026-08-01", "2026-08-31", scope="all")

    # Legacy top-level shape is untouched.
    assert [x["zone"] for x in z["zones"]] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert z["total_minutes"] > 0
    assert abs(sum(x["share"] for x in z["zones"]) - 1.0) < 0.01

    trn = z["training_zones"]
    assert trn["model"] == "training"
    assert trn["total_seconds"] == pytest.approx(z["total_seconds"], abs=0.1)
    threshold = trn["minutes_by_zone"]["threshold"]
    z4 = z["minutes_by_zone"]["Z4"]
    # Z4 strictly contains the threshold band's time here plus grey-zone time:
    # the percentage model cannot separate them, the named bands can.
    assert threshold > 0
    assert z4 > threshold
    assert "threshold" in z["note"]


# --- repetitions --------------------------------------------------------------

def test_reps_are_decoded_labelled_and_ordered(sandbox):
    from apple_health_mcp import server
    reps = server.get_workout_detail()["reps"]
    assert reps["structured"] is True
    # The session is in the export TWICE under two source names; the steps must
    # not double.
    assert reps["count"] == len(_STEPS)
    assert [s["step_index"] for s in reps["steps"]] == list(range(len(_STEPS)))
    assert [s["key_path"] for s in reps["steps"]] == [s[0] for s in _STEPS]
    assert [s["role"] for s in reps["steps"]] == [
        "warmup", "work", "recovery", "work", "recovery", "cooldown"]
    assert reps["roles"] == {"warmup": 1, "work": 2, "recovery": 2, "cooldown": 1}

    work = [s for s in reps["steps"] if s["role"] == "work"]
    assert [s["distance_km"] for s in work] == [1.0, 1.0]
    assert [s["pace_min_per_km"] for s in work] == [4.0, 4.0]   # 4 min / km
    assert [s["training_zone"] for s in work] == ["threshold", "threshold"]
    assert [s["avg_hr"] for s in work] == [182.0, 184.0]
    assert work[0]["min_hr"] == 174.0 and work[0]["max_hr"] == 188.0
    assert reps["work_summary"]["count"] == 2
    assert reps["work_summary"]["avg_pace_min_per_km"] == 4.0

    rec = [s for s in reps["steps"] if s["role"] == "recovery"]
    assert [s["slot"] for s in rec] == [1, 1]
    assert rec[1]["successful"] is False
    assert all(s["pace_min_per_km"] > 4.0 for s in rec)


def test_an_ordinary_run_has_no_reps_section_content(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail(workout_id=_plain_workout_id())
    reps = d["reps"]
    assert reps["structured"] is False
    assert reps["count"] == 0 and reps["steps"] == []
    assert reps["work_summary"] is None
    assert "error" not in d


# --- decoupling ---------------------------------------------------------------

def test_decoupling_is_suppressed_on_an_interval_session(sandbox):
    from apple_health_mcp import server
    dec = server.get_workout_detail()["decoupling"]
    assert dec["applicable"] is False
    assert dec["drift_pct"] is None
    # Never False: a False here reads as a finding about aerobic fitness.
    assert dec["good_aerobic_control"] is None
    assert "Interval session" in dec["reason"]
    assert "2 work reps" in dec["reason"]


def test_decoupling_still_reported_on_a_steady_run(sandbox):
    from apple_health_mcp import server
    dec = server.get_workout_detail(workout_id=_plain_workout_id())["decoupling"]
    assert dec["applicable"] is True
    assert dec["basis"] == "hr_to_power"
    assert dec["drift_pct"] > 0            # HR rises against constant power
    assert dec["good_aerobic_control"] in (True, False)
    assert "reason" not in dec


# --- include ------------------------------------------------------------------

def test_include_defaults_to_the_full_response(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail()
    for key in ("summary", "series", "hr_zones", "training_zones",
                "decoupling", "splits", "reps", "note"):
        assert key in d, key
    assert d["series"]["points"]


def test_include_trims_the_payload(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail(include=["zones"])
    assert set(d) == {"summary", "hr_zones", "training_zones", "note"}
    assert "include=" in d["note"]

    r = server.get_workout_detail(include=["reps"])
    assert set(r) == {"summary", "reps", "note"}
    assert r["reps"]["count"] == len(_STEPS)

    s = server.get_workout_detail(include=["series", "splits"])
    assert set(s) == {"summary", "series", "splits", "note"}


def test_include_rejects_unknown_sections_over_the_tool(sandbox):
    from apple_health_mcp import server
    d = server.get_workout_detail(include=["zones", "everything"])
    assert "error" in d and "everything" in d["error"]


def test_decoupling_alone_still_knows_about_the_structure(sandbox):
    """`reps` is excluded but decoupling still has to see the intervals."""
    from apple_health_mcp import server
    d = server.get_workout_detail(include=["decoupling"])
    assert set(d) == {"summary", "decoupling", "note"}
    assert d["decoupling"]["applicable"] is False


# --- run_sql ------------------------------------------------------------------

def test_run_sql_advertises_and_reaches_workout_events(sandbox):
    from apple_health_mcp import server
    # Description introspection is best-effort: FastMCP's registry is private.
    getter = getattr(getattr(server.mcp, "_tool_manager", None), "get_tool", None)
    if callable(getter):
        tool = getter("run_sql")
        assert "workout_events" in (getattr(tool, "description", "") or "")
    res = server.run_sql(
        "SELECT count(*) AS n FROM workout_events WHERE event_kind = 'activity'")
    assert res["rows"][0]["n"] == 2 * len(_STEPS)    # both stored copies
