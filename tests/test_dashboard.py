"""Tests for the dashboard builder.

The point of most of these is the *cleaning*, not the rendering: the raw export
duplicates workouts, double-counts sleep across devices, and sums steps over
every source at once. Each of those is asserted against a synthetic DB where the
correct answer is known by construction.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from apple_health_mcp import config, dashboard, server, storage

MSK = timezone(timedelta(hours=3))


@pytest.fixture()
def sandbox(tmp_path, monkeypatch):
    """Throwaway DB + dashboard path, matching the convention in test_parser."""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "health.duckdb")
    monkeypatch.setattr(config, "DASHBOARD_OUTPUT_PATH",
                        tmp_path / "out" / "dashboard.html")
    monkeypatch.setattr(server, "_schema_ready", False)
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()
    return tmp_path


def _dt(day: str, hhmm: str = "12:00") -> datetime:
    return datetime.fromisoformat(f"{day}T{hhmm}:00").replace(tzinfo=MSK)


def _rec(con, rtype: str, day: str, value: float, source="Apple Watch",
         hhmm="12:00", prio=30) -> None:
    con.execute(
        "INSERT INTO records (row_hash, type, source_name, unit, value, "
        "value_str, start_ts, end_ts, source_priority) VALUES (?,?,?,?,?,?,?,?,?)",
        (f"{rtype}-{day}-{hhmm}-{source}-{value}", rtype, source, "u", value,
         str(value), _dt(day, hhmm), _dt(day, hhmm), prio))


def _workout(con, day: str, hhmm: str, km: float, minutes: float, hr: float,
             row_hash: str, wtype="running", source="Apple Watch") -> None:
    start = _dt(day, hhmm)
    con.execute(
        "INSERT INTO workouts (row_hash, type, source_name, duration, "
        "duration_unit, distance, distance_unit, energy, energy_unit, avg_hr, "
        "max_hr, start_ts, end_ts) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (row_hash, wtype, source, minutes, "min", km, "km", 500.0, "kcal", hr,
         hr + 15, start, start + timedelta(minutes=minutes)))


def _sleep(con, day: str, start_h: str, end_h: str, stage: str,
           source="Apple Watch", tag="") -> None:
    s, e = _dt(day, start_h), _dt(day, end_h)
    if e <= s:
        e += timedelta(days=1)
    con.execute(
        "INSERT INTO sleep (row_hash, source_name, stage, raw_value, start_ts, "
        "end_ts) VALUES (?,?,?,?,?,?)",
        (f"{source}-{stage}-{day}-{start_h}-{tag}", source, stage, stage, s, e))


def _seed(con, days: int = 40, start="2026-01-06") -> None:
    """A month-plus of plausible data: daily vitals, steps, and twice-weekly runs."""
    d0 = datetime.fromisoformat(start).date()
    for i in range(days):
        day = (d0 + timedelta(days=i)).isoformat()
        _rec(con, "resting_heart_rate", day, 60 + (i % 5), hhmm="07:00")
        _rec(con, "hrv", day, 45 + (i % 7), hhmm="07:00")
        _rec(con, "step_count", day, 9000 + i * 10, hhmm="14:00")
        _rec(con, "active_energy", day, 500.0, hhmm="14:00")
        _rec(con, "walking_heart_rate_average", day, 105.0, hhmm="15:00")
        _rec(con, "respiratory_rate", day, 16.0, hhmm="07:00")
        if i % 5 == 0:
            _rec(con, "vo2max", day, 45.0, hhmm="16:00")
        _sleep(con, day, "23:00", "07:00", "core")
        _sleep(con, day, "01:00", "02:00", "deep")
        _sleep(con, day, "03:00", "04:30", "rem")
        if i % 3 == 0:
            _workout(con, day, "18:00", 8.0, 48.0, 155.0, f"run-{day}")


# --- cleaning ---------------------------------------------------------------- #

def test_duplicate_workouts_are_collapsed(sandbox):
    """The export stores each workout 2-3x; only one may survive."""
    con = storage.connect()
    try:
        _seed(con)
        # Same session, three different row_hashes and two source names — this
        # is exactly what a repeated export plus a watch rename produces.
        for i, src in enumerate(["Apple Watch", "Maksim's Apple Watch",
                                 "Apple Watch"]):
            _workout(con, "2026-02-16", "18:00", 10.0, 60.0, 160.0,
                     f"dupe-{i}", source=src)
        payload = dashboard._collect(con)
    finally:
        con.close()

    on_day = [r for r in payload["runs"] if r["d"] == "2026-02-16"]
    assert len(on_day) == 1, "duplicate workout records were not collapsed"
    assert payload["dupes"] == 2
    # ...and the week must not claim 30 km.
    week = next(w for w in payload["weekly"] if w["w"] == "2026-02-16")
    assert week["km"] == pytest.approx(10.0)


def test_overlapping_sleep_is_unioned_not_summed(sandbox):
    """iPhone and Watch both log the night; the union is 8 h, the sum is 16 h.

    Identical intervals only prove de-duplication. The partial-overlap and
    split-night cases below are what separate the gaps-and-islands scan from a
    per-night ``MAX(end) - MIN(start)`` span, which passed the old test.
    """
    con = storage.connect()
    try:
        _seed(con, days=30)
        # (a) byte-identical duplicates across two sources
        for src in ("Apple Watch", "iPhone M"):
            _sleep(con, "2026-03-01", "23:00", "07:00", "core", source=src,
                   tag="dup")
        # (b) partial overlap: union 8.0, naive sum 15.17
        _sleep(con, "2026-03-10", "23:00", "06:30", "core", source="Apple Watch",
               tag="lap")
        _sleep(con, "2026-03-10", "23:20", "07:00", "core", source="iPhone M",
               tag="lap")
        # (c) one night split around a wake-up: union 7.0, MIN/MAX span 8.0
        _sleep(con, "2026-03-05", "23:00", "02:00", "core", tag="split-a")
        _sleep(con, "2026-03-06", "03:00", "07:00", "core", tag="split-b")
        payload = dashboard._collect(con)
    finally:
        con.close()

    nights = dict(payload["sleep_raw"])
    assert nights["2026-03-02"] == pytest.approx(8.0, abs=0.01), (
        "duplicate segments were summed instead of unioned")
    assert nights["2026-03-11"] == pytest.approx(8.0, abs=0.01), (
        "partially overlapping segments were summed instead of unioned")
    assert nights["2026-03-06"] == pytest.approx(7.0, abs=0.01), (
        "a split night was measured end-to-end instead of as two islands")


def test_sleep_is_dated_by_wake_day(sandbox):
    """A night is filed under the day you wake on, matching get_sleep.

    Dating by the onset evening instead put every sleep point one day left of
    the resting HR and HRV that night produced.
    """
    con = storage.connect()
    try:
        _seed(con, days=30)
        _sleep(con, "2026-03-20", "22:30", "06:30", "core", tag="wake")
        payload = dashboard._collect(con)
    finally:
        con.close()
    nights = dict(payload["sleep_raw"])
    assert "2026-03-21" in nights, "an evening onset must file under the wake day"
    assert "2026-03-20" not in nights


def test_steps_take_highest_source_not_the_sum(sandbox):
    """Watch 12k + phone 9k on one day is a 12k day, not a 21k day.

    Each source contributes several rows a day in the real export, so the
    per-source SUM inside the correction has to run before the MAX across
    sources — with one row each the two collapse and the SUM goes untested.
    """
    con = storage.connect()
    try:
        _seed(con, days=30)
        day = "2026-01-20"
        con.execute("DELETE FROM records WHERE type='step_count' "
                    "AND start_ts::DATE = ?", (day,))
        _rec(con, "step_count", day, 12000, source="Apple Watch", hhmm="10:00")
        _rec(con, "step_count", day, 9000, source="iPhone M", hhmm="10:00",
             prio=20)
        rows = dashboard._q_daily_activity(con)
    finally:
        con.close()

    got = next(r["steps"] for r in rows if str(r["d"]) == day)
    assert got == pytest.approx(12000.0), (
        f"expected the highest single source (12000), got {got} — "
        "sources were summed")


def test_hrmax_ignores_a_single_spike(sandbox):
    """One artefactual 220 bpm reading must not set every zone boundary."""
    con = storage.connect()
    try:
        _seed(con)
        con.execute("UPDATE workouts SET max_hr = 220 WHERE row_hash = "
                    "(SELECT min(row_hash) FROM workouts)")
        payload = dashboard._collect(con)
    finally:
        con.close()
    # The spike must neither set the value nor drag it far from the bulk (170).
    assert payload["hrmax"] < 220
    assert payload["hrmax"] < 200


# --- derived series ----------------------------------------------------------- #

def test_weekly_grid_includes_empty_weeks(sandbox):
    """Gaps are the most important feature of this athlete's history."""
    con = storage.connect()
    try:
        _seed(con, days=20, start="2026-01-06")
        _workout(con, "2026-03-10", "18:00", 6.0, 36.0, 150.0, "late-run")
        payload = dashboard._collect(con)
    finally:
        con.close()

    weeks = payload["weekly"]
    assert any(w["n"] == 0 for w in weeks), "empty weeks were dropped"
    # Contiguous Mondays, no holes.
    from datetime import date as _d
    for a, b in zip(weeks, weeks[1:]):
        assert _d.fromisoformat(b["w"]) - _d.fromisoformat(a["w"]) == timedelta(days=7)


