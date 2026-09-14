"""Tests for `zones.py` — HR-anchor resolution and the two zone models.

Nothing here uses real personal data. The DB-facing helpers take the read-only
query function as an argument, so most tests drive them with a stub `q`; the
handful that need to prove the generated SQL actually *runs* (DISTINCT ON, the
CASE mapping, the `zone` alias) go through a throwaway in-memory DuckDB.

Two regressions are pinned deliberately:

- the percentage model must stay bit-for-bit identical to `analytics`, because
  the dashboard and the existing tool tests depend on it;
- `resolve_hr_max` must return the calibrated 209, never the 210 artefact — and
  the p95 must be the interpolated `statistics.quantiles` one, since
  `sorted(xs)[int(n * 0.95)]` returns exactly the outlier it should exclude.
"""
from __future__ import annotations

import json

import duckdb
import pytest

from apple_health_mcp import analytics, config, zones

# --- stub query function ------------------------------------------------------


def make_q(run_maxima=(), observed_max=None, resting=None):
    """Stand-in for `server._q`, dispatching on the shape of the SQL."""
    def q(sql, params=()):
        if "DISTINCT ON" in sql:
            return [{"max_hr": v} for v in run_maxima]
        if "max(max_hr)" in sql:
            return [{"m": observed_max}]
        if "quantile_cont" in sql:
            return [{"m": resting}]
        raise AssertionError(f"unexpected SQL: {sql}")
    return q


@pytest.fixture()
def no_reference(tmp_path, monkeypatch):
    """Point the calibration reference at a path that does not exist."""
    p = tmp_path / "absent_calibration_reference.json"
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", p)
    return p


@pytest.fixture()
def reference(tmp_path, monkeypatch):
    """A written calibration reference, as the recalibration job leaves it."""
    p = tmp_path / "calibration_reference.json"
    p.write_text(json.dumps({"n": 400, "hrv_ln_sd": 0.21,
                             "hr_max": 209, "resting_hr": 64}))
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", p)
    return p


# --- where the calibration reference is looked up -----------------------------

def test_calibration_reference_follows_state_dir(tmp_path, monkeypatch):
    """Redirecting STATE_DIR is enough to sandbox the reference file.

    The file is written by a launchd job on a schedule. If the path were bound
    to STATE_DIR at import time, a test that redirected STATE_DIR would still
    read the developer's real data/calibration_reference.json — and would start
    passing or failing depending on whether that job had fired.
    """
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", None)
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    assert config.calibration_reference_path() == \
        tmp_path / config.CALIBRATION_REFERENCE_NAME

    # Nothing there yet -> no calibrated anchor, so the derivation is used.
    q = make_q(run_maxima=[190, 195, 200, 205, 210], observed_max=210)
    assert "p95" in zones.resolve_hr_max(q)[1]

    # Write the file the job would write; the same call now prefers it.
    (tmp_path / config.CALIBRATION_REFERENCE_NAME).write_text(
        json.dumps({"hr_max": 209, "resting_hr": 64}))
    assert zones.resolve_hr_max(q) == (209.0,
                                       "calibrated hr_max from "
                                       f"{config.CALIBRATION_REFERENCE_NAME}")
    assert zones.resolve_resting_hr(make_q(resting=56.4))[0] == 64.0


def test_an_explicit_reference_path_still_wins(tmp_path, monkeypatch):
    """Tests that patch the constant directly keep working."""
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"hr_max": 201}))
    monkeypatch.setattr(config, "STATE_DIR", tmp_path)
    monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", explicit)
    (tmp_path / config.CALIBRATION_REFERENCE_NAME).write_text(
        json.dumps({"hr_max": 209}))
    assert config.calibration_reference_path() == explicit
    assert zones.resolve_hr_max(make_q(observed_max=210))[0] == 201.0


# --- the shipped training-zone config ----------------------------------------

def test_shipped_zone_config_matches_the_training_model():
    """The default file is the athlete's documented bands, in order."""
    assert config.DEFAULT_ZONES_CONFIG_PATH.exists(), \
        f"missing shipped zone config at {config.DEFAULT_ZONES_CONFIG_PATH}"
    zs = zones.load_training_zones(config.DEFAULT_ZONES_CONFIG_PATH)
    assert [(z["name"], z["min_bpm"], z["max_bpm"]) for z in zs] == [
        ("recovery", None, 149),
        ("easy", 150, 165),
        ("grey", 166, 177),
        ("threshold", 178, 186),
        ("vo2max", 187, None),
    ]


