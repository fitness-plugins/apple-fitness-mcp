#!/usr/bin/env python3
"""Offline weight / reference calibration — Phases 4.2 & 4.3.

**This is OUT OF THE LIVE SCORING PATH.** It never mutates `ScoringConfig`; it
only *prints suggestions* the user applies by hand, and reports drift between the
freshly re-derived baseline reference stats and a stored reference file.

Two jobs:

1. `fit` — given a history of daily sub-scores and the user's own subjective
   readiness labels, fit a ridge/linear model to suggest recovery weights and
   report each sub-score's correlation with the subjective label.

2. `--check` — re-derive the per-metric baseline reference stats (HRV ln-mean /
   ln-sd and the SWC band edge) from the full HRV history and compare them to the
   stored `calibration_reference.json`; print the drift and exit non-zero when it
   crosses the tolerance, so the biweekly launchd job can gate a reminder on it.

Pure-Python (no numpy) so it runs on the stock interpreter. HRV history and
sub-scores are read from CSV; for real use, generate those CSVs from the DuckDB
export (a `--from-db` path is stubbed for wiring later).
"""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path

SUBSCORE_COLS = ["hrv", "sleeping_hr", "sleep", "resp", "temp", "hrv_cv"]


# --- small linear algebra (pure Python) ---------------------------------------