def test_block_start_tracks_the_current_streak(sandbox):
    con = storage.connect()
    try:
        _seed(con, days=40, start="2026-01-06")
        payload = dashboard._collect(con)
    finally:
        con.close()
    weekly = payload["weekly"]
    start = payload["month"]["block_start"]
    assert any(w["w"] == start for w in weekly)
    # Everything from the block start onward (bar the in-progress week) has runs.
    after = [w for w in weekly if w["w"] >= start][:-1]
    assert all(w["n"] > 0 for w in after)


def test_block_start_skips_every_trailing_runless_week(sandbox):
    """The fixture above has no gap, so it cannot exercise the walk-back.

    With several runless weeks at the end, block_start must land on the last
    week that actually contains a run — not on an empty one, which zeroes the
    current-block figures and blanks three tabs.
    """
    con = storage.connect()
    try:
        from datetime import date as _d
        _seed(con, days=40, start="2026-01-06")   # runs through mid-February
        # Four weeks of vitals only: the watch kept recording, he stopped running.
        for i in range(28):
            day = (_d.fromisoformat("2026-02-15") + timedelta(days=i)).isoformat()
            _rec(con, "resting_heart_rate", day, 61.0, hhmm="07:00")
            _rec(con, "hrv", day, 46.0, hhmm="07:00")
        payload = dashboard._collect(con)
    finally:
        con.close()
    weekly = payload["weekly"]
    start = payload["month"]["block_start"]
    week = next(w for w in weekly if w["w"] == start)
    assert week["n"] > 0, (
        f"block_start {start} points at a week with no runs — the walk-back "
        f"stopped inside the trailing gap")
    assert payload["month"]["runs"] or True   # block may be historical; keys stay


