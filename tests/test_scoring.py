"""Tests for the recovery/readiness scoring layer.

Pure math (`apple_health_mcp.scoring`) is tested directly. The two MCP tools
(`get_recovery`, `get_readiness`) run against a synthetic export that carries
nightly sleep stages plus night-window HRV / resting HR / respiratory rate /
wrist temperature. No real personal data is used.

Timestamps are written with a +0500 offset so the local wall-clock equals the
written time on the +05 test machine; all of a night's samples sit in the early
morning (02:00-07:00 local), well clear of the 18:00 sleep-day boundary, so each
night maps cleanly to one wake-day regardless of small timezone shifts.
"""
from __future__ import annotations

import zipfile
from datetime import datetime, timedelta

import pytest

from apple_health_mcp import config, import_pipeline, scoring

# --- pure math ----------------------------------------------------------------


def test_baseline_edge_cases():
    assert scoring.baseline([]) == (None, 0.0)
    assert scoring.baseline([42.0]) == (42.0, 0.0)      # single value -> no spread
    assert scoring.baseline([None, 5.0, None]) == (5.0, 0.0)
    mean, sd = scoring.baseline([2.0, 4.0, 6.0])
    assert mean == 4.0 and abs(sd - 2.0) < 1e-9         # sample sd of 2,4,6 == 2


def test_zscore_edge_cases():
    assert scoring.zscore(10, None, 2) is None          # no baseline
    assert scoring.zscore(10, 5, 0) is None             # zero spread
    assert scoring.zscore(None, 5, 2) is None           # no today value
    assert scoring.zscore(9, 5, 2) == 2.0


def test_subscore_baseline_point_and_monotonicity():
    # 50 == on baseline for the signed metrics; temp peaks at 100 on baseline.
    assert scoring.hrv_score(0) == 50.0
    assert scoring.rhr_score(0) == 50.0
    assert scoring.resp_score(0) == 50.0
    assert scoring.temp_score(0) == 100.0
    # HRV up is good, RHR/resp up is bad.
    assert scoring.hrv_score(1) > 50 > scoring.hrv_score(-1)
    assert scoring.rhr_score(1) < 50 < scoring.rhr_score(-1)
    assert scoring.resp_score(1) < 50 < scoring.resp_score(-1)
    # Temperature falls off symmetrically in either direction.
    assert scoring.temp_score(1) == scoring.temp_score(-1) < 100
    # None input -> None sub-score (omitted downstream).
    assert scoring.hrv_score(None) is None


def test_subscore_clamping():
    assert scoring.hrv_score(100) == 100.0              # clamps at 100
    assert scoring.hrv_score(-100) == 0.0               # clamps at 0
    assert scoring.rhr_score(100) == 0.0
    assert scoring.temp_score(100) == 0.0


def test_sleep_score_components_and_renormalization():
    # Perfect night: full duration, good deep+rem, no wakes, regular bedtime.
    full = scoring.sleep_score(8.0, need=8.0, deep_rem_frac=0.45,
                               awakenings=0, regularity=0.0)
    assert full == 100.0
    # Only duration present -> renormalizes to just that component.
    assert scoring.sleep_score(4.0, need=8.0) == 50.0
    assert scoring.sleep_score(None) is None


def test_recovery_weight_renormalization():
    # Only hrv + sleeping_hr present -> weights 0.40/0.20 renormalize to 2:1.
    out = scoring.recovery_score({"hrv": 90.0, "sleeping_hr": 60.0})
    assert out["score"] == pytest.approx((90 * 0.40 + 60 * 0.20) / 0.60, abs=0.1)
    weights = {c["metric"]: c["weight"] for c in out["contributors"]}
    assert weights["hrv"] == pytest.approx(2 / 3, abs=0.01)
    assert weights["sleeping_hr"] == pytest.approx(1 / 3, abs=0.01)
    # Nothing present -> null score.
    assert scoring.recovery_score({"hrv": None})["score"] is None


