"""Tests for the offline calibration scaffolding (Phases 4.2/4.3).

`calibrate.py` is out of the live scoring path — these exercise the fit and the
drift-check on the committed sample CSVs, and confirm `--check` exits non-zero
past tolerance.
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
FIX = ROOT / "tests" / "fixtures"


@pytest.fixture(scope="module")
def cal():
    spec = importlib.util.spec_from_file_location(
        "calibrate", ROOT / "scripts" / "calibrate.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # type: ignore[union-attr]
    return mod


def test_fit_suggests_weights_and_correlations(cal):
    res = cal.fit(FIX / "sample_subscores.csv", FIX / "sample_subjective.csv", lam=1.0)
    assert res["n_days"] >= 7
    # Every feature gets a suggested weight and a correlation.
    assert set(res["suggested_weights"]) == set(res["features"])
    assert set(res["correlations"]) == set(res["features"])
    # Suggested weights are a normalized distribution (rounded to 3 dp).
    assert abs(sum(res["suggested_weights"].values()) - 1.0) < 0.01
    # The synthetic labels were driven mainly by hrv + sleep, so those dominate.
    assert res["suggested_weights"]["hrv"] > 0.2
    assert res["suggested_weights"]["sleep"] > 0.15
    assert res["correlations"]["hrv"] is not None


def test_reference_stats(cal):
    vals = cal.load_hrv_history(FIX / "sample_hrv_history.csv")
    ref = cal.reference_stats(vals)
    assert ref["n"] == len(vals)
    assert ref["hrv_ln_sd"] > 0
    # SWC edge is half the ln-sd by construction (Phase 2.3); both rounded to 5 dp.
    assert abs(ref["hrv_swc_edge"] - 0.5 * ref["hrv_ln_sd"]) < 1e-4


def test_check_writes_reference_then_within_tolerance(cal, tmp_path):
    ref = tmp_path / "ref.json"
    # First run: no stored reference -> writes baseline, exit 0.
    assert cal.check(FIX / "sample_hrv_history.csv", ref, tol=0.10) == 0
    assert ref.exists()
    stored = json.loads(ref.read_text())
    assert "hrv_ln_sd" in stored
    # Same data again -> within tolerance, exit 0.
    assert cal.check(FIX / "sample_hrv_history.csv", ref, tol=0.10) == 0


def test_check_exits_nonzero_on_drift(cal, tmp_path):
    ref = tmp_path / "ref.json"
    cal.check(FIX / "sample_hrv_history.csv", ref, tol=0.10)          # seed
    # A drifted history (much larger spread) crosses the tolerance -> exit 1.
    assert cal.check(FIX / "sample_hrv_history_drifted.csv", ref, tol=0.10) == 1
