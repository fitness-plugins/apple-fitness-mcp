"""Shared training-analytics helpers for the intraday workout tools.

These sit under `get_workout_detail`, `get_hr_zones`, and `get_training_load` in
`server.py`. Two kinds of helper live here:

- **DB helpers** take the read-only query function `q` (i.e. `server._q`) as their
  first argument. They reuse the existing data layer and open no new connection.
  `q(sql, params) -> list[dict]`.
- **Pure math** (zones, TRIMP, decoupling, ACWR, pace, cadence) take plain
  numbers/rows so they are testable without a database.

Units in the export (verified against real data): `running_speed` is **km/hr**,
`running_stride_length` metres, `distance_walking_running` km per sample,
`heart_rate` count/min, `running_power` W, `active_energy` kcal. Day/time
boundaries use the session timezone (the machine's local, +05), matching the
existing tools — no manual offset math.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

Query = Callable[[str, tuple], list[dict[str, Any]]]

# Full column list for a workout row (mirrors the workouts table order used by
# the existing get_workouts tool, plus row_hash so callers get the id back).
_WORKOUT_COLS = (
    "row_hash, type, source_name, device, duration, duration_unit, "
    "distance, distance_unit, energy, energy_unit, avg_hr, max_hr, "
    "start_ts, end_ts"
)

# Intraday record types used for the binned series. Output name -> record type.
SERIES_METRICS = {
    "hr": "heart_rate",
    "power": "running_power",
    "speed": "running_speed",
    "stride": "running_stride_length",
}

# Energy -> load scale for the TRIMP fallback. Calibrated so a typical logged run
# (energy in kcal) lands on roughly the same load scale as its Banister TRIMP:
# a ~67 min run at avg 182 bpm ≈ 172 TRIMP and burned ~916 kcal -> ~0.19.
ENERGY_LOAD_K = 0.19
# Non-exercise daily active energy (kcal) treated as baseline; only active energy
# above this counts as unlogged effort on days without a logged workout.
NONEXERCISE_BASELINE_KCAL = 400.0


# --- (a) workout selection ----------------------------------------------------

def select_workout(q: Query, workout_id: Optional[str] = None,
                   type: Optional[str] = None,
                   date: Optional[str] = None) -> Optional[dict]:
    """Return one workout row (all columns) or None.

    With `workout_id` (a row_hash) that exact workout is returned. Otherwise the
    most recent workout matching the optional `type` (ILIKE substring) and `date`
    (YYYY-MM-DD, on the local calendar) filters is chosen.
    """
    if workout_id:
        rows = q(f"SELECT {_WORKOUT_COLS} FROM workouts WHERE row_hash = ?",
                 (workout_id,))
        return rows[0] if rows else None
    clauses: list[str] = []
    params: list = []
    if type:
        clauses.append("type ILIKE ?")
        params.append(f"%{type}%")
    if date:
        clauses.append("start_ts::DATE = CAST(? AS DATE)")
        params.append(date)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = q(
        f"SELECT {_WORKOUT_COLS} FROM workouts{where} "
        "ORDER BY start_ts DESC LIMIT 1",
        tuple(params),
    )
    return rows[0] if rows else None


# --- (b) binned intraday series ----------------------------------------------

def binned_series(q: Query, start_ts: datetime, end_ts: datetime,
                  bin_seconds: int, metrics: dict[str, str]) -> list[dict]:
    """Bin `records_dedup` inside [start_ts, end_ts] into `bin_seconds` buckets.

    `metrics` maps an output column name to a record `type`; each becomes an
    averaged, NULL-when-absent column so missing metrics degrade gracefully.
    Buckets are aligned to `start_ts` (time_bucket origin) and reported as an
    integer second offset from it.
    """
    bin_seconds = max(1, int(bin_seconds))
    # Metric names/types are trusted code constants (SERIES_METRICS), so they are
    # safe to inline; only timestamps are parameterised.
    selects = ", ".join(
        f"avg(value) FILTER (WHERE type = '{rt}') AS {name}"
        for name, rt in metrics.items()
    )
    types_in = ", ".join(f"'{rt}'" for rt in metrics.values())
    sql = (
        f"SELECT date_diff('second', ?, "
        f"time_bucket(INTERVAL {bin_seconds} SECOND, start_ts, ?)) AS t_offset_sec, "
        f"{selects} "
        "FROM records_dedup "
        f"WHERE start_ts >= ? AND start_ts <= ? AND type IN ({types_in}) "
        "GROUP BY t_offset_sec ORDER BY t_offset_sec"
    )
    return q(sql, (start_ts, start_ts, start_ts, end_ts))


# --- (c) HR zones -------------------------------------------------------------

_ZONE_PCTS = (
    ("Z1", 0.50, 0.60),
    ("Z2", 0.60, 0.70),
    ("Z3", 0.70, 0.80),
    ("Z4", 0.80, 0.90),
    ("Z5", 0.90, 1.00),
)


def zone_bounds(max_hr: float) -> list[dict]:
    """Z1–Z5 nominal %-of-max bounds in bpm. Z1 also captures anything below 50%
    and Z5 anything at/above 90%, so every sample maps to a zone."""
    out = []
    for label, lo, hi in _ZONE_PCTS:
        out.append({
            "zone": label,
            "lo_pct": int(lo * 100),
            "hi_pct": int(hi * 100),
            "lo_bpm": round(max_hr * lo),
            "hi_bpm": round(max_hr * hi),
        })
    return out


def zone_case_sql(max_hr: float, col: str = "hr") -> str:
    """SQL CASE mapping an HR column to a zone label. Everything below 60% of max
    falls into Z1; at/above 90% into Z5 (so all time is accounted for)."""
    z90 = max_hr * 0.90
    z80 = max_hr * 0.80
    z70 = max_hr * 0.70
    z60 = max_hr * 0.60
    return (
        f"CASE WHEN {col} >= {z90} THEN 'Z5' "
        f"WHEN {col} >= {z80} THEN 'Z4' "
        f"WHEN {col} >= {z70} THEN 'Z3' "
        f"WHEN {col} >= {z60} THEN 'Z2' "
        "ELSE 'Z1' END"
    )


def zone_time(q: Query, where_sql: str, params: tuple, max_hr: float,
              cap_seconds: int = 60) -> dict:
    """Time-in-zone over the heart_rate rows selected by `where_sql`.

    Each sample is weighted by the gap to the next sample (`lead`), capped at
    `cap_seconds` so gaps between separate sessions don't inflate a zone. Returns
    an ordered Z1–Z5 list with seconds/minutes/share plus totals and bounds.
    """
    sql = (
        "WITH hr AS ("
        "  SELECT value AS hr, "
        "    least(date_diff('second', start_ts, "
        "      lead(start_ts) OVER (ORDER BY start_ts)), ?) AS dt "
        f"  FROM records_dedup WHERE {where_sql}"
        ") "
        f"SELECT {zone_case_sql(max_hr)} AS zone, "
        "sum(coalesce(dt, 0)) AS seconds "
        "FROM hr WHERE hr IS NOT NULL GROUP BY zone"
    )
    rows = q(sql, (cap_seconds, *params))
    secs = {r["zone"]: float(r["seconds"] or 0.0) for r in rows}
    total = sum(secs.values())
    bounds = {b["zone"]: b for b in zone_bounds(max_hr)}
    zones = []
    for label, _lo, _hi in _ZONE_PCTS:
        s = secs.get(label, 0.0)
        zones.append({
            "zone": label,
            "lo_bpm": bounds[label]["lo_bpm"],
            "hi_bpm": bounds[label]["hi_bpm"],
            "seconds": round(s, 1),
            "minutes": round(s / 60.0, 1),
            "share": round(s / total, 3) if total else 0.0,
        })
    return {"zones": zones, "total_seconds": round(total, 1),
            "total_minutes": round(total / 60.0, 1)}


# --- (d) max-HR / resting-HR resolution --------------------------------------

def resolve_max_hr(q: Query, max_hr: Optional[int]) -> tuple[int, str]:
    """(value, source). Passed value wins; else max(workouts.max_hr); else 190."""
    if max_hr is not None:
        return int(max_hr), "provided"
    rows = q("SELECT max(max_hr) AS m FROM workouts", ())
    m = rows[0]["m"] if rows else None
    if m:
        return int(round(m)), "estimated from workouts.max_hr"
    return 190, "default (no workout max_hr available)"


def resolve_resting_hr(q: Query, resting_hr: Optional[int],
                       start_date: Optional[str] = None,
                       end_date: Optional[str] = None) -> tuple[int, str]:
    """(value, source). Passed value wins; else avg(resting_heart_rate) in range
    (falling back to all-time if the range has none); else 60."""
    if resting_hr is not None:
        return int(resting_hr), "provided"
    clauses = ["type = 'resting_heart_rate'"]
    params: list = []
    if start_date:
        clauses.append("start_ts >= CAST(? AS TIMESTAMPTZ)")
        params.append(start_date)
    if end_date:
        clauses.append("start_ts < CAST(? AS TIMESTAMPTZ) + INTERVAL 1 DAY")
        params.append(end_date)
    where = " AND ".join(clauses)
    rows = q(f"SELECT avg(value) AS m FROM records_dedup WHERE {where}",
             tuple(params))
    m = rows[0]["m"] if rows else None
    if m:
        return int(round(m)), "estimated from resting_heart_rate (range)"
    rows = q("SELECT avg(value) AS m FROM records_dedup "
             "WHERE type = 'resting_heart_rate'", ())
    m = rows[0]["m"] if rows else None
    if m:
        return int(round(m)), "estimated from resting_heart_rate (all-time)"
    return 60, "default (no resting_heart_rate available)"


# --- pure math ----------------------------------------------------------------

def pace_min_per_km(speed_kmh: Optional[float]) -> Optional[float]:
    """Convert a running speed in km/h to pace in minutes per km."""
    if not speed_kmh or speed_kmh <= 0:
        return None
    return round(60.0 / speed_kmh, 2)


def cadence_spm(speed_kmh: Optional[float],
                stride_m: Optional[float]) -> Optional[float]:
    """Steps/min from speed (km/h) and stride length (m):
    steps/min = metres_per_min / stride = (speed_kmh * 1000/60) / stride."""
    if not speed_kmh or not stride_m or stride_m <= 0:
        return None
    metres_per_min = speed_kmh * 1000.0 / 60.0
    return round(metres_per_min / stride_m, 1)


def trimp(duration_min: float, avg_hr: Optional[float], rest_hr: float,
          max_hr: float) -> Optional[float]:
    """Banister TRIMP (male coefficients per spec):
    duration * HRr * 0.64 * e^(1.92*HRr), HRr = (avg-rest)/(max-rest) clamped
    to [0, 1]. Returns None if the HR reserve is undefined."""
    if avg_hr is None or max_hr <= rest_hr or not duration_min:
        return None
    hrr = (avg_hr - rest_hr) / (max_hr - rest_hr)
    hrr = min(1.0, max(0.0, hrr))
    return round(duration_min * hrr * 0.64 * math.exp(1.92 * hrr), 1)


def decoupling(ratio_first: Optional[float],
               ratio_second: Optional[float]) -> Optional[float]:
    """Aerobic decoupling / cardiac drift as a percentage:
    (second - first) / first * 100. None if the first-half ratio is unusable."""
    if not ratio_first or ratio_second is None:
        return None
    return round((ratio_second - ratio_first) / ratio_first * 100.0, 1)


def acwr(dates: list, loads: list[float], acute_days: int = 7,
         chronic_days: int = 28) -> list[dict]:
    """Acute:chronic workload ratio over a contiguous daily load series.

    acute = sum of the last `acute_days` (incl. today); chronic = sum of the last
    `chronic_days` expressed as a weekly-equivalent (/ (chronic_days/acute_days)).
    Flags: 'sweet_spot' 0.8–1.3, 'elevated' > 1.5, else 'low'/'moderate'.
    `dates`/`loads` must be day-contiguous and equal length.
    """
    out = []
    scale = chronic_days / acute_days
    for i in range(len(loads)):
        acute = sum(loads[max(0, i - acute_days + 1): i + 1])
        chronic_window = loads[max(0, i - chronic_days + 1): i + 1]
        chronic = sum(chronic_window) / scale
        ratio = round(acute / chronic, 2) if chronic > 0 else None
        enough = i >= chronic_days - 1
        if ratio is None:
            flag = "no_load"
        elif not enough:
            flag = "insufficient_history"
        elif ratio > 1.5:
            flag = "elevated"
        elif ratio < 0.8:
            flag = "low"
        elif ratio <= 1.3:
            flag = "sweet_spot"
        else:
            flag = "moderate"
        out.append({
            "date": str(dates[i]),
            "acute_7d": round(acute, 1),
            "chronic_28d_weekly": round(chronic, 1),
            "acwr": ratio,
            "flag": flag,
        })
    return out


def daily_date_range(start: datetime, end: datetime) -> list:
    """Inclusive list of date objects from start.date() to end.date()."""
    d0 = start.date() if isinstance(start, datetime) else start
    d1 = end.date() if isinstance(end, datetime) else end
    days = (d1 - d0).days
    return [d0 + timedelta(days=i) for i in range(days + 1)]