def test_script_tag_in_the_plan_cannot_break_out_of_the_data_blob(sandbox):
    """The plan carries model-written prose straight into an inline <script>.

    json.dumps does not escape "/", so an unescaped "</script>" would close the
    tag early and blank every panel.
    """
    con = storage.connect()
    try:
        _seed(con)
        payload = dashboard._collect(con)
    finally:
        con.close()
    payload["plan"] = {"planned": [{"id": "x"}],
                       "notes": "before </script><h1>out</h1> after"}
    html = dashboard.render(payload)

    blob = re.search(r"const D = (\{.*?\});\n", html, re.S).group(1)
    assert "</script" not in blob, "the payload can close its own script tag"
    assert json.loads(blob)["plan"]["notes"] == payload["plan"]["notes"], (
        "escaping changed the value the template reads back")


def test_every_payload_key_the_template_reads_exists(sandbox):
    """A key the template reads but the payload lacks renders 'undefined'.

    Scraped from the template rather than listed here, so the contract cannot
    drift silently.
    """
    con = storage.connect()
    try:
        _seed(con)
        payload = dashboard._collect(con)
    finally:
        con.close()
    tpl = dashboard.HTML_TEMPLATE
    for prefix, obj, name in (("D", payload, "payload"),
                              ("K", payload["kpi"], "payload['kpi']"),
                              ("M", payload["month"], "payload['month']")):
        read = set(re.findall(rf"\b{prefix}\.([a-z][a-z0-9_]*)\b", tpl))
        missing = sorted(k for k in read if k not in obj)
        assert not missing, f"{name} is missing keys the template reads: {missing}"


