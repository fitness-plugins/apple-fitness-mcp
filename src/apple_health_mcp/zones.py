"""Heart-rate anchors (HRmax / resting HR) and the two zone models.

This module owns every number a zone boundary is derived from, so the tools in
`server.py` never re-derive them and never disagree with each other.

**Two zone models, both live at once.**

- ``percent`` — the classic ``Z1..Z5`` %-of-max scheme (Z1 <60, Z2 60-70,
  Z3 70-80, Z4 80-90, Z5 >=90). Bit-for-bit the same numbers and the same SQL
  text as the older `analytics.zone_bounds` / `analytics.zone_case_sql`, because
  the dashboard and the existing tests depend on it.
- ``training`` — the athlete's own named absolute-bpm bands (recovery / easy /
  grey / threshold / vo2max), loaded from ``config/training_zones.json``. The
  percentage model cannot answer the one question that matters most about a
  quality session: at a 209 bpm HRmax, Z4 spans 168-189 and so lumps the *grey
  zone* (166-177, the moderate-intensity rut the whole training model exists to
  eliminate) together with real *threshold* work (178-186). Those are different
  sessions with different adaptations, and they need different buckets.

**Why the anchors are resolved here.** The observed maximum across all workouts
is 210 bpm and that single reading is a sensor artefact; the project's own
calibration puts functional HRmax at 209 (95th percentile of per-run maxima).
Deriving zones from the raw observed max means every boundary in the system is
built on noise. `resolve_hr_max` therefore prefers, in order: an explicit
override, the value the recalibration job persisted in
`config.calibration_reference_path()`, the functional HRmax re-derived from
deduplicated per-run maxima, and only then the raw observed max.

Percentiles use ``statistics.quantiles(..., n=20, method="inclusive")[18]``.
``sorted(xs)[int(len(xs) * 0.95)]`` is NOT a 95th percentile — for small n it
returns the maximum, so the outlier the percentile exists to exclude would set
the value anyway. See CLAUDE.md.
"""
from __future__ import annotations

import json
import re
import statistics as st
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from . import config

Query = Callable[..., list[dict[str, Any]]]

PERCENT_MODEL = "percent"
TRAINING_MODEL = "training"
MODELS = (PERCENT_MODEL, TRAINING_MODEL)

# --- percentage model ---------------------------------------------------------

# (label, lo fraction of max, hi fraction of max). Z1 has no lower gap and Z5 no
# upper one, so every sample maps to exactly one zone.
_ZONE_PCTS = (
    ("Z1", 0.00, 0.60),
    ("Z2", 0.60, 0.70),
    ("Z3", 0.70, 0.80),
    ("Z4", 0.80, 0.90),
    ("Z5", 0.90, 1.00),
)

# --- anchor resolution defaults ----------------------------------------------

FALLBACK_HR_MAX = 190.0
FALLBACK_RESTING_HR = 60.0
# Plausibility gates on a persisted calibrated value: a typo or a half-written
# file must not silently redefine every zone boundary.
HR_MAX_RANGE = (120.0, 250.0)
RESTING_HR_RANGE = (25.0, 120.0)
# Keys accepted in the calibration reference file, in preference order.
HR_MAX_KEYS = ("hr_max", "hrmax", "functional_hr_max", "max_hr")
RESTING_HR_KEYS = ("resting_hr", "rhr", "resting_heart_rate")
# A p95 needs a real sample; below this many runs fall through to the raw max.
MIN_RUNS_FOR_FUNCTIONAL_MAX = 4
# The population the functional HRmax is derived from: running workouts on or
# after this date. It deliberately mirrors `dashboard._SINCE_DAILY`, so the
# dashboard's HRmax and the tools' HRmax are the same number by construction
# rather than by coincidence -- two derivations over different windows would
# give two different "functional HRmax" values with no way to tell them apart.
# (Duplicated rather than imported: `dashboard` pulls in duckdb and storage, and
# `zones` must stay importable without them.)
FUNCTIONAL_HR_MAX_SINCE = "2024-01-01"