def test_band_boundaries():
    assert scoring.band(67) == "green"
    assert scoring.band(66) == "yellow"
    assert scoring.band(34) == "yellow"
    assert scoring.band(33) == "red"
    assert scoring.band(None) is None


def test_readiness_sweet_spot_no_penalty():
    out = scoring.readiness_score(70.0, acwr=1.0)
    assert out["penalty"] == 0.0
    assert out["score"] == 70.0
    # Low ACWR (undertraining) also incurs no load penalty.
    assert scoring.readiness_score(70.0, acwr=0.6)["penalty"] == 0.0


def test_readiness_penalty_above_1_5():
    out = scoring.readiness_score(70.0, acwr=1.8)
    assert out["penalty"] > 0
    assert out["score"] < 70.0
    assert "elevated" in out["recommendation"].lower()


# --- synthetic export for the tools ------------------------------------------


def _rec(rtype, unit, value, ts):
    return (f'<Record type="{rtype}" sourceName="Maksim\'s Apple Watch" '
            f'unit="{unit}" value="{value}" startDate="{ts}" endDate="{ts}" '
            f'creationDate="{ts}"/>')


def _sleep(stage_value, start, end):
    return ('<Record type="HKCategoryTypeIdentifierSleepAnalysis" '
            f'sourceName="Maksim\'s Apple Watch" value="{stage_value}" '
            f'startDate="{start}" endDate="{end}" creationDate="{start}"/>')


def _hr_samples(day: datetime, hr_value, start_h=2, end_h=6, end_m=27,
                step_min=3) -> list[str]:
    """Overnight heart_rate samples at `hr_value` every `step_min` minutes across
    [start_h:00, end_h:end_m]. With 3-min spacing a 5-min rolling window holds >= 2
    samples, so every window is valid and the sustained min equals `hr_value`."""
    rows = []
    t = day.replace(hour=start_h, minute=0)
    stop = day.replace(hour=end_h, minute=end_m)
    while t <= stop:
        ts = t.strftime("%Y-%m-%d %H:%M:%S +0500")
        rows.append(_rec("HKQuantityTypeIdentifierHeartRate", "count/min",
                         hr_value, ts))
        t += timedelta(minutes=step_min)
    return rows


def _night_rows(day: datetime, hrv, shr, resp, temp) -> list[str]:
    """One night: contiguous core/deep/rem sleep stages 02:00-06:30 local (4.5 h
    asleep), overnight heart_rate held at `shr` through those stages (so the
    stage-restricted sustained-minimum sleeping HR resolves to exactly `shr`), and
    HRV / resp / wrist-temp sampled inside the window (~03:30)."""
    def at(h, m=0):
        return day.replace(hour=h, minute=m).strftime("%Y-%m-%d %H:%M:%S +0500")

    rows = [
        _sleep("HKCategoryValueSleepAnalysisAsleepDeep", at(2), at(3, 30)),
        _sleep("HKCategoryValueSleepAnalysisAsleepCore", at(3, 30), at(5, 30)),
        _sleep("HKCategoryValueSleepAnalysisAsleepREM", at(5, 30), at(6, 30)),
        _rec("HKQuantityTypeIdentifierHeartRateVariabilitySDNN", "ms", hrv, at(3)),
        _rec("HKQuantityTypeIdentifierRespiratoryRate", "count/min", resp, at(3, 30)),
        _rec("HKQuantityTypeIdentifierAppleSleepingWristTemperature", "degC",
             temp, at(3, 30)),
    ]
    rows += _hr_samples(day, shr)
    return rows


def _build_recovery_xml(nights: int, last_good: bool = True) -> str:
    rows: list[str] = []
    start = datetime(2024, 5, 1, 0, 0, 0)
    for i in range(nights):
        day = start + timedelta(days=i)
        last = i == nights - 1
        # Baseline nights wobble around a steady mean (so sd > 0 -> z defined).
        hrv = 50 + (4 if i % 2 else -4)
        shr = 58 + (2 if i % 2 else -2)
        resp = 15.0 + (0.4 if i % 2 else -0.4)
        temp = 36.5 + (0.05 if i % 2 else -0.05)
        if last and last_good:
            hrv, shr, resp, temp = 72, 50, 14.0, 36.5   # strongly recovered
        rows += _night_rows(day, hrv, shr, resp, temp)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
        '<ExportDate value="2024-06-01 09:00:00 +0500"/>\n'
        + "\n".join(rows) + "\n</HealthData>\n"
    )