def _solve(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve A x = b by Gauss-Jordan elimination (A small, square)."""
    n = len(A)
    M = [row[:] + [b[i]] for i, row in enumerate(A)]
    for col in range(n):
        piv = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            continue
        M[col], M[piv] = M[piv], M[col]
        pivval = M[col][col]
        M[col] = [v / pivval for v in M[col]]
        for r in range(n):
            if r != col and abs(M[r][col]) > 1e-15:
                factor = M[r][col]
                M[r] = [a - factor * b_ for a, b_ in zip(M[r], M[col])]
    return [M[i][n] for i in range(n)]


def ridge_fit(X: list[list[float]], y: list[float], lam: float) -> list[float]:
    """Ridge regression with an intercept. Returns coefficients (intercept last).
    Ridge penalty is applied to the feature terms, not the intercept."""
    n_features = len(X[0])
    aug = [row + [1.0] for row in X]          # intercept column
    p = n_features + 1
    ata = [[0.0] * p for _ in range(p)]
    aty = [0.0] * p
    for row, yi in zip(aug, y):
        for i in range(p):
            aty[i] += row[i] * yi
            for j in range(p):
                ata[i][j] += row[i] * row[j]
    for i in range(n_features):               # regularize features only
        ata[i][i] += lam
    return _solve(ata, aty)


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


# --- IO -----------------------------------------------------------------------

def _read_csv(path: Path) -> tuple[list[str], list[dict]]:
    with open(path, newline="") as f:
        r = csv.DictReader(f)
        return list(r.fieldnames or []), list(r)


def load_subscores(path: Path) -> tuple[list[str], dict[str, dict]]:
    fields, rows = _read_csv(path)
    feats = [c for c in fields if c != "date" and c in SUBSCORE_COLS]
    out: dict[str, dict] = {}
    for row in rows:
        vals = {}
        for c in feats:
            v = row.get(c, "")
            vals[c] = float(v) if v not in ("", None) else None
        out[row["date"]] = vals
    return feats, out


def load_labels(path: Path) -> dict[str, float]:
    _, rows = _read_csv(path)
    out = {}
    for row in rows:
        v = row.get("perceived_readiness", "")
        if v not in ("", None):
            out[row["date"]] = float(v)
    return out


def load_hrv_history(path: Path) -> list[float]:
    _, rows = _read_csv(path)
    out = []
    for row in rows:
        v = row.get("hrv", "")
        if v not in ("", None):
            out.append(float(v))
    return out


# --- fit (4.2) ----------------------------------------------------------------

def fit(subscores_path: Path, labels_path: Path, lam: float) -> dict:
    feats, subs = load_subscores(subscores_path)
    labels = load_labels(labels_path)
    dates = sorted(set(subs) & set(labels))
    X, y, per_feat = [], [], {f: ([], []) for f in feats}
    for d in dates:
        row = subs[d]
        if any(row.get(f) is None for f in feats):
            continue
        xr = [row[f] for f in feats]
        X.append(xr)
        y.append(labels[d])
        for f, v in zip(feats, xr):
            per_feat[f][0].append(v)
            per_feat[f][1].append(labels[d])
    result = {"n_days": len(X), "features": feats,
              "correlations": {}, "suggested_weights": {}, "coefficients": {}}
    if len(X) < max(3, len(feats) + 1):
        result["note"] = "Not enough aligned days to fit; need more subjective log."
        return result
    coefs = ridge_fit(X, y, lam)
    feat_coefs = dict(zip(feats, coefs[:-1]))
    # Suggested weights: clip negative coefficients to 0, normalize to sum 1.
    pos = {f: max(0.0, c) for f, c in feat_coefs.items()}
    total = sum(pos.values()) or 1.0
    for f in feats:
        result["coefficients"][f] = round(feat_coefs[f], 4)
        result["suggested_weights"][f] = round(pos[f] / total, 3)
        xs, ys = per_feat[f]
        r = pearson(xs, ys)
        result["correlations"][f] = round(r, 3) if r is not None else None
    return result


# --- reference drift (4.3) ----------------------------------------------------

def _median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2.0


def reference_stats(hrv_values: list[float]) -> dict:
    """Full-history HRV reference: ln-mean, ln-sd (sample), robust ln-MAD scale,
    and the SWC edge (0.5 ln-sd). These are the numbers the live baseline drifts
    toward as data accrues; the reminder watches ln-sd."""
    logs = [math.log(v) for v in hrv_values if v > 0]
    n = len(logs)
    if n < 2:
        return {"n": n}
    mean = sum(logs) / n
    sd = math.sqrt(sum((x - mean) ** 2 for x in logs) / (n - 1))
    med = _median(logs)
    mad = 1.4826 * _median([abs(x - med) for x in logs])
    return {"n": n, "hrv_ln_mean": round(mean, 5), "hrv_ln_sd": round(sd, 5),
            "hrv_ln_mad_scale": round(mad, 5), "hrv_swc_edge": round(0.5 * sd, 5)}


def check(hrv_history_path: Path, reference_path: Path, tol: float) -> int:
    current = reference_stats(load_hrv_history(hrv_history_path))
    if current.get("n", 0) < 2:
        print("check: not enough HRV history to compute reference stats.")
        return 0
    if not reference_path.exists():
        reference_path.write_text(json.dumps(current, indent=2) + "\n")
        print(f"check: no stored reference; wrote baseline to {reference_path}.")
        return 0
    stored = json.loads(reference_path.read_text())
    drifts = {}
    exceeded = False
    for key in ("hrv_ln_mean", "hrv_ln_sd", "hrv_ln_mad_scale", "hrv_swc_edge"):
        cur, old = current.get(key), stored.get(key)
        if cur is None or old is None or old == 0:
            continue
        rel = abs(cur - old) / abs(old)
        drifts[key] = {"stored": old, "current": cur, "rel_drift": round(rel, 4)}
        if rel > tol:
            exceeded = True
    print("=== calibration drift (current vs stored reference) ===")
    for key, d in drifts.items():
        flag = "  <-- exceeds tol" if d["rel_drift"] > tol else ""
        print(f"{key:18s} stored={d['stored']:.5f} current={d['current']:.5f} "
              f"rel={d['rel_drift']:.4f}{flag}")
    print(f"tolerance: {tol:.2%}")
    if exceeded:
        print("DRIFT EXCEEDS TOLERANCE — recalibration recommended "
              "(re-run `calibrate.py fit`, review suggested weights).")
        return 1
    print("within tolerance — no action needed.")
    return 0


# --- CLI ----------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--subscores", type=Path, help="CSV: date,hrv,sleeping_hr,...")
    p.add_argument("--labels", type=Path, help="CSV: date,perceived_readiness")
    p.add_argument("--lam", type=float, default=1.0, help="ridge penalty")
    p.add_argument("--check", action="store_true",
                   help="drift-report mode: exit non-zero past tolerance")
    p.add_argument("--hrv-history", type=Path, help="CSV: date,hrv (for --check)")
    p.add_argument("--reference", type=Path,
                   default=Path("calibration_reference.json"))
    p.add_argument("--tol", type=float, default=0.10,
                   help="relative drift tolerance for --check")
    args = p.parse_args(argv)

    if args.check:
        if not args.hrv_history:
            p.error("--check requires --hrv-history CSV")
        return check(args.hrv_history, args.reference, args.tol)

    if args.subscores and args.labels:
        res = fit(args.subscores, args.labels, args.lam)
        print(json.dumps(res, indent=2))
        print("\n# Suggested ScoringConfig.recovery_weights (review before applying):")
        print("#   " + ", ".join(f"{f}={w}"
              for f, w in res["suggested_weights"].items()))
        return 0

    p.error("provide --subscores + --labels (fit), or --check + --hrv-history")


if __name__ == "__main__":
    raise SystemExit(main())
