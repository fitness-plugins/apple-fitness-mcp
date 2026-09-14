"""Python side of the cross-language parity harness.

Loads the committed `parity_fixture.json` and asserts `apple_health_mcp.scoring`
still reproduces every captured number. This guards the fixture from silently
drifting away from the Python reference; the Swift `ParityTests` loads the same
file and asserts the port matches it within 1e-6. If this test fails after a
scoring change, regenerate: `uv run python scripts/gen_parity_fixture.py`.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from apple_health_mcp import scoring

FIXTURE = Path(__file__).parent / "fixtures" / "parity_fixture.json"
EPS = 1e-6


@pytest.fixture(scope="module")
def fx() -> dict:
    return json.loads(FIXTURE.read_text())


def _close(a, b):
    if a is None or b is None:
        return a is None and b is None
    return abs(a - b) <= EPS


def test_baseline_stats(fx):
    for c in fx["baseline_stats"]:
        cfg = scoring.ScoringConfig(baseline_estimator=c["estimator"],
                                    baseline_ewma=c["ewma"],
                                    winsorize_enabled=c["winsor"])
        center, scale, n = scoring.baseline_stats(c["vals"], cfg)
        assert _close(center, c["center"]), c
        assert _close(scale, c["scale"]), c
        assert n == c["n"], c


def test_cv(fx):
    for c in fx["cv"]:
        assert _close(scoring.cv(c["vals"]), c["cv"]), c


def test_rolling_cv(fx):
    for c in fx["rolling_cv"]:
        got = scoring.rolling_cvs(c["vals"], c["window"])
        assert len(got) == len(c["cvs"]), c
        for a, b in zip(got, c["cvs"]):
            assert _close(a, b), c


def test_hrv_cv_score(fx):
    for c in fx["hrv_cv_score"]:
        assert _close(scoring.hrv_cv_score(c["cv_today"], c["baseline_cvs"]),
                      c["expected"]), c


def test_hrv_taper(fx):
    cfg = scoring.ScoringConfig(hrv_taper_enabled=True)
    for c in fx["hrv_taper"]:
        assert _close(scoring.hrv_score(c["z"], cfg), c["expected"]), c


def test_temp(fx):
    for c in fx["temp"]:
        cfg = scoring.ScoringConfig(temp_asymmetric=c["asymmetric"])
        assert _close(scoring.temp_score(c["z"], cfg, c["luteal"]),
                      c["expected"]), c


def test_sleep_need(fx):
    for c in fx["sleep_need"]:
        assert _close(scoring.personalized_sleep_need(c["baseline_hours"]),
                      c["need"]), c


def test_significance(fx):
    for c in fx["significance"]:
        swc, meaningful, sig = scoring.significance(c["z"], c["scale"])
        assert _close(swc, c["swc"]), c
        assert meaningful == c["meaningful"], c
        assert sig == c["significant"], c


def test_hrv_baseline_z(fx):
    for c in fx["hrv_baseline_z"]:
        cfg = scoring.ScoringConfig(hrv_log_transform=c["log"])
        mean, sd, z = scoring.hrv_baseline_z(c["today"], c["base"], cfg,
                                             recent_values=c.get("recent"))
        assert _close(mean, c["mean"]), c
        assert _close(sd, c["sd"]), c
        assert _close(z, c["z"]), c


def test_subscore(fx):
    fns = {"hrv": scoring.hrv_score, "rhr": scoring.rhr_score,
           "resp": scoring.resp_score, "temp": scoring.temp_score}
    for c in fx["subscore"]:
        assert _close(fns[c["kind"]](c["z"]), c["expected"]), c


def test_sleep(fx):
    for c in fx["sleep"]:
        got = scoring.sleep_score(c["hours"], c["need"], c["deep_rem"],
                                  c["awakenings"], c["regularity"])
        assert _close(got, c["expected"]), c


def test_recovery(fx):
    for c in fx["recovery"]:
        r = scoring.recovery_score(c["subscores"])
        assert _close(r["score"], c["score"]), c
        assert r["band"] == c["band"], c
        assert r["contributors"] == c["contributors"], c


def test_metric_confidence(fx):
    for c in fx["metric_confidence"]:
        assert _close(scoring.metric_confidence(c["n"]), c["conf"]), c


def test_recovery_confidence(fx):
    for c in fx["recovery_confidence"]:
        r = scoring.recovery_score(c["subscores"], confidences=c["confidences"])
        assert _close(r["score"], c["score"]), c
        assert r["band"] == c["band"], c
        assert _close(r["confidence"], c["confidence"]), c
        assert _close(r["ci"], c["ci"]), c
        assert r["contributors"] == c["contributors"], c


def test_load_penalty(fx):
    for c in fx["load_penalty"]:
        got = scoring.load_penalty(c["acwr"], c["acute_load"], c["recovery_time"])
        assert _close(got, c["expected"]), c


def test_acwr(fx):
    for c in fx["acwr"]:
        cfg = scoring.ScoringConfig(acwr_uncoupled=c["uncoupled"])
        assert _close(scoring.acwr_ratio(c["loads"], cfg), c["expected"]), c


def test_readiness(fx):
    for c in fx["readiness"]:
        r = scoring.readiness_score(c["recovery"], c["acwr"], c["acute_load"],
                                    c["recovery_time"])
        assert _close(r["score"], c["score"]), c
        assert r["band"] == c["band"], c
        assert _close(r["penalty"], c["penalty"]), c
        assert r["recommendation"] == c["recommendation"], c


def test_illness(fx):
    for c in fx["illness"]:
        r = scoring.illness_signals(c["zscores"])
        assert r["count"] == c["count"], c
        assert r["signals"] == c["signals"], c
        assert r["triggered"] == c["triggered"], c
        assert _close(r["pressure"], c["pressure"]), c


def test_readiness_illness(fx):
    for c in fx["readiness_illness"]:
        r = scoring.readiness_score(c["recovery"], c["acwr"],
                                    illness_zscores=c["zscores"])
        assert _close(r["score"], c["score"]), c
        assert r["band"] == c["band"], c
        assert r["recommendation"] == c["recommendation"], c
        assert bool(r["illness"] and r["illness"]["triggered"]) \
            == c["illness_triggered"], c