_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

# Built-in mirror of the shipped config/training_zones.json, used only when that
# file is absent (e.g. installed as a wheel, which packages src/ only). A file
# that exists but is malformed raises instead — silently falling back would hide
# a typo in the athlete's own configuration.
_BUILTIN_TRAINING_ZONES: tuple[dict[str, Any], ...] = (
    {"name": "recovery", "min_bpm": None, "max_bpm": 149},
    {"name": "easy", "min_bpm": 150, "max_bpm": 165},
    {"name": "grey", "min_bpm": 166, "max_bpm": 177},
    {"name": "threshold", "min_bpm": 178, "max_bpm": 186},
    {"name": "vo2max", "min_bpm": 187, "max_bpm": None},
)

_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


# --- calibrated reference file ------------------------------------------------

def load_calibration_reference(path: Optional[Path] = None) -> dict:
    """The persisted calibration reference as a dict ({} when unusable).

    Written by `scripts/calibrate.py --check` (driven biweekly by
    `launchd/com.applehealth.recalibration.plist` via
    `scripts/recalibration_check.sh`) to `data/calibration_reference.json`,
    overridable with the `CALIBRATION_REFERENCE` env var. Never raises: a
    missing, unreadable, half-written or non-object file just means "no
    calibrated value", and the caller falls through to the observed numbers.
    """
    p = Path(path) if path is not None else config.calibration_reference_path()
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _calibrated_value(ref: dict, keys: Sequence[str],
                      lo: float, hi: float) -> Optional[float]:
    """First plausible numeric value among `keys`, or None."""
    for key in keys:
        v = ref.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        v = float(v)
        if lo <= v <= hi:
            return v
    return None


# --- HR anchors ---------------------------------------------------------------

def resolve_hr_max(q: Optional[Query] = None, hr_max: Optional[float] = None, *,
                   reference_path: Optional[Path] = None,
                   derive_functional: bool = True,
                   since: Optional[str] = FUNCTIONAL_HR_MAX_SINCE
                   ) -> tuple[float, str]:
    """(value, source) for the HRmax every zone boundary is scaled from.

    Preference order:

    1. `hr_max` passed by the caller.
    2. The calibrated value persisted by the recalibration job.
    3. Functional HRmax = p95 of the *deduplicated* per-run maxima over the
       same population `dashboard.py` uses (needs at least
       `MIN_RUNS_FOR_FUNCTIONAL_MAX` runs). This excludes the single 210 bpm
       artefact; on the real export the dashboard's own run of this derivation
       produced 208.1, while the project notes quote a functional HRmax of 209,
       so the two are not the same number and the calibrated value in step 2 is
       what should settle it. Pass `derive_functional=False` to skip this step.
    4. `max(workouts.max_hr)` — the raw observed max.
    5. `FALLBACK_HR_MAX`.

    `q` is the read-only query function (`server._q`); omit it to resolve
    without touching the database. `since` bounds the population step 3 derives
    from (`None` = all time). `source` is a short human-readable string in the
    style the tools already surface as `max_hr_source`; it names both the sample
    size and the window, so a disagreement with the dashboard shows up in the
    tool output instead of silently changing every zone boundary.
    """
    if hr_max is not None:
        return float(hr_max), "provided"

    ref_path = (Path(reference_path) if reference_path is not None
                else config.calibration_reference_path())
    cal = _calibrated_value(load_calibration_reference(ref_path),
                            HR_MAX_KEYS, *HR_MAX_RANGE)
    if cal is not None:
        return cal, f"calibrated hr_max from {ref_path.name}"

    if q is not None:
        if derive_functional:
            run_max = per_run_maxima(q, since=since)
            if len(run_max) >= MIN_RUNS_FOR_FUNCTIONAL_MAX:
                # Deliberately NOT rounded: this is the same real number
                # dashboard.py derives from the same population, and rounding
                # here would let the two drift a bpm apart. Rounding for display
                # happens in the zone bounds.
                p95 = st.quantiles(run_max, n=20, method="inclusive")[18]
                window = f"since {since}" if since else "all time"
                return (float(p95),
                        f"functional HRmax (p95 of {len(run_max)} per-run "
                        f"maxima, {window})")
        rows = q("SELECT max(max_hr) AS m FROM workouts", ())
        m = rows[0]["m"] if rows else None
        if m:
            return float(round(m)), "estimated from workouts.max_hr"

    return FALLBACK_HR_MAX, "default (no workout max_hr available)"