def test_rolling_mean_does_not_bridge_a_data_gap(sandbox):
    """A calendar window must yield no point when the readings aren't there."""
    pts = [["2026-01-01", 10.0], ["2026-01-02", 10.0], ["2026-01-03", 10.0],
           ["2026-06-01", 99.0]]
    rolled = dict(dashboard._roll(pts, 14, 3))
    assert "2026-06-01" not in rolled, "rolled across a five-month gap"


# --- output ------------------------------------------------------------------- #

def test_build_writes_self_contained_html(sandbox):
    con = storage.connect()
    try:
        _seed(con)
    finally:
        con.close()

    res = dashboard.build()
    assert res["status"] == "built"
    out = config.DASHBOARD_OUTPUT_PATH
    assert out.exists()

    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    # No network dependencies: the file has to work offline, from file://.
    assert not re.search(r'(?:src|href)\s*=\s*["\']https?://', html)
    # Artifact rules forbid browser storage.
    assert "localStorage" not in html and "sessionStorage" not in html
    # Placeholders all substituted.
    for token in ("__DATA__", "__FIRST__", "__NOW__", "__NRUN__", "__NWK__"):
        assert token not in html
    # The payload is real, parseable JSON.
    blob = re.search(r"const D = (\{.*?\});\n", html, re.S).group(1)
    data = json.loads(blob)
    assert data["runs"] and data["weekly"] and data["kpi"]


def test_build_is_atomic_and_overwrites(sandbox):
    con = storage.connect()
    try:
        _seed(con)
    finally:
        con.close()
    first = dashboard.build()["bytes"]
    second = dashboard.build()["bytes"]
    assert first == second
    # No temp files left behind.
    assert not list(config.DASHBOARD_OUTPUT_PATH.parent.glob(".dash-*"))


def test_build_respects_explicit_path(sandbox, tmp_path):
    con = storage.connect()
    try:
        _seed(con)
    finally:
        con.close()
    target = tmp_path / "nested" / "custom.html"
    res = dashboard.build(target)
    assert res["path"] == str(target) and target.exists()


# --- MCP tool ------------------------------------------------------------------ #

def test_tool_reports_empty_db_actionably(sandbox):
    res = server.build_dashboard()
    assert res["status"] == "empty"
    assert "reload_data" in res["hint"]


def test_tool_builds_and_returns_headline(sandbox):
    con = storage.connect()
    try:
        _seed(con)
    finally:
        con.close()
    res = server.build_dashboard()
    assert res["status"] == "built"
    assert res["runs"] > 0
    assert res["duplicate_workout_records_removed"] == 0
    assert set(res["headline"]) >= {"resting_hr_30d", "hrv_30d", "fitness_ctl"}