def test_zone_config_is_read_from_the_configured_path(tmp_path, monkeypatch):
    p = tmp_path / "my_zones.json"
    p.write_text(json.dumps({"zones": [
        {"name": "low", "min_bpm": None, "max_bpm": 159},
        {"name": "high", "min_bpm": 160, "max_bpm": None},
    ]}))
    monkeypatch.setattr(config, "ZONES_CONFIG_PATH", p)
    assert [z["zone"] for z in zones.training_zone_bounds()] == ["low", "high"]
    # The bands really are configuration: 159/160 is not a hardcoded boundary.
    b = zones.training_zone_bounds()
    assert zones.classify(159, b) == "low"
    assert zones.classify(160, b) == "high"


def test_missing_zone_config_falls_back_to_the_builtin_default(tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(config, "ZONES_CONFIG_PATH", tmp_path / "gone.json")
    assert [z["zone"] for z in zones.training_zone_bounds()] == [
        "recovery", "easy", "grey", "threshold", "vo2max"]


@pytest.mark.parametrize("bands, needle", [
    # A gap would silently drop time out of every total.
    ([{"name": "a", "min_bpm": None, "max_bpm": 149},
      {"name": "b", "min_bpm": 160, "max_bpm": None}], "contiguous"),
    # An overlap would double-count it.
    ([{"name": "a", "min_bpm": None, "max_bpm": 150},
      {"name": "b", "min_bpm": 150, "max_bpm": None}], "contiguous"),
    # Only the outer bands may be open-ended.
    ([{"name": "a", "min_bpm": None, "max_bpm": 149},
      {"name": "b", "min_bpm": 150, "max_bpm": 200}], "null max_bpm"),
    # Zone names reach SQL, so they must be identifier-shaped.
    ([{"name": "a'; DROP TABLE workouts; --", "min_bpm": None, "max_bpm": 149},
      {"name": "b", "min_bpm": 150, "max_bpm": None}], "must match"),
    # Names key every total, so a duplicate would silently merge two bands.
    ([{"name": "a", "min_bpm": None, "max_bpm": 149},
      {"name": "a", "min_bpm": 150, "max_bpm": None}], "duplicate"),
])
def test_invalid_zone_config_raises(tmp_path, bands, needle):
    p = tmp_path / "bad.json"
    p.write_text(json.dumps({"zones": bands}))
    with pytest.raises(ValueError, match=needle):
        zones.load_training_zones(p)


# --- percentage model: unchanged ---------------------------------------------

def test_percent_bounds_match_analytics():
    """The %-of-max model is a drop-in: same numbers as the old helper."""
    for hr_max in (180, 190, 200, 209, 210):
        new = zones.percent_zone_bounds(hr_max)
        old = analytics.zone_bounds(hr_max)
        key = lambda zs: [(z["zone"], z["lo_pct"], z["hi_pct"],
                           z["lo_bpm"], z["hi_bpm"]) for z in zs]
        assert key(new) == key(old)


def test_percent_case_sql_matches_analytics():
    """Identical SQL text, so nothing about the shipped query changes."""
    for hr_max in (180, 200, 209):
        bounds = zones.percent_zone_bounds(hr_max)
        assert zones.zone_case_sql(bounds, "hr") == \
            analytics.zone_case_sql(hr_max, "hr")


def test_percent_zone_edges():
    b = zones.percent_zone_bounds(200)
    assert [z["zone"] for z in b] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert b[0]["lo_bpm"] == 0 and b[0]["hi_bpm"] == 120
    assert b[4]["lo_bpm"] == 180 and b[4]["hi_bpm"] == 200
    # Z1 catches everything below 60%, Z5 everything from 90% up: no gaps.
    assert zones.classify(0, b) == "Z1"
    assert zones.classify(119.9, b) == "Z1"
    assert zones.classify(120, b) == "Z2"
    assert zones.classify(179.9, b) == "Z4"
    assert zones.classify(180, b) == "Z5"
    assert zones.classify(500, b) == "Z5"
    assert zones.classify(None, b) is None


# --- training model -----------------------------------------------------------

def test_training_zone_edges():
    b = zones.training_zone_bounds()
    # min/max_bpm are inclusive, so 165 is still easy and 166 is already grey.
    assert zones.classify(0, b) == "recovery"
    assert zones.classify(149, b) == "recovery"
    assert zones.classify(150, b) == "easy"
    assert zones.classify(165, b) == "easy"
    # A binned average lands with its whole-bpm floor, not in the next band.
    assert zones.classify(165.4, b) == "easy"
    assert zones.classify(166, b) == "grey"
    assert zones.classify(177, b) == "grey"
    assert zones.classify(178, b) == "threshold"
    assert zones.classify(186, b) == "threshold"
    assert zones.classify(187, b) == "vo2max"
    assert zones.classify(204, b) == "vo2max"
    # Open-ended edges are reported as 0 / None.
    assert b[0]["lo_bpm"] == 0
    assert b[-1]["hi_bpm"] is None


def test_classify_does_not_depend_on_bound_order():
    """`classify` selects the band that contains the value, never the last one
    whose floor it clears — so a caller may pass bounds in any order."""
    import random

    for bounds in (zones.training_zone_bounds(),
                   zones.percent_zone_bounds(209)):
        ordered = list(bounds)
        shuffled = list(bounds)
        random.Random(0).shuffle(shuffled)
        assert [b["zone"] for b in shuffled] != [b["zone"] for b in ordered]
        for bpm in (0, 100, 149, 150, 165, 166, 177, 178, 186, 187, 209, 240):
            assert zones.classify(bpm, shuffled) == zones.classify(bpm, ordered)

    # Reversed order is the case the old positional scan got wrong.
    b = zones.training_zone_bounds()
    assert zones.classify(182, list(reversed(b))) == "threshold"
    assert zones.classify(140, list(reversed(b))) == "recovery"
    # A nonsense reading below every floor still lands in the lowest band.
    assert zones.classify(-5, list(reversed(b))) == "recovery"
    # Totals are order-independent too.
    samples = [(140, 60), (182, 60)]
    assert (zones.time_in_zones(samples, list(reversed(b)),
                                zones.TRAINING_MODEL)["minutes_by_zone"]
            == zones.time_in_zones(samples, b,
                                   zones.TRAINING_MODEL)["minutes_by_zone"])


def test_training_model_separates_grey_from_threshold():
    """The point of the whole exercise.

    At a 209 bpm HRmax the percentage model puts 170 and 182 in the same bucket
    (Z4), so "how long was he actually at threshold rather than in the grey
    zone" cannot be answered. The training model splits them.
    """
    pct = zones.percent_zone_bounds(209)
    trn = zones.training_zone_bounds()
    assert zones.classify(170, pct) == zones.classify(182, pct) == "Z4"
    assert zones.classify(170, trn) == "grey"
    assert zones.classify(182, trn) == "threshold"

    samples = [(170, 600), (182, 900)]
    lumped = zones.time_in_zones(samples, pct, zones.PERCENT_MODEL)
    split = zones.time_in_zones(samples, trn, zones.TRAINING_MODEL)
    assert lumped["minutes_by_zone"]["Z4"] == 25.0
    assert split["minutes_by_zone"]["grey"] == 10.0
    assert split["minutes_by_zone"]["threshold"] == 15.0
    # Both models account for exactly the same total time.
    assert lumped["total_seconds"] == split["total_seconds"] == 1500.0


def test_both_models_are_available_at_once():
    b_pct = zones.zone_bounds(zones.PERCENT_MODEL, hr_max=209)
    b_trn = zones.zone_bounds(zones.TRAINING_MODEL)
    assert [z["zone"] for z in b_pct] == ["Z1", "Z2", "Z3", "Z4", "Z5"]
    assert [z["zone"] for z in b_trn][2] == "grey"
    with pytest.raises(ValueError):
        zones.zone_bounds(zones.PERCENT_MODEL)          # hr_max required
    with pytest.raises(ValueError):
        zones.zone_bounds("nonsense")


# --- time in zone -------------------------------------------------------------

def test_time_in_zones_shares_and_null_handling():
    b = zones.training_zone_bounds()
    out = zones.time_in_zones(
        [(140, 60), (155, 60), (170, 120), (180, 300), (195, 60),
         (None, 999), (150, None)], b, zones.TRAINING_MODEL)
    assert out["model"] == "training"
    # The null-HR sample contributes nothing; the null duration counts as zero.
    assert out["total_seconds"] == 600.0
    assert out["total_minutes"] == 10.0
    assert out["minutes_by_zone"] == {"recovery": 1.0, "easy": 1.0, "grey": 2.0,
                                      "threshold": 5.0, "vo2max": 1.0}
    assert abs(sum(z["share"] for z in out["zones"]) - 1.0) < 1e-6
    # Every configured zone is reported even when it holds no time.
    empty = zones.time_in_zones([], b, zones.TRAINING_MODEL)
    assert [z["zone"] for z in empty["zones"]] == [z["zone"] for z in b]
    assert empty["total_seconds"] == 0.0
    assert all(z["share"] == 0.0 for z in empty["zones"])


def test_series_samples_weights_each_bin():
    points = [{"hr": 180.2}, {"hr": None}, {"hr": 150.0}]
    assert zones.series_samples(points, 30) == [(180.2, 30.0), (150.0, 30.0)]
    out = zones.time_in_zones(zones.series_samples(points, 30),
                              zones.training_zone_bounds(),
                              zones.TRAINING_MODEL)
    assert out["total_seconds"] == 60.0
    assert out["minutes_by_zone"]["threshold"] == 0.5


# --- HR anchors ---------------------------------------------------------------

def test_resolve_hr_max_prefers_the_calibrated_value(reference):
    q = make_q(run_maxima=[190, 195, 200, 205, 210], observed_max=210)
    value, source = zones.resolve_hr_max(q)
    assert value == 209.0
    assert "calibrat" in source


def test_resolve_hr_max_ignores_the_single_artefact(no_reference):
    """Without a stored calibration, the p95 of per-run maxima still excludes
    the 210 spike — `sorted(xs)[int(n * 0.95)]` would return it."""
    q = make_q(run_maxima=[190, 195, 200, 205, 210], observed_max=210)
    value, source = zones.resolve_hr_max(q)
    assert value == 209.0
    assert "p95" in source


def test_resolve_hr_max_falls_back_to_the_observed_max(no_reference):
    # Too few runs for a meaningful percentile -> the raw observed maximum.
    q = make_q(run_maxima=[205, 210], observed_max=210)
    assert zones.resolve_hr_max(q) == (210.0, "estimated from workouts.max_hr")
    # ...as does an explicit opt-out of the derivation.
    q2 = make_q(run_maxima=[190, 195, 200, 205, 210], observed_max=210)
    assert zones.resolve_hr_max(q2, derive_functional=False) == \
        (210.0, "estimated from workouts.max_hr")


def test_resolve_hr_max_overrides_and_empty_db(no_reference):
    assert zones.resolve_hr_max(make_q(), 205) == (205.0, "provided")
    value, source = zones.resolve_hr_max(make_q(observed_max=None))
    assert value == zones.FALLBACK_HR_MAX
    assert "default" in source


def test_a_corrupt_or_implausible_reference_is_ignored(tmp_path, monkeypatch):
    q = make_q(run_maxima=[190, 195, 200, 205, 210], observed_max=210)
    for payload in ("{ not json", json.dumps({"hr_max": 9999}),
                    json.dumps({"hr_max": "209"}), json.dumps([1, 2, 3])):
        p = tmp_path / "ref.json"
        p.write_text(payload)
        monkeypatch.setattr(config, "CALIBRATION_REFERENCE_PATH", p)
        value, source = zones.resolve_hr_max(q)
        assert (value, "p95" in source) == (209.0, True), payload


def test_resolve_resting_hr(no_reference):
    q = make_q(resting=56.4)
    assert zones.resolve_resting_hr(q) == \
        (56.0, "p10 of resting_heart_rate (trailing 90d)")
    assert zones.resolve_resting_hr(q, 64) == (64.0, "provided")
    value, source = zones.resolve_resting_hr(make_q(resting=None))
    assert value == zones.FALLBACK_RESTING_HR
    assert "default" in source


def test_resolve_resting_hr_prefers_the_calibrated_value(reference):
    value, source = zones.resolve_resting_hr(make_q(resting=56.4))
    assert value == 64.0
    assert "calibrat" in source


# --- the generated SQL actually runs -----------------------------------------

@pytest.fixture()
def duck():
    """A throwaway in-memory DB with just the columns these helpers touch.

    The point is to prove the generated SQL parses and runs (DISTINCT ON, the
    CASE mapping, the `zone` alias) — column aliases are a known trap in this
    project, so the SQL is exercised rather than string-matched.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE workouts (type VARCHAR, start_ts TIMESTAMP, "
                "max_hr DOUBLE)")
    rows = []
    for i, mx in enumerate([190, 195, 200, 205, 210]):
        # Each run is exported 2-3 times: same (type, start minute), different
        # seconds. Identity is the start MINUTE, so these are one run.
        for sec in (0, 17, 43):
            rows.append(f"('running', '2026-08-{10 + i:02d} 06:00:{sec:02d}', "
                        f"{mx}.0)")
    rows.append("('walking', '2026-08-20 12:00:00', 230.0)")   # not a run
    rows.append("('running', '2026-08-21 06:00:00', NULL)")    # no HR recorded
    con.execute("INSERT INTO workouts VALUES " + ", ".join(rows))

    con.execute("CREATE TABLE records_dedup (type VARCHAR, "
                "start_ts TIMESTAMP, value DOUBLE)")
    # One sample every 30 s: 140, 160, 170, 182, 190, 190 bpm.
    hrs = [140, 160, 170, 182, 190, 190]
    con.execute("INSERT INTO records_dedup VALUES " + ", ".join(
        f"('heart_rate', '2026-08-20 06:{i // 2:02d}:{(i % 2) * 30:02d}', "
        f"{float(v)})" for i, v in enumerate(hrs)))

    def q(sql, params=()):
        cur = con.execute(sql, list(params))
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]
    yield q
    con.close()


def test_per_run_maxima_dedupes_repeat_exports(duck):
    # 15 workout rows + a walk + an HR-less run -> 5 real runs.
    assert zones.per_run_maxima(duck) == [190.0, 195.0, 200.0, 205.0, 210.0]


def test_resolve_hr_max_against_a_real_database(duck, no_reference):
    value, source = zones.resolve_hr_max(duck)
    assert value == 209.0                      # not the 210 artefact
    assert source == ("functional HRmax (p95 of 5 per-run maxima, since "
                      f"{zones.FUNCTIONAL_HR_MAX_SINCE})")


def test_zone_time_runs_for_both_models(duck):
    where = "type = 'heart_rate'"
    # Samples are 30 s apart; the last one has no successor, so it weighs 0.
    trn = zones.zone_time(duck, where, (), zones.training_zone_bounds(),
                          zones.TRAINING_MODEL)
    assert trn["total_seconds"] == 150.0
    assert trn["minutes_by_zone"] == {"recovery": 0.5, "easy": 0.5, "grey": 0.5,
                                      "threshold": 0.5, "vo2max": 0.5}

    pct = zones.zone_time(duck, where, (), zones.percent_zone_bounds(209),
                          zones.PERCENT_MODEL)
    assert pct["total_seconds"] == 150.0
    # The percentage model lumps the grey-zone 170 and the threshold 182 into Z4.
    assert pct["minutes_by_zone"]["Z4"] == 1.0
    assert [z["zone"] for z in pct["zones"]] == ["Z1", "Z2", "Z3", "Z4", "Z5"]


def test_zone_time_matches_the_legacy_helper(duck):
    """`zones.zone_time` on the percentage model == `analytics.zone_time`."""
    where = "type = 'heart_rate'"
    new = zones.zone_time(duck, where, (), zones.percent_zone_bounds(209),
                          zones.PERCENT_MODEL)
    old = analytics.zone_time(duck, where, (), 209)
    assert new["total_seconds"] == old["total_seconds"]
    assert new["zones"] == old["zones"]