def _build_no_sleep_xml() -> str:
    row = _rec("HKQuantityTypeIdentifierHeartRate", "count/min", 60,
               "2024-05-01 12:00:00 +0500")
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
            '<ExportDate value="2024-06-01 09:00:00 +0500"/>\n' + row +
            "\n</HealthData>\n")


def _wrap(rows: list[str]) -> str:
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<HealthData locale="en_US">\n'
            '<ExportDate value="2024-06-01 09:00:00 +0500"/>\n'
            + "\n".join(rows) + "\n</HealthData>\n")


def _build_awake_exclusion_xml() -> str:
    """One night with an awake gap (04:30-04:45) that has NO core/deep/rem segment.
    Overnight HR is held at 55 across the asleep stages, but a single 35 bpm sample
    sits inside the awake gap — the stage restriction must exclude it, so sleeping
    HR stays ~55 (a raw wake-to-wake min would wrongly read 35)."""
    day = datetime(2024, 5, 20, 0, 0, 0)

    def at(h, m=0):
        return day.replace(hour=h, minute=m).strftime("%Y-%m-%d %H:%M:%S +0500")

    rows = [
        _sleep("HKCategoryValueSleepAnalysisAsleepDeep", at(2), at(3, 30)),
        _sleep("HKCategoryValueSleepAnalysisAsleepCore", at(3, 30), at(4, 30)),
        _sleep("HKCategoryValueSleepAnalysisAwake", at(4, 30), at(4, 45)),
        _sleep("HKCategoryValueSleepAnalysisAsleepREM", at(4, 45), at(6, 30)),
    ]
    rows += _hr_samples(day, 55)                       # continuous 02:00-06:27
    rows.append(_rec("HKQuantityTypeIdentifierHeartRate", "count/min", 35,
                     at(4, 37)))                       # lone low, inside awake gap
    return _wrap(rows)


def _build_gate_fail_xml() -> str:
    """One night with only ~1 h asleep (below SLEEPING_HR_MIN_ASLEEP_H) — it must
    fail the validity gate and yield a null sleeping HR that does not contribute."""
    day = datetime(2024, 5, 20, 0, 0, 0)

    def at(h, m=0):
        return day.replace(hour=h, minute=m).strftime("%Y-%m-%d %H:%M:%S +0500")

    rows = [_sleep("HKCategoryValueSleepAnalysisAsleepCore", at(2), at(3))]
    rows += _hr_samples(day, 55, start_h=2, end_h=2, end_m=57)
    return _wrap(rows)


def _build_fallback_xml() -> str:
    """One night staged only as generic 'asleep' (no core/deep/rem). Sleeping HR
    must fall back to the p5 of overnight HR, tagged p5_fallback."""
    day = datetime(2024, 5, 20, 0, 0, 0)

    def at(h, m=0):
        return day.replace(hour=h, minute=m).strftime("%Y-%m-%d %H:%M:%S +0500")

    rows = [_sleep("HKCategoryValueSleepAnalysisAsleepUnspecified", at(2), at(6, 30))]
    rows += _hr_samples(day, 56)
    return _wrap(rows)


def _install(tmp_path, monkeypatch, xml: str):
    db = tmp_path / "health.duckdb"
    exp = tmp_path / "AppleHealthExport"
    exp.mkdir()
    monkeypatch.setattr(config, "DB_PATH", db)
    monkeypatch.setattr(config, "EXPORT_DIR", exp)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "IMPORT_STATE_PATH", tmp_path / "import_state.json")
    monkeypatch.setattr(config, "LOG_DIR", tmp_path / "logs")
    archive = exp / "export.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("apple_health_export/export.xml", xml)
    import_pipeline.import_archive(archive)
    from apple_health_mcp import server
    server._schema_ready = False   # re-init schema against this sandbox DB
    return tmp_path