def per_run_maxima(q: Query,
                   since: Optional[str] = FUNCTIONAL_HR_MAX_SINCE) -> list[float]:
    """Sorted per-run max HR, one value per real run.

    The export stores each workout 2-3 times (repeated exports plus a watch
    rename), so identity is `(type, start minute)` and the copies are collapsed
    with `DISTINCT ON` exactly as `dashboard._q_workouts` does. Without that the
    percentile is computed over triplicated readings.

    `since` (an ISO date, or None for all time) bounds the population the same
    way `dashboard._q_workouts` does. It is inlined into the SQL like the
    dashboard's own cutoff, so it is validated as a bare date first.
    """
    window = ""
    if since:
        if not _DATE_RE.match(str(since)):
            raise ValueError(
                f"since must be an ISO date (YYYY-MM-DD), got {since!r}")
        window = f" AND start_ts >= '{since}'"
    # The DISTINCT ON expressions are also selected, and the subquery is left
    # unaliased, exactly as dashboard._q_workouts writes it — that form is known
    # to run in the DuckDB build the server ships.
    rows = q(
        "SELECT max_hr FROM ("
        "  SELECT DISTINCT ON (type, date_trunc('minute', start_ts)) "
        "         type, date_trunc('minute', start_ts) AS run_minute, max_hr "
        "  FROM workouts WHERE type = 'running' AND max_hr IS NOT NULL"
        f"{window} "
        "  ORDER BY type, date_trunc('minute', start_ts)"
        ")", ())
    return sorted(float(r["max_hr"]) for r in rows if r["max_hr"])


def resolve_resting_hr(q: Optional[Query] = None,
                       resting_hr: Optional[float] = None, *,
                       reference_path: Optional[Path] = None,
                       window_days: int = 90,
                       pct: float = 0.10) -> tuple[float, str]:
    """(value, source) for resting HR, used by the TRIMP heart-rate reserve.

    Preference order: caller override, the calibrated value from the
    recalibration reference, then the existing behaviour — a low percentile
    (p10 by default) of recent `resting_heart_rate`. A mean or median sits well
    above true resting because "resting" readings still include elevated ones.
    The trailing window is anchored to the most recent reading (so it does not
    depend on the query range), then an all-time percentile, then
    `FALLBACK_RESTING_HR`.
    """
    if resting_hr is not None:
        return float(resting_hr), "provided"

    ref_path = (Path(reference_path) if reference_path is not None
                else config.calibration_reference_path())
    cal = _calibrated_value(load_calibration_reference(ref_path),
                            RESTING_HR_KEYS, *RESTING_HR_RANGE)
    if cal is not None:
        return cal, f"calibrated resting_hr from {ref_path.name}"

    if q is not None:
        tag = f"p{int(pct * 100)}"
        # window_days / pct are code constants -> safe to inline.
        rows = q(
            f"SELECT quantile_cont(value, {pct}) AS m FROM records_dedup "
            "WHERE type = 'resting_heart_rate' AND start_ts >= "
            "(SELECT max(start_ts) FROM records_dedup "
            " WHERE type = 'resting_heart_rate') "
            f"- INTERVAL {int(window_days)} DAY", ())
        m = rows[0]["m"] if rows else None
        if m:
            return (float(round(m)),
                    f"{tag} of resting_heart_rate (trailing {window_days}d)")
        rows = q(f"SELECT quantile_cont(value, {pct}) AS m FROM records_dedup "
                 "WHERE type = 'resting_heart_rate'", ())
        m = rows[0]["m"] if rows else None
        if m:
            return float(round(m)), f"{tag} of resting_heart_rate (all-time)"

    return FALLBACK_RESTING_HR, "default (no resting_heart_rate available)"


