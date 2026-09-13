#!/usr/bin/env python3
"""Generate the cross-language parity fixture.

The Swift `ScoreEngine` is a 1:1 port of `apple_health_mcp.scoring`. To prove
they stay numerically identical we run a curated set of inputs through the Python
reference here, capture the outputs, and commit the result as JSON. Both a Python
test (`tests/test_parity.py`) and a Swift test (`ParityTests.swift`) load the
same fixture and assert their engine reproduces every number within 1e-6.

Regenerate after any change to the scoring math, then copy the fixture into the
Swift test bundle:

    uv run python scripts/gen_parity_fixture.py
    cp tests/fixtures/parity_fixture.json \\
       ../apple-fitness-ios/ios/ScoreEngine/Tests/ScoreEngineTests/Fixtures/

The Swift side reads the *same* default config, so the fixture bakes in the
Python `DEFAULT_CONFIG` scalars under "config"; a Swift test asserts
`ScoringConfig.default` matches them field-for-field (the sync contract).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from apple_health_mcp import scoring
from apple_health_mcp.scoring import DEFAULT_CONFIG as C


def _config_block() -> dict:
    """Scalar config fields the Swift `ScoringConfig.default` must match."""
    return {
        "k": C.k, "k_hrv": C.k_hrv,
        "band_green": C.band_green, "band_yellow": C.band_yellow,
        "temp_asymmetric": C.temp_asymmetric,
        "temp_warm_weight": C.temp_warm_weight,
        "temp_cool_weight": C.temp_cool_weight,
        "cycle_aware": C.cycle_aware,
        "cycle_temp_z_shift": C.cycle_temp_z_shift,
        "sleep_need_default_h": C.sleep_need_default_h,
        "sleep_target_deep_rem": C.sleep_target_deep_rem,
        "sleep_awakening_penalty": C.sleep_awakening_penalty,
        "sleep_regularity_zero_min": C.sleep_regularity_zero_min,
        "sleep_duration_curve": C.sleep_duration_curve,
        "sleep_under_penalty_per_h": C.sleep_under_penalty_per_h,
        "sleep_over_penalty_per_h": C.sleep_over_penalty_per_h,
        "sleep_need_min": C.sleep_need_min,
        "sleep_need_max": C.sleep_need_max,
        "sleep_need_min_samples": C.sleep_need_min_samples,
        "acwr_sweet_hi": C.acwr_sweet_hi, "acwr_elevated": C.acwr_elevated,
        "acwr_ramp_per_unit": C.acwr_ramp_per_unit,
        "acwr_steep_per_unit": C.acwr_steep_per_unit,
        "recovery_time_penalty_per_h": C.recovery_time_penalty_per_h,
        "penalty_cap": C.penalty_cap,
        "baseline_window_days": C.baseline_window_days,
        "min_baseline_n": C.min_baseline_n,
        "min_history_days": C.min_history_days,
        "confidence_weighting": C.confidence_weighting,
        "confidence_ci_max": C.confidence_ci_max,
        "confidence_ok_threshold": C.confidence_ok_threshold,
        "baseline_estimator": C.baseline_estimator,
        "baseline_ewma": C.baseline_ewma,
        "baseline_half_life_days": C.baseline_half_life_days,
        "winsorize_enabled": C.winsorize_enabled,
        "winsor_k": C.winsor_k,
        "hrv_smoothing_days": C.hrv_smoothing_days,
        "swc_multiplier": C.swc_multiplier,
        "hrv_band_from_sd": C.hrv_band_from_sd,
        "hrv_cv_enabled": C.hrv_cv_enabled,
        "hrv_taper_enabled": C.hrv_taper_enabled,
        "hrv_taper_z": C.hrv_taper_z,
        "hrv_taper_factor": C.hrv_taper_factor,
        "acwr_acute_days": C.acwr_acute_days,
        "acwr_chronic_days": C.acwr_chronic_days,
        "acwr_uncoupled": C.acwr_uncoupled,
        "hrv_log_transform": C.hrv_log_transform,
        "illness_detector_enabled": C.illness_detector_enabled,
        "illness_z_threshold": C.illness_z_threshold,
        "illness_trigger": C.illness_trigger,
        "illness_readiness_cap": C.illness_readiness_cap,
        # recovery_weights / sleep_subweights as sorted key/value lists.
        "recovery_weights": C.recovery_weights,
        "sleep_subweights": C.sleep_subweights,
    }


def _baseline_stats_cases() -> list[dict]:
    series = {
        "steady": [50, 55, 48, 60, 52, 58, 49, 61, 53, 57, 51, 59, 54, 56],
        "trend_up": [40 + i for i in range(20)],           # sustained rise
        "with_outlier": [50, 52, 48, 51, 49, 53, 200, 50, 52, 48, 51, 49],  # 1 spike
    }
    out = []
    for name, vals in series.items():
        vals = [float(v) for v in vals]
        for est in ("robust", "classic"):
            for ewma in (True, False):
                for winsor in (True, False):
                    cfg = scoring.ScoringConfig(baseline_estimator=est,
                                                baseline_ewma=ewma,
                                                winsorize_enabled=winsor)
                    center, scale, n = scoring.baseline_stats(vals, cfg)
                    out.append({"name": name, "vals": vals, "estimator": est,
                                "ewma": ewma, "winsor": winsor,
                                "center": center, "scale": scale, "n": n})
    return out


def _cv_cases() -> list[dict]:
    windows = [[3.9, 4.0, 3.95, 4.1, 3.85], [5.0, 5.0, 5.0], [4.2, 3.8], [1.0], []]
    return [{"vals": w, "cv": scoring.cv(w)} for w in windows]


def _rolling_cv_cases() -> list[dict]:
    series = [3.9, 4.0, 3.95, 4.1, 3.85, 4.05, 3.9, 4.0, 3.95, 4.1, 3.88, 4.02]
    out = []
    for window in (3, 7):
        out.append({"vals": series, "window": window,
                    "cvs": scoring.rolling_cvs(series, window)})
    return out


def _hrv_cv_score_cases() -> list[dict]:
    base = [0.020, 0.030, 0.025, 0.028, 0.022, 0.031, 0.026, 0.024, 0.029,
            0.027, 0.023, 0.030]
    out = []
    for cvt in (None, 0.005, 0.026, 0.050):
        out.append({"cv_today": cvt, "baseline_cvs": base,
                    "expected": scoring.hrv_cv_score(cvt, base)})
    return out


def _hrv_taper_cases() -> list[dict]:
    cfg = scoring.ScoringConfig(hrv_taper_enabled=True)
    return [{"z": z, "expected": scoring.hrv_score(z, cfg)}
            for z in (1.0, 2.0, 2.5, 3.0, 4.0)]


def _significance_cases() -> list[dict]:
    out = []
    for z in (None, 0.0, 0.4, 0.5, 0.9, 1.0, 1.5, -1.2):
        for scale in (0.0, 2.0, 5.5):
            swc, meaningful, sig = scoring.significance(z, scale)
            out.append({"z": z, "scale": scale, "swc": swc,
                        "meaningful": meaningful, "significant": sig})
    return out


def _hrv_baseline_z_cases() -> list[dict]:
    bases = [
        [50, 55, 48, 60, 52, 58, 49, 61, 53, 57, 51, 59],   # steady baseline
        [30, 90, 45, 70, 35, 80, 40, 60, 50, 100, 33, 88],  # skewed/spread
    ]
    out = []
    for base in bases:
        for today in (62.0, 40.0, 100.0):
            for log in (True, False):
                cfg = scoring.ScoringConfig(hrv_log_transform=log)
                mean, sd, z = scoring.hrv_baseline_z(today, base, cfg)
                out.append({"today": today, "base": base, "log": log,
                            "mean": mean, "sd": sd, "z": z})
    # Guards: today <= 0 and empty baseline.
    m, s, z = scoring.hrv_baseline_z(None, [50, 55, 60], C)
    out.append({"today": None, "base": [50, 55, 60], "log": True,
                "mean": m, "sd": s, "z": z})
    # 7-day-smoothed today (recent_values incl. today, oldest-first).
    for base in bases:
        recent = [55.0, 48.0, 60.0, 52.0, 58.0, 49.0, 90.0]   # last incl. today
        m, s, z = scoring.hrv_baseline_z(recent[-1], base, C, recent_values=recent)
        out.append({"today": recent[-1], "base": base, "log": True,
                    "recent": recent, "mean": m, "sd": s, "z": z})
    return out


def _subscore_cases() -> list[dict]:
    zs = [-3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0, 5.25, None]
    out = []
    for kind, fn in (("hrv", scoring.hrv_score), ("rhr", scoring.rhr_score),
                     ("resp", scoring.resp_score), ("temp", scoring.temp_score)):
        for z in zs:
            out.append({"kind": kind, "z": z, "expected": fn(z)})
    return out


def _temp_cases() -> list[dict]:
    out = []
    for z in (-3.0, -2.0, -1.0, -0.5, 0.0, 0.5, 1.0, 2.0, 3.0):
        for luteal in (False, True):
            for asym in (True, False):
                cfg = scoring.ScoringConfig(temp_asymmetric=asym)
                out.append({"z": z, "luteal": luteal, "asymmetric": asym,
                            "expected": scoring.temp_score(z, cfg, luteal)})
    return out


def _sleep_cases() -> list[dict]:
    cases = [
        (8.0, 8.0, 0.45, 0, 0.0),
        (4.0, 8.0, None, None, None),
        (7.0, 8.0, 0.30, 2, 30.0),
        (9.5, None, 0.20, 5, 75.0),
        (6.0, 7.5, 0.50, 1, 12.0),
        (11.0, 8.0, None, None, None),        # big oversleep
        (8.0, 7.0, 0.40, 0, 0.0),             # over personalized need
        (None, None, None, None, None),
    ]
    out = []
    for hours, need, dr, aw, reg in cases:
        out.append({"hours": hours, "need": need, "deep_rem": dr,
                    "awakenings": aw, "regularity": reg,
                    "expected": scoring.sleep_score(hours, need, dr, aw, reg)})
    return out


def _sleep_need_cases() -> list[dict]:
    windows = [
        [7.5, 7.0, 8.0, 7.2, 7.8, 7.1],       # mature -> personalized median
        [6.0, 5.5, 6.2],                       # immature (< min samples) -> default
        [10.0, 10.5, 9.8, 10.2, 9.9, 10.1],   # clamped to max
        [5.0, 4.8, 5.2, 5.1, 4.9, 5.0],       # clamped to min
        [],
    ]
    return [{"baseline_hours": w, "need": scoring.personalized_sleep_need(w)}
            for w in windows]


def _metric_confidence_cases() -> list[dict]:
    return [{"n": n, "conf": scoring.metric_confidence(n)}
            for n in (0, 3, 5, 9, 10, 15, 20)]


def _recovery_confidence_cases() -> list[dict]:
    subs = {"hrv": 80.0, "sleeping_hr": 60.0, "sleep": 70.0, "resp": 55.0,
            "temp": 90.0}
    confs = [
        {"hrv": 1.0, "sleeping_hr": 1.0, "sleep": 1.0, "resp": 1.0, "temp": 1.0},
        {"hrv": 0.3, "sleeping_hr": 1.0, "sleep": 0.5, "resp": 0.2, "temp": 1.0},
        {"hrv": 0.0, "sleeping_hr": 0.0, "sleep": 0.0, "resp": 0.0, "temp": 0.0},
    ]
    out = []
    for cf in confs:
        r = scoring.recovery_score(subs, confidences=cf)
        out.append({"subscores": subs, "confidences": cf, "score": r["score"],
                    "band": r["band"], "confidence": r["confidence"],
                    "ci": r["ci"], "contributors": r["contributors"]})
    return out


def _recovery_cases() -> list[dict]:
    subs = [
        {"hrv": 90.0, "sleeping_hr": 60.0},
        {"hrv": 86.0, "sleeping_hr": 86.0, "sleep": 100.0, "resp": 50.0, "temp": 100.0},
        {"hrv": 72.5, "sleeping_hr": 44.0, "sleep": 88.3, "resp": 61.0, "temp": 95.0},
        {"hrv": None},
    ]
    out = []
    for s in subs:
        r = scoring.recovery_score(s)
        out.append({"subscores": s, "score": r["score"], "band": r["band"],
                    "contributors": r["contributors"]})
    return out


def _load_penalty_cases() -> list[dict]:
    cases = [(1.0, None, None), (0.6, None, None), (1.3, None, None),
             (1.4, None, None), (1.5, None, None), (1.8, None, None),
             (2.0, None, None), (1.4, None, 10.0), (None, None, 5.0),
             (3.0, 500.0, 40.0)]
    out = []
    for acwr, al, rt in cases:
        out.append({"acwr": acwr, "acute_load": al, "recovery_time": rt,
                    "expected": scoring.load_penalty(acwr, al, rt)})
    return out


def _readiness_cases() -> list[dict]:
    cases = [(70.0, 1.0, None, None), (70.0, 1.8, None, None),
             (82.4, 1.45, 300.0, 8.0), (None, None, None, None),
             (55.0, 2.2, 400.0, 20.0)]
    out = []
    for rec, acwr, al, rt in cases:
        r = scoring.readiness_score(rec, acwr, al, rt)
        out.append({"recovery": rec, "acwr": acwr, "acute_load": al,
                    "recovery_time": rt, "score": r["score"], "band": r["band"],
                    "penalty": r["penalty"], "recommendation": r["recommendation"]})
    return out


def _acwr_cases() -> list[dict]:
    steady = [50.0] * 30
    ramp = [float(10 + 3 * i) for i in range(30)]        # rising load
    taper = [float(100 - 2 * i) for i in range(30)]      # falling load
    spike = [40.0] * 23 + [200.0] * 7                    # acute spike vs steady base
    series = {"steady": steady, "ramp": ramp, "taper": taper, "spike": spike,
              "short": [30.0] * 5, "empty": [], "zeros": [0.0] * 30}
    out = []
    for name, loads in series.items():
        for uncoupled in (True, False):
            cfg = scoring.ScoringConfig(acwr_uncoupled=uncoupled)
            out.append({"name": name, "loads": loads, "uncoupled": uncoupled,
                        "expected": scoring.acwr_ratio(loads, cfg)})
    return out


def _illness_cases() -> list[dict]:
    zsets = [
        {"temp": 1.5, "resp": 1.2, "sleeping_hr": 0.5, "hrv": -0.3},   # 2 fire
        {"temp": 0.2, "resp": 0.1, "sleeping_hr": 0.0, "hrv": 0.5},    # none
        {"temp": 2.0, "resp": 1.5, "sleeping_hr": 1.1, "hrv": -1.8},   # all 4
        {"temp": 1.5, "hrv": None, "resp": None},                      # 1 fire only
        {"hrv": -2.0, "sleeping_hr": 1.4},                             # 2 fire
        {},                                                            # empty
    ]
    out = []
    for zs in zsets:
        r = scoring.illness_signals(zs)
        out.append({"zscores": zs, "count": r["count"], "pressure": r["pressure"],
                    "triggered": r["triggered"], "signals": r["signals"]})
    return out


def _readiness_illness_cases() -> list[dict]:
    cases = [
        (80.0, 1.0, {"temp": 1.5, "resp": 1.3, "sleeping_hr": 0.4, "hrv": -1.2}),
        (80.0, 1.0, {"temp": 0.1, "resp": 0.2, "sleeping_hr": 0.0, "hrv": 0.3}),
        (50.0, 1.0, {"temp": 2.0, "resp": 2.0, "sleeping_hr": 2.0, "hrv": -2.0}),
    ]
    out = []
    for rec, acwr, zs in cases:
        r = scoring.readiness_score(rec, acwr, illness_zscores=zs)
        out.append({"recovery": rec, "acwr": acwr, "zscores": zs,
                    "score": r["score"], "band": r["band"], "penalty": r["penalty"],
                    "recommendation": r["recommendation"],
                    "illness_triggered": bool(r["illness"] and r["illness"]["triggered"])})
    return out


def build_fixture() -> dict:
    return {
        "_note": "Generated by scripts/gen_parity_fixture.py — do not edit by hand.",
        "config": _config_block(),
        "baseline_stats": _baseline_stats_cases(),
        "significance": _significance_cases(),
        "cv": _cv_cases(),
        "rolling_cv": _rolling_cv_cases(),
        "hrv_cv_score": _hrv_cv_score_cases(),
        "hrv_taper": _hrv_taper_cases(),
        "temp": _temp_cases(),
        "sleep_need": _sleep_need_cases(),
        "metric_confidence": _metric_confidence_cases(),
        "recovery_confidence": _recovery_confidence_cases(),
        "hrv_baseline_z": _hrv_baseline_z_cases(),
        "subscore": _subscore_cases(),
        "sleep": _sleep_cases(),
        "recovery": _recovery_cases(),
        "load_penalty": _load_penalty_cases(),
        "readiness": _readiness_cases(),
        "acwr": _acwr_cases(),
        "illness": _illness_cases(),
        "readiness_illness": _readiness_illness_cases(),
    }


def main() -> None:
    fixture = build_fixture()
    default_out = Path(__file__).resolve().parents[1] / "tests" / "fixtures" \
        / "parity_fixture.json"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default_out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