@pytest.fixture()
def recovery_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_recovery_xml(20))


@pytest.fixture()
def short_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_recovery_xml(8))


@pytest.fixture()
def no_sleep_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_no_sleep_xml())


@pytest.fixture()
def thin_baseline_sandbox(tmp_path, monkeypatch):
    # 10 nights -> 9 baseline samples per metric (< MIN_BASELINE_N of 10).
    return _install(tmp_path, monkeypatch, _build_recovery_xml(10, last_good=False))


@pytest.fixture()
def enough_baseline_sandbox(tmp_path, monkeypatch):
    # 11 nights -> 10 baseline samples per metric (== MIN_BASELINE_N).
    return _install(tmp_path, monkeypatch, _build_recovery_xml(11, last_good=False))


@pytest.fixture()
def awake_exclusion_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_awake_exclusion_xml())


@pytest.fixture()
def gate_fail_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_gate_fail_xml())


@pytest.fixture()
def fallback_sandbox(tmp_path, monkeypatch):
    return _install(tmp_path, monkeypatch, _build_fallback_xml())


# --- tools --------------------------------------------------------------------


def test_get_recovery_ok(recovery_sandbox):
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["state"] == "ok"
    assert r["history_days"] >= 14
    # A strongly-recovered final night (high HRV, low sleeping HR) -> green.
    assert r["score"] is not None and r["band"] == "green"

    # Every physiological sub-score plus sleep is present with baseline context.
    for m in ("hrv", "sleeping_hr", "resp", "temp"):
        assert r["subscores"][m] is not None
        assert r["metrics"][m]["today"] is not None
        assert r["metrics"][m]["baseline_mean"] is not None
        assert r["metrics"][m]["z"] is not None
    assert r["subscores"]["sleep"] is not None

    # HRV above baseline -> high sub-score; sleeping HR below baseline -> high too.
    assert r["metrics"]["hrv"]["z"] > 0 and r["subscores"]["hrv"] > 50
    assert r["metrics"]["sleeping_hr"]["z"] < 0 and r["subscores"]["sleeping_hr"] > 50

    # 19 baseline nights -> every metric is above MIN_BASELINE_N, so none is
    # flagged low_confidence and the top-level list is empty.
    for m in ("hrv", "sleeping_hr", "resp", "temp"):
        assert r["metrics"][m]["low_confidence"] is False
    assert r["low_confidence_metrics"] == []


def test_sleeping_hr_sustained_minimum(recovery_sandbox):
    """sleeping_hr is derived from overnight heart_rate inside core/deep/rem and
    resolves to the lowest sustained 5-min window. The synthetic night holds HR at
    a constant, so today == that constant, tagged stage_restricted with the night's
    asleep hours and valid-window count exposed."""
    from apple_health_mcp import server
    r = server.get_recovery()
    shr = r["metrics"]["sleeping_hr"]
    assert shr["source"] == "stage_restricted"
    assert shr["metric"] == "heart_rate"
    assert shr["today"] == pytest.approx(50.0, abs=0.5)   # last night held at 50
    assert shr["asleep_hours"] == pytest.approx(4.5, abs=0.2)
    assert shr["valid_windows"] >= server.SLEEPING_HR_MIN_VALID_WINDOWS
    assert shr["baseline_n"] > 0 and shr["z"] is not None


def test_get_recovery_calibrating(short_sandbox):
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["state"] == "calibrating"
    assert r["history_days"] < 14
    # Still returns a usable score while calibrating.
    assert r["score"] is not None
    assert "calibrating" in r["note"].lower()


def test_get_recovery_insufficient_data(no_sleep_sandbox):
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["state"] == "insufficient_data"
    assert r["score"] is None