# --- training-zone configuration ----------------------------------------------

def load_training_zones(path: Optional[Path] = None) -> list[dict]:
    """The athlete's named bpm bands as raw config dicts.

    Reads `config.ZONES_CONFIG_PATH` (env `HEALTH_ZONES_CONFIG`). A missing file
    falls back to the built-in mirror of the shipped default; a file that exists
    but is malformed raises `ValueError`, because silently ignoring a typo in
    the athlete's own zones is exactly the failure this module exists to stop.

    Not cached: the file is tiny next to the DuckDB queries around it, and a
    cache would serve stale bands after the user edits his zones.
    """
    p = Path(path) if path is not None else config.ZONES_CONFIG_PATH
    if not p.exists():
        return [dict(z) for z in _BUILTIN_TRAINING_ZONES]
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError(f"unreadable training-zone config {p}: {exc}") from exc
    raw = data.get("zones") if isinstance(data, dict) else data
    if not isinstance(raw, list) or not raw:
        raise ValueError(f"training-zone config {p} has no 'zones' list")
    return _validate_training_zones(raw, p)


def _validate_training_zones(raw: list, p: Path) -> list[dict]:
    """Check names, ordering and contiguity; return the bands unchanged.

    Bands are inclusive on both edges and must tile the whole bpm axis with no
    gap and no overlap: the first has an open bottom, the last an open top, and
    every `max_bpm + 1` equals the next `min_bpm`. A gap would silently drop
    time from every total; an overlap would double-count it.
    """
    out: list[dict] = []
    prev_max: Optional[int] = None
    last = len(raw) - 1
    for i, z in enumerate(raw):
        if not isinstance(z, dict):
            raise ValueError(f"training-zone config {p}: zone {i} is not an object")
        name = z.get("name")
        # The name is interpolated into SQL (zone_case_sql), so it is restricted
        # to an identifier-shaped token rather than escaped.
        if not isinstance(name, str) or not _NAME_RE.match(name):
            raise ValueError(
                f"training-zone config {p}: zone {i} name {name!r} must match "
                "[A-Za-z][A-Za-z0-9_]*")
        if any(o["name"] == name for o in out):
            # Zone names key every total; duplicates would silently merge bands.
            raise ValueError(f"training-zone config {p}: duplicate zone name "
                             f"{name!r}")
        lo, hi = z.get("min_bpm"), z.get("max_bpm")
        for label, v in (("min_bpm", lo), ("max_bpm", hi)):
            if v is not None and (isinstance(v, bool) or not isinstance(v, int)):
                raise ValueError(f"training-zone config {p}: {name}.{label} "
                                 "must be an integer or null")
        if (lo is None) != (i == 0):
            raise ValueError(f"training-zone config {p}: only the first zone may "
                             f"have a null min_bpm (got {name})")
        if (hi is None) != (i == last):
            raise ValueError(f"training-zone config {p}: only the last zone may "
                             f"have a null max_bpm (got {name})")
        if lo is not None and hi is not None and hi < lo:
            raise ValueError(f"training-zone config {p}: {name} max_bpm {hi} "
                             f"is below min_bpm {lo}")
        if prev_max is not None and lo != prev_max + 1:
            raise ValueError(
                f"training-zone config {p}: {name} starts at {lo}, expected "
                f"{prev_max + 1} — bands must be contiguous (no gap, no overlap)")
        prev_max = hi
        out.append(dict(z))
    if len(out) < 2:
        raise ValueError(f"training-zone config {p}: need at least two zones")
    return out


# --- zone bounds --------------------------------------------------------------

def percent_zone_bounds(hr_max: float) -> list[dict]:
    """Z1-Z5 %-of-max bounds in bpm.

    Identical numbers to the long-standing `analytics.zone_bounds`: Z1 spans
    everything below 60% (`lo_bpm` 0) and Z5 anything at or above 90%, so every
    sample maps to a zone with no gap. `lo_edge`/`hi_edge` carry the *unrounded*
    thresholds actually used for classification; `lo_bpm`/`hi_bpm` are the
    rounded display values.
    """
    out = []
    for label, lo, hi in _ZONE_PCTS:
        out.append({
            "zone": label,
            "lo_pct": int(lo * 100),
            "hi_pct": int(hi * 100),
            "lo_bpm": round(hr_max * lo),
            "hi_bpm": round(hr_max * hi),
            "lo_edge": hr_max * lo,
            "hi_edge": (hr_max * hi) if label != "Z5" else float("inf"),
        })
    return out


def training_zone_bounds(zones: Optional[list[dict]] = None,
                         path: Optional[Path] = None) -> list[dict]:
    """The athlete's named bpm bands in the same shape as `percent_zone_bounds`.

    `min_bpm`/`max_bpm` in the config are inclusive; internally a band becomes
    the half-open interval `[lo_edge, hi_edge)` so a fractional average (a 165.4
    bpm bin) lands in the band its whole-bpm floor belongs to. The open bottom
    reports `lo_bpm` 0 and the open top `hi_bpm` None.
    """
    raw = zones if zones is not None else load_training_zones(path)
    out = []
    for z in raw:
        lo, hi = z.get("min_bpm"), z.get("max_bpm")
        entry = {
            "zone": z["name"],
            "lo_bpm": 0 if lo is None else int(lo),
            "hi_bpm": None if hi is None else int(hi),
            "lo_edge": 0.0 if lo is None else float(lo),
            # max_bpm is inclusive, so the exclusive edge is one bpm higher.
            "hi_edge": float("inf") if hi is None else float(hi) + 1.0,
        }
        if z.get("description"):
            entry["description"] = z["description"]
        out.append(entry)
    return out


def zone_bounds(model: str = PERCENT_MODEL, hr_max: Optional[float] = None,
                zones: Optional[list[dict]] = None,
                path: Optional[Path] = None) -> list[dict]:
    """Bounds for either model. `hr_max` is required for `percent`."""
    if model == PERCENT_MODEL:
        if hr_max is None:
            raise ValueError("the percent zone model needs hr_max")
        return percent_zone_bounds(hr_max)
    if model == TRAINING_MODEL:
        return training_zone_bounds(zones, path)
    raise ValueError(f"unknown zone model {model!r} (expected one of {MODELS})")


# --- classification -----------------------------------------------------------

def classify(bpm: Optional[float], bounds: list[dict]) -> Optional[str]:
    """Zone name for one bpm value, or None when `bpm` is None.

    Bands are half-open `[lo_edge, hi_edge)`, and the answer is the band that
    *contains* the value — selected by range, never by position, so the result
    does not depend on the order `bounds` happens to arrive in. A value below
    every band's floor falls into the lowest band (the bottom band is open in
    both shipped models, so this only catches a nonsense reading).
    """
    if bpm is None:
        return None
    v = float(bpm)
    for b in bounds:
        if b["lo_edge"] <= v < b["hi_edge"]:
            return b["zone"]
    return min(bounds, key=lambda b: b["lo_edge"])["zone"]


def zone_case_sql(bounds: list[dict], col: str = "hr") -> str:
    """SQL CASE mapping an HR column to a zone label, for either model.

    Descending `lo_edge` comparisons with the lowest band as `ELSE`, so all time
    is accounted for. For the percentage model this reproduces the exact text
    `analytics.zone_case_sql` has always produced.
    """
    ordered = sorted(bounds, key=lambda b: b["lo_edge"], reverse=True)
    whens = " ".join(f"WHEN {col} >= {b['lo_edge']} THEN '{b['zone']}'"
                     for b in ordered[:-1])
    return f"CASE {whens} ELSE '{ordered[-1]['zone']}' END"


# --- time in zone -------------------------------------------------------------

def _empty_totals(bounds: list[dict]) -> dict[str, float]:
    return {b["zone"]: 0.0 for b in bounds}