def test_get_readiness_end_to_end(recovery_sandbox):
    from apple_health_mcp import server
    r = server.get_readiness()
    assert r["state"] == "ok"
    assert r["score"] is not None
    assert r["recovery_score"] == r["recovery"]["score"]
    # No workouts in this fixture -> no ACWR load, so readiness == recovery.
    assert r["load"]["penalty"] == 0.0
    assert r["score"] == r["recovery_score"]
    assert isinstance(r["recommendation"], str) and r["recommendation"]


def test_get_readiness_insufficient(no_sleep_sandbox):
    from apple_health_mcp import server
    r = server.get_readiness()
    assert r["state"] == "insufficient_data"
    assert r["score"] is None


def test_low_confidence_below_threshold(thin_baseline_sandbox):
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["metrics"]["hrv"]["baseline_n"] < server.MIN_BASELINE_N
    # Every scored metric on a thin baseline flips low_confidence true...
    for m in ("hrv", "sleeping_hr", "resp", "temp"):
        assert r["metrics"][m]["low_confidence"] is True
    # ...and is aggregated into the top-level list (order follows the metric map).
    assert set(r["low_confidence_metrics"]) == {"hrv", "sleeping_hr", "resp", "temp"}
    assert "provisional" in r["note"].lower()


def test_low_confidence_at_threshold(enough_baseline_sandbox):
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["metrics"]["hrv"]["baseline_n"] >= server.MIN_BASELINE_N
    for m in ("hrv", "sleeping_hr", "resp", "temp"):
        assert r["metrics"][m]["low_confidence"] is False
    assert r["low_confidence_metrics"] == []
    assert "provisional" not in r["note"].lower()


def test_low_confidence_independent_of_state(thin_baseline_sandbox):
    """low_confidence is a finer-grained flag: it does not alter the state field."""
    from apple_health_mcp import server
    r = server.get_recovery()
    assert r["state"] == "calibrating"          # 10 nights -> < 14 days history
    assert r["low_confidence_metrics"]          # yet still flags thin baselines


def test_readiness_surfaces_low_confidence(thin_baseline_sandbox):
    from apple_health_mcp import server
    r = server.get_readiness()
    assert set(r["low_confidence_metrics"]) == {"hrv", "sleeping_hr", "resp", "temp"}
    assert "provisional" in r["note"].lower()


def test_sleeping_hr_excludes_awake_samples(awake_exclusion_sandbox):
    """A lone 35 bpm reading inside an awake gap must not become the sleeping HR;
    the stage restriction keeps it ~55 (the asleep-stage level)."""
    from apple_health_mcp import server
    r = server.get_recovery("2024-05-20")
    shr = r["metrics"]["sleeping_hr"]
    assert shr["source"] == "stage_restricted"
    assert shr["today"] == pytest.approx(55.0, abs=1.0)   # NOT dragged to 35
    assert shr["today"] > 45


def test_sleeping_hr_gate_failure_returns_null(gate_fail_sandbox):
    """A night with < 3 h asleep fails the validity gate: sleeping HR is null and
    does not contribute to recovery (must not pollute the baseline)."""
    from apple_health_mcp import server
    r = server.get_recovery("2024-05-20")
    shr = r["metrics"]["sleeping_hr"]
    assert shr["today"] is None
    assert shr["subscore"] is None
    assert shr["asleep_hours"] is not None and shr["asleep_hours"] < 3   # visible
    # Omitted from the weighted mean entirely.
    assert "sleeping_hr" not in [c["metric"] for c in r["contributors"]]


def test_sleeping_hr_p5_fallback(fallback_sandbox):
    """With only generic 'asleep' staging, sleeping HR falls back to the p5 of
    overnight HR and is tagged p5_fallback."""
    from apple_health_mcp import server
    r = server.get_recovery("2024-05-20")
    shr = r["metrics"]["sleeping_hr"]
    assert shr["source"] == "p5_fallback"
    assert shr["today"] == pytest.approx(56.0, abs=1.0)
    assert shr["asleep_hours"] == pytest.approx(4.5, abs=0.3)