def _assemble(secs: dict[str, float], bounds: list[dict], model: str) -> dict:
    """Shape the per-zone seconds into the tools' response payload."""
    total = sum(secs.values())
    zones = []
    for b in bounds:
        s = secs.get(b["zone"], 0.0)
        zones.append({
            "zone": b["zone"],
            "lo_bpm": b["lo_bpm"],
            "hi_bpm": b["hi_bpm"],
            "seconds": round(s, 1),
            "minutes": round(s / 60.0, 1),
            "share": round(s / total, 3) if total else 0.0,
        })
    return {
        "model": model,
        "zones": zones,
        "minutes_by_zone": {z["zone"]: z["minutes"] for z in zones},
        "total_seconds": round(total, 1),
        "total_minutes": round(total / 60.0, 1),
    }


def time_in_zones(samples: Iterable[Sequence[Any]], bounds: list[dict],
                  model: str = PERCENT_MODEL) -> dict:
    """Time in zone from `(bpm, seconds)` pairs.

    Samples with a null bpm are dropped (they carry no zone); a null duration
    counts as zero. Returns the same shape as `zone_time`, so the answer to
    "how many minutes were actually at threshold rather than in the grey zone"
    is one lookup in `minutes_by_zone` regardless of which model produced it.
    """
    secs = _empty_totals(bounds)
    for sample in samples:
        bpm, dt = sample[0], sample[1]
        zone = classify(bpm, bounds)
        if zone is None:
            continue
        secs[zone] = secs.get(zone, 0.0) + float(dt or 0.0)
    return _assemble(secs, bounds, model)


def series_samples(points: Iterable[dict], bin_seconds: float,
                   key: str = "hr") -> list[tuple[float, float]]:
    """`(bpm, seconds)` pairs from a binned series (`analytics.binned_series`).

    Each bin contributes its own width, so a series with gaps does not credit
    time that was never recorded.
    """
    out: list[tuple[float, float]] = []
    for p in points:
        v = p.get(key)
        if v is None:
            continue
        out.append((float(v), float(bin_seconds)))
    return out


def zone_time(q: Query, where_sql: str, params: tuple, bounds: list[dict],
              model: str = PERCENT_MODEL, cap_seconds: int = 60) -> dict:
    """Time-in-zone over the `heart_rate` rows selected by `where_sql`.

    Each sample is weighted by the gap to the next sample (`lead`), capped at
    `cap_seconds` so gaps between separate sessions don't inflate a zone —
    the same weighting the tools have always used, now driven by `bounds` so it
    serves either model. `where_sql` and `params` are the caller's (they already
    carry the date/scope filters); only `bounds` is inlined, and its zone names
    are validated identifier tokens.

    The last sample has no successor, so `lead` is NULL and it must weigh 0 —
    hence the `coalesce` INSIDE `least`, which is load-bearing. `least` ignores
    NULL arguments rather than propagating them, so the obvious
    `least(date_diff(...), cap)` returns the *cap* for that row: every call
    silently credited a full `cap_seconds` to whichever zone the final sample
    fell in. Feeding `least` a 0 instead of a NULL keeps the arithmetic out of
    `least`'s NULL handling entirely, so this does not depend on how a
    particular DuckDB build treats it.
    """
    sql = (
        "WITH hr AS ("
        "  SELECT value AS hr, "
        "    least(coalesce(date_diff('second', start_ts, "
        "      lead(start_ts) OVER (ORDER BY start_ts)), 0), ?) AS dt "
        f"  FROM records_dedup WHERE {where_sql}"
        ") "
        f"SELECT {zone_case_sql(bounds)} AS zone, "
        "sum(coalesce(dt, 0)) AS seconds "
        "FROM hr WHERE hr IS NOT NULL GROUP BY zone"
    )
    rows = q(sql, (cap_seconds, *params))
    secs = _empty_totals(bounds)
    for r in rows:
        secs[r["zone"]] = secs.get(r["zone"], 0.0) + float(r["seconds"] or 0.0)
    return _assemble(secs, bounds, model)
