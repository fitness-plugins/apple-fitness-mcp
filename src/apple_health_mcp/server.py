"""FastMCP server exposing read-only tools over the local Apple Health DuckDB.

Every tool is annotated read-only. Data never leaves the machine: these tools
run queries against a local DuckDB file and return rows to the Claude Desktop
client over stdio.
"""
from __future__ import annotations

import math
import re
from datetime import timedelta
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import analytics, config, import_pipeline, scoring, storage

mcp = FastMCP("apple-health")

RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
# reload_data is the one tool that writes (it imports new data). It only ever
# *adds* records (idempotent, never deletes), so it is non-destructive.
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False,
                        idempotentHint=True, openWorldHint=False)

# Metrics that should be summed over a period (counts/energy/distance); the rest
# default to averaging (heart rate, weight, hrv, vo2max, ...).
_SUM_METRICS = {
    "step_count", "active_energy", "basal_energy", "flights_climbed",
    "exercise_time", "stand_time", "dietary_energy", "water",
    "distance_walking_running", "distance_cycling", "distance_swimming",
}
_GRAN = {"day": "day", "week": "week", "month": "month"}


_schema_ready = False


def _ensure_ready() -> None:
    """Guarantee the schema exists so even a brand-new/empty DB is queryable.

    Runs the read-write init at most once per process: doing it on every tool
    call would repeatedly grab the write lock and can conflict with a read-only
    connection held elsewhere in the same process (DuckDB forbids mixing
    read-write and read-only handles to one file within a process).
    """
    global _schema_ready
    if _schema_ready:
        return
    con = storage.connect()
    try:
        storage.init_schema(con)
    finally:
        con.close()
    _schema_ready = True


def _q(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    """Run a read-only query and return a list of dict rows."""
    con = storage.connect_readonly()
    try:
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        con.close()


def _date_filter(col: str, start_date: Optional[str], end_date: Optional[str],
                 params: list) -> str:
    clauses = []
    if start_date:
        clauses.append(f"{col} >= CAST(? AS TIMESTAMPTZ)")
        params.append(start_date)
    if end_date:
        clauses.append(f"{col} < CAST(? AS TIMESTAMPTZ) + INTERVAL 1 DAY")
        params.append(end_date)
    return (" WHERE " + " AND ".join(clauses)) if clauses else ""


@mcp.tool(annotations=RO,
          description="List every available metric with its record count, "
                      "unit(s), and the date range covered.")
def list_metrics() -> dict:
    _ensure_ready()
    rows = _q(
        "SELECT type, count(*) AS records, "
        "min(start_ts)::DATE AS first_day, max(start_ts)::DATE AS last_day, "
        "string_agg(DISTINCT unit, ', ') AS units "
        "FROM records GROUP BY type ORDER BY records DESC"
    )
    workouts = _q("SELECT count(*) AS workouts, min(start_ts)::DATE AS first_day, "
                  "max(start_ts)::DATE AS last_day FROM workouts")
    sleep = _q("SELECT count(*) AS sleep_segments, min(start_ts)::DATE AS first_day, "
               "max(start_ts)::DATE AS last_day FROM sleep")
    return {
        "metrics": rows,
        "workouts": workouts[0] if workouts else {},
        "sleep": sleep[0] if sleep else {},
        "note": "Empty result means no export has been imported yet.",
    }


@mcp.tool(annotations=RO,
          description="Aggregated statistics for a metric grouped by day, week, "
                      "or month. Uses the deduplicated records view.")
def get_summary(metric: str, start_date: Optional[str] = None,
                end_date: Optional[str] = None, granularity: str = "day") -> dict:
    _ensure_ready()
    gran = _GRAN.get(granularity, "day")
    params: list = [metric]
    where = " WHERE type = ?"
    extra = _date_filter("start_ts", start_date, end_date, params)
    if extra:
        where += extra.replace(" WHERE ", " AND ")
    agg = "sum(value)" if metric in _SUM_METRICS else "avg(value)"
    primary = "total" if metric in _SUM_METRICS else "average"
    rows = _q(
        f"SELECT date_trunc('{gran}', start_ts)::DATE AS period, "
        f"count(*) AS samples, {agg} AS {primary}, "
        "min(value) AS min, max(value) AS max, avg(value) AS avg, sum(value) AS sum "
        f"FROM records_dedup{where} GROUP BY period ORDER BY period",
        tuple(params),
    )
    return {"metric": metric, "granularity": gran, "primary": primary,
            "periods": rows}


def _series(metric: str, start_date, end_date, agg="avg", limit=5000) -> dict:
    _ensure_ready()
    params: list = [metric]
    where = " WHERE type = ?"
    extra = _date_filter("start_ts", start_date, end_date, params)
    if extra:
        where += extra.replace(" WHERE ", " AND ")
    daily = "sum(value)" if agg == "sum" else "avg(value)"
    rows = _q(
        f"SELECT start_ts::DATE AS day, {daily} AS value, count(*) AS samples, "
        "min(value) AS min, max(value) AS max, any_value(unit) AS unit "
        f"FROM records_dedup{where} GROUP BY day ORDER BY day LIMIT {int(limit)}",
        tuple(params),
    )
    return {"metric": metric, "daily": rows}


@mcp.tool(annotations=RO, description="Daily step totals over an optional range.")
def get_steps(start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> dict:
    return _series("step_count", start_date, end_date, agg="sum")


@mcp.tool(annotations=RO, description="Daily heart-rate stats (bpm).")
def get_heart_rate(start_date: Optional[str] = None,
                   end_date: Optional[str] = None) -> dict:
    return _series("heart_rate", start_date, end_date, agg="avg")


@mcp.tool(annotations=RO,
          description="Raw individual heart-rate readings (bpm), one row per "
                      "sample, sorted by timestamp — no aggregation (use "
                      "get_heart_rate for daily avg/min/max). Optional "
                      "start_date/end_date select a single day or a wider range; "
                      "readings are deduplicated by source. Capped at `limit` "
                      "rows (default 2000, oldest-first); a dense day can hold "
                      "thousands of samples, so narrow the range or raise limit "
                      "if truncated.")
def get_heart_rate_raw(start_date: Optional[str] = None,
                       end_date: Optional[str] = None,
                       limit: int = 2000) -> dict:
    _ensure_ready()
    params: list = []
    where = _date_filter("start_ts", start_date, end_date, params)
    where = (where + " AND " if where else " WHERE ") + "type = 'heart_rate'"
    lim = max(1, min(int(limit), 20000))
    rows = _q(
        "SELECT start_ts, round(value, 0) AS bpm, source_name "
        f"FROM records_dedup{where} ORDER BY start_ts LIMIT {lim + 1}",
        tuple(params),
    )
    truncated = len(rows) > lim
    rows = rows[:lim]
    note = "Raw per-sample heart rate, sorted by time."
    if truncated:
        note += (f" Truncated to {lim} rows — narrow the date range or raise "
                 "limit to see the rest.")
    return {"count": len(rows), "truncated": truncated, "readings": rows,
            "note": note}


@mcp.tool(annotations=RO, description="Daily heart-rate variability (HRV SDNN, ms).")
def get_hrv(start_date: Optional[str] = None,
            end_date: Optional[str] = None) -> dict:
    return _series("hrv", start_date, end_date, agg="avg")


@mcp.tool(annotations=RO, description="Daily body weight (kg or lb per source).")
def get_weight(start_date: Optional[str] = None,
               end_date: Optional[str] = None) -> dict:
    return _series("weight", start_date, end_date, agg="avg")


@mcp.tool(annotations=RO, description="VO2 max measurements over an optional range.")
def get_vo2max(start_date: Optional[str] = None,
               end_date: Optional[str] = None) -> dict:
    return _series("vo2max", start_date, end_date, agg="avg")


@mcp.tool(annotations=RO,
          description="Nightly sleep broken down by stage (in_bed, core, deep, "
                      "rem, awake) with total hours asleep. Each night is dated "
                      "by the WAKE day, so get_sleep for a date returns the "
                      "sleep you woke from that day. start_date/end_date filter "
                      "on that same wake-day, inclusive.")
def get_sleep(start_date: Optional[str] = None,
              end_date: Optional[str] = None) -> dict:
    _ensure_ready()
    # Attribute each segment to the "sleep day" = the day you wake up. A sleep
    # day runs 18:00 -> 18:00, so an evening's pre-midnight sleep and the next
    # morning's sleep share the same (wake) date. Shifting +6h makes the day
    # boundary fall at 18:00. CRITICAL: filter and group on the SAME expression,
    # otherwise a date query returns a differently-labelled night.
    night_expr = "(start_ts + INTERVAL 6 HOUR)::DATE"
    params: list = []
    clauses = []
    if start_date:
        clauses.append(f"{night_expr} >= CAST(? AS DATE)")
        params.append(start_date)
    if end_date:
        clauses.append(f"{night_expr} <= CAST(? AS DATE)")
        params.append(end_date)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    rows = _q(
        f"SELECT {night_expr} AS night, stage, "
        "round(sum(date_diff('second', start_ts, end_ts)) / 3600.0, 2) AS hours "
        f"FROM sleep{where} GROUP BY night, stage ORDER BY night, stage",
        tuple(params),
    )
    nights: dict[str, dict] = {}
    for r in rows:
        n = str(r["night"])
        nights.setdefault(n, {"night": n, "stages": {}, "hours_asleep": 0.0})
        nights[n]["stages"][r["stage"]] = r["hours"]
        if r["stage"] in ("asleep", "core", "deep", "rem"):
            nights[n]["hours_asleep"] += r["hours"]
    return {"nights": list(nights.values())}


@mcp.tool(annotations=RO,
          description="Workouts in a date range, optionally filtered by activity "
                      "type (e.g. running, cycling, walking).")
def get_workouts(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 type: Optional[str] = None) -> dict:
    _ensure_ready()
    params: list = []
    where = _date_filter("start_ts", start_date, end_date, params)
    if type:
        where = (where + " AND " if where else " WHERE ") + "type ILIKE ?"
        params.append(f"%{type}%")
    rows = _q(
        "SELECT start_ts, type, "
        "round(duration, 1) AS duration, duration_unit, "
        "round(distance, 3) AS distance, distance_unit, "
        "round(energy, 1) AS energy, energy_unit, "
        "round(avg_hr, 0) AS avg_hr, round(max_hr, 0) AS max_hr, source_name "
        f"FROM workouts{where} ORDER BY start_ts DESC LIMIT 1000",
        tuple(params),
    )
    return {"count": len(rows), "workouts": rows}


# --- intraday training analytics ------------------------------------------------


@mcp.tool(annotations=RO,
          description="Full intraday breakdown of one workout: binned HR / power "
                      "/ speed (pace) / cadence series, time in HR zones, aerobic "
                      "decoupling (cardiac drift), and per-km splits. Picks the "
                      "workout by workout_id (row_hash), or the most recent one "
                      "matching optional type/date filters. max_hr defaults to the "
                      "athlete's observed max across all workouts. HR zones: "
                      "Z1 <60%, Z2 60-70%, Z3 70-80%, Z4 80-90%, Z5 >=90% of "
                      "max_hr. Metrics absent from a given workout (e.g. power on "
                      "a walk) degrade to null.")
def get_workout_detail(workout_id: Optional[str] = None,
                       type: Optional[str] = None, date: Optional[str] = None,
                       bin_seconds: int = 30,
                       max_hr: Optional[int] = None) -> dict:
    _ensure_ready()
    w = analytics.select_workout(_q, workout_id=workout_id, type=type, date=date)
    if not w:
        return {"error": "No matching workout found.",
                "note": "Adjust workout_id/type/date, or import data first."}
    hr_max, hr_src = analytics.resolve_max_hr(_q, max_hr)
    start, end = w["start_ts"], w["end_ts"]

    # 2. binned series with derived pace + cadence.
    points = analytics.binned_series(_q, start, end, bin_seconds,
                                     analytics.SERIES_METRICS)
    for p in points:
        p["pace_min_per_km"] = analytics.pace_min_per_km(p.get("speed"))
        p["cadence_spm"] = analytics.cadence_spm(p.get("speed"), p.get("stride"))
        for k in ("hr", "power", "speed", "stride"):
            if p.get(k) is not None:
                p[k] = round(p[k], 1)

    # 3. HR zones over the window.
    zones = analytics.zone_time(
        _q, "type = 'heart_rate' AND start_ts >= ? AND start_ts <= ?",
        (start, end), hr_max)

    # 4. decoupling: split the window in half, HR-to-power (or HR-to-speed) drift.
    mid = start + (end - start) / 2
    halves = _q(
        "SELECT CASE WHEN start_ts < ? THEN 1 ELSE 2 END AS half, "
        "avg(value) FILTER (WHERE type = 'heart_rate') AS hr, "
        "avg(value) FILTER (WHERE type = 'running_power') AS power, "
        "avg(value) FILTER (WHERE type = 'running_speed') AS speed "
        "FROM records_dedup WHERE start_ts >= ? AND start_ts <= ? "
        "GROUP BY half ORDER BY half",
        (mid, start, end))
    decoupling = _decoupling_from_halves(halves)

    # 5. per-km splits from cumulative running distance.
    splits = _km_splits(start, end)

    summary = {
        "workout_id": w["row_hash"],
        "type": w["type"],
        "start_ts": w["start_ts"], "end_ts": w["end_ts"],
        "duration_min": round(w["duration"], 1) if w["duration"] else None,
        "distance_km": round(w["distance"], 3) if w["distance"] else None,
        "energy_kcal": round(w["energy"], 1) if w["energy"] else None,
        "avg_hr": round(w["avg_hr"], 0) if w["avg_hr"] else None,
        "max_hr": round(w["max_hr"], 0) if w["max_hr"] else None,
        "source_name": w["source_name"],
        "max_hr_used": hr_max, "max_hr_source": hr_src,
    }
    return {
        "summary": summary,
        "series": {"bin_seconds": bin_seconds, "count": len(points),
                   "points": points},
        "hr_zones": zones,
        "decoupling": decoupling,
        "splits": splits,
        "note": f"max_hr {hr_max} bpm ({hr_src}). Missing metrics are null "
                "(e.g. no power/cadence when the device didn't record them).",
    }


def _decoupling_from_halves(halves: list[dict]) -> dict:
    """Aerobic decoupling from the two half-window aggregate rows."""
    by_half = {r["half"]: r for r in halves}
    h1, h2 = by_half.get(1), by_half.get(2)
    if not h1 or not h2:
        return {"drift_pct": None,
                "note": "Not enough data in both halves to compute drift."}
    # Prefer HR:power (efficiency); fall back to HR:speed when power is absent.
    if h1.get("power") and h2.get("power"):
        basis = "hr_to_power"
        r1 = h1["hr"] / h1["power"] if h1.get("hr") else None
        r2 = h2["hr"] / h2["power"] if h2.get("hr") else None
    else:
        basis = "hr_to_speed"
        r1 = h1["hr"] / h1["speed"] if h1.get("hr") and h1.get("speed") else None
        r2 = h2["hr"] / h2["speed"] if h2.get("hr") and h2.get("speed") else None
    drift = analytics.decoupling(r1, r2)
    return {
        "drift_pct": drift,
        "basis": basis,
        "first_half_ratio": round(r1, 4) if r1 else None,
        "second_half_ratio": round(r2, 4) if r2 else None,
        "good_aerobic_control": (drift is not None and drift < 5),
    }


def _km_splits(start, end) -> list[dict]:
    """Per-kilometre splits from cumulative distance_walking_running samples.

    Each sample carries a distance delta (km); the running total assigns each
    sample to a km bucket. Split time is the gap between successive buckets'
    first samples (last split runs to the workout end)."""
    rows = _q(
        "WITH d AS ("
        "  SELECT start_ts, value, "
        "    sum(value) OVER (ORDER BY start_ts) AS cum_km "
        "  FROM records_dedup "
        "  WHERE type = 'distance_walking_running' "
        "    AND start_ts >= ? AND start_ts <= ?"
        ") "
        # round before ceil so a float artifact at an exact km boundary (e.g.
        # cum_km = 2.0000000004) doesn't spawn a spurious trailing split.
        # dist_km = distance actually covered in the bucket (sum of sample deltas).
        "SELECT cast(ceil(round(cum_km, 6)) AS INT) AS km, "
        "min(start_ts) AS t_start, sum(value) AS dist_km, sum(1) AS samples "
        "FROM d WHERE cum_km > 0 GROUP BY km ORDER BY km",
        (start, end))
    if not rows:
        return []
    splits = []
    last = len(rows) - 1
    for i, r in enumerate(rows):
        t0 = r["t_start"]
        t1 = rows[i + 1]["t_start"] if i + 1 < len(rows) else end
        dur = (t1 - t0).total_seconds()
        dur_min = dur / 60.0
        dist = r["dist_km"] or 0.0
        split = {"km": r["km"], "duration_sec": round(dur, 1)}
        # A trailing partial km would misreport pace if elapsed minutes were shown
        # as if a full km; scale by the real distance and flag it.
        if i == last and dist < 0.95:
            split["distance_km"] = round(dist, 3)
            split["partial"] = True
            split["pace_min_per_km"] = (round(dur_min / dist, 2)
                                        if dist > 0 and dur else None)
        else:
            split["pace_min_per_km"] = round(dur_min, 2) if dur else None
        splits.append(split)
    return splits


@mcp.tool(annotations=RO,
          description="Time spent in heart-rate zones Z1-Z5 aggregated over a "
                      "period (not a single workout). scope='workouts' counts "
                      "only HR recorded inside logged workout windows; "
                      "scope='all' counts every heart_rate sample in range. "
                      "max_hr defaults to the observed workout max. HR zones: "
                      "Z1 <60%, Z2 60-70%, Z3 70-80%, Z4 80-90%, Z5 >=90% of "
                      "max_hr (Z1 has no lower gap, so scope='all' totals include "
                      "rest/sleep HR).")
def get_hr_zones(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 max_hr: Optional[int] = None, scope: str = "workouts") -> dict:
    _ensure_ready()
    hr_max, hr_src = analytics.resolve_max_hr(_q, max_hr)
    clauses = ["type = 'heart_rate'"]
    params: list = []
    rng = _date_filter("start_ts", start_date, end_date, params)
    if rng:
        clauses.append(rng.replace(" WHERE ", ""))
    if scope == "workouts":
        clauses.append(
            "EXISTS (SELECT 1 FROM workouts w "
            "WHERE records_dedup.start_ts BETWEEN w.start_ts AND w.end_ts)")
    where_sql = " AND ".join(clauses)
    zones = analytics.zone_time(_q, where_sql, tuple(params), hr_max)
    return {
        "scope": scope,
        "max_hr_used": hr_max, "max_hr_source": hr_src,
        **zones,
        "note": f"max_hr {hr_max} bpm ({hr_src}). Time weighted by gaps between "
                "samples, capped at 60s. scope='all' includes non-workout HR.",
    }


@mcp.tool(annotations=RO,
          description="Daily training load and acute:chronic workload ratio "
                      "(ACWR) over a period. Load is Banister TRIMP from a "
                      "workout's HR reserve where avg_hr exists, otherwise an "
                      "active-energy proxy so unlogged effort still counts. ACWR "
                      "= 7-day acute load vs 28-day chronic; 0.8-1.3 is the "
                      "sweet spot, >1.5 flags elevated injury risk. max_hr / "
                      "resting_hr default to observed values.")
def get_training_load(start_date: Optional[str] = None,
                      end_date: Optional[str] = None, max_hr: Optional[int] = None,
                      resting_hr: Optional[int] = None) -> dict:
    _ensure_ready()
    hr_max, hr_src = analytics.resolve_max_hr(_q, max_hr)
    rest, rest_src = analytics.resolve_resting_hr(_q, resting_hr)

    # Seed the ACWR rolling windows with the 28 days BEFORE start_date so the
    # 7-/28-day averages for the first displayed day reflect real prior load
    # rather than starting from zero. Load is still computed the same way, just
    # over an extended [calc_start, end_date] range.
    calc_start = start_date
    if start_date:
        calc_start = (_to_dt(start_date).date()
                      - timedelta(days=28)).isoformat()
    daily_ext = _training_daily(calc_start, end_date, hr_max, rest)

    acwr_all: list[dict] = []
    if daily_ext:
        first = min(d["date"] for d in daily_ext)
        last = max(d["date"] for d in daily_ext)
        span = analytics.daily_date_range(_to_dt(first), _to_dt(last))
        load_by_day = {d["date"]: d["load"] for d in daily_ext}
        dates = [str(d) for d in span]
        loads = [load_by_day.get(str(d), 0.0) for d in span]
        # Flag insufficient_history against the dataset's true first day, not the
        # (possibly recent) query start.
        acwr_all = analytics.acwr(dates, loads,
                                  min_history_date=_dataset_start())

    # Emit only the display range; acute/chronic already reflect the lookback.
    def _in_range(day: str) -> bool:
        return ((start_date is None or day >= start_date)
                and (end_date is None or day <= end_date))

    daily = [d for d in daily_ext if _in_range(d["date"])]
    acwr_series = [a for a in acwr_all if _in_range(a["date"])]
    latest = acwr_series[-1] if acwr_series else None
    return {
        "max_hr_used": hr_max, "max_hr_source": hr_src,
        "resting_hr_used": rest, "resting_hr_source": rest_src,
        "daily": daily,
        "acwr": acwr_series,
        "latest_acwr": latest,
        "note": f"TRIMP (Banister, male coeffs) uses max_hr {hr_max}, resting "
                f"{rest}. Days without HR use an active-energy proxy "
                f"(kcal above {int(analytics.NONEXERCISE_BASELINE_KCAL)} baseline "
                f"x {analytics.ENERGY_LOAD_K}).",
    }


def _to_dt(date_str: str):
    from datetime import datetime as _d
    return _d.fromisoformat(str(date_str))


def _dataset_start():
    """Earliest day the dataset carries any load-bearing data (active_energy or a
    workout). Used so ACWR only flags insufficient_history genuinely early in the
    whole dataset, not merely early in a query window."""
    rows = _q(
        "SELECT min(day) AS d FROM ("
        "  SELECT min(start_ts)::DATE AS day FROM records_dedup "
        "    WHERE type = 'active_energy' "
        "  UNION ALL SELECT min(start_ts)::DATE FROM workouts"
        ") t")
    d = rows[0]["d"] if rows else None
    return d  # a datetime.date, or None on an empty database


def _training_daily(start_date, end_date, hr_max, rest) -> list[dict]:
    """Per-day training load: TRIMP from workouts (energy proxy when HR absent),
    plus an active-energy proxy on days with no logged workout."""
    params: list = []
    where = _date_filter("start_ts", start_date, end_date, params)
    wk = _q(
        "SELECT start_ts::DATE AS day, duration, avg_hr, energy "
        f"FROM workouts{where} ORDER BY day",
        tuple(params))
    by_day: dict[str, dict] = {}
    workout_days: set[str] = set()
    for w in wk:
        day = str(w["day"])
        workout_days.add(day)
        load = analytics.trimp(w["duration"] or 0, w["avg_hr"], rest, hr_max)
        method = "trimp"
        if load is None:
            load = round((w["energy"] or 0) * analytics.ENERGY_LOAD_K, 1)
            method = "energy_proxy"
        d = by_day.setdefault(day, {"date": day, "load": 0.0, "workouts": 0,
                                    "methods": set()})
        d["load"] += load
        d["workouts"] += 1
        d["methods"].add(method)

    # Days with no workout but meaningful active energy -> unlogged-effort proxy.
    eparams: list = []
    ewhere = _date_filter("start_ts", start_date, end_date, eparams)
    eclause = " AND type = 'active_energy'" if ewhere else \
        " WHERE type = 'active_energy'"
    energy_rows = _q(
        "SELECT start_ts::DATE AS day, sum(value) AS kcal "
        f"FROM records_dedup{ewhere}{eclause} GROUP BY day ORDER BY day",
        tuple(eparams))
    for e in energy_rows:
        day = str(e["day"])
        if day in workout_days:
            continue
        extra = (e["kcal"] or 0) - analytics.NONEXERCISE_BASELINE_KCAL
        if extra <= 0:
            continue
        load = round(extra * analytics.ENERGY_LOAD_K, 1)
        by_day[day] = {"date": day, "load": load, "workouts": 0,
                       "methods": {"energy_proxy"}}

    out = []
    for day in sorted(by_day):
        d = by_day[day]
        methods = d["methods"]
        method = next(iter(methods)) if len(methods) == 1 else "mixed"
        out.append({"date": d["date"], "load": round(d["load"], 1),
                    "workouts": d["workouts"], "method": method})
    return out


# --- recovery & readiness -------------------------------------------------------

# Recovery physiological metrics: sub-score name -> underlying record type.
_RECOVERY_METRICS = {
    "hrv": "hrv",
    "sleeping_hr": "heart_rate",
    "resp": "respiratory_rate",
    "temp": "sleeping_wrist_temperature",
}
# Per-metric extraction strategy (a data-layer concern, not scoring math):
#   "sleep_window"    — average the metric strictly inside each night's sleep span,
#     so daytime activity doesn't pollute the baseline (HRV/SDNN, resp rate, wrist
#     temp are sampled through the night).
#   "stage_restricted" — our own sleeping HR: the lowest *sustained* 5-min window of
#     heart_rate taken only from core/deep/rem segments (NOT Apple's opaque daily
#     resting_heart_rate, and NOT a raw p5 over the wake-to-wake window that an
#     awakening or gap would corrupt). See _sleeping_hr_series.
_METRIC_SOURCE = {
    "hrv": "sleep_window",
    "sleeping_hr": "stage_restricted",
    "resp": "sleep_window",
    "temp": "sleep_window",
}
# Sleeping-HR extraction knobs (data-layer, calibrated to Apple's ~1 sample / 5 min
# overnight cadence). The rolling window smooths single low outliers; the gate
# keeps a thin/gappy night from polluting the baseline.
SLEEPING_HR_WINDOW_MIN = 5           # rolling-average window (minutes, forward)
SLEEPING_HR_MIN_SAMPLES_PER_WINDOW = 2  # a window needs >= this many samples
SLEEPING_HR_MIN_ASLEEP_H = 3.0       # gate: >= this many hours in core/deep/rem
SLEEPING_HR_MIN_VALID_WINDOWS = 5    # gate: >= this many valid rolling windows
SLEEPING_HR_FALLBACK_PCT = 0.05      # p5 when only generic 'asleep' staging exists
# The wake-day expression shared with get_sleep (18:00->18:00 boundary); nights
# line up with that tool so a recovery date matches the sleep you woke from.
_NIGHT_EXPR = "(start_ts + INTERVAL 6 HOUR)::DATE"
# Sleep stages that count as the actual asleep window (in_bed brackets it).
_ASLEEP_STAGES = ("asleep", "core", "deep", "rem")
# Baseline windowing / confidence thresholds. The canonical values live on
# scoring.ScoringConfig (single source of truth, mirrored in Swift); these names
# are kept as aliases so the orchestration reads the same knobs the math does.
#   BASELINE_WINDOW_DAYS — trailing baseline window per metric, excluding today.
#   MIN_HISTORY_DAYS     — days of history before state leaves 'calibrating'.
#   MIN_BASELINE_N       — baseline samples before a z is trusted; below it a
#                          scored metric is flagged low_confidence (finer-grained
#                          than the state field, which it does not affect).
BASELINE_WINDOW_DAYS = scoring.DEFAULT_CONFIG.baseline_window_days
MIN_HISTORY_DAYS = scoring.DEFAULT_CONFIG.min_history_days
MIN_BASELINE_N = scoring.DEFAULT_CONFIG.min_baseline_n
# Bedtime-regularity lookback (nights) and recovery-time heuristic constants.
REGULARITY_WINDOW_NIGHTS = 14
RECOVERY_TIME_PER_TRIMP_H = 0.25   # recovery hours a hard session "owes"
RECOVERY_TIME_MAX_H = 48.0


def _night_metric_series(metric_type: str, start_night: str,
                         end_night: str) -> list[dict]:
    """Per-night average of `metric_type` inside each night's sleep window.

    Nights are the sleep table's wake-days; the window is that night's earliest
    sleep start to its latest sleep end. Nights without the metric come back with
    value NULL (LEFT JOIN) so callers see the gap. `start_night`/`end_night` are
    inclusive wake-day bounds (YYYY-MM-DD).
    """
    asleep_in = ", ".join(f"'{s}'" for s in _ASLEEP_STAGES + ("in_bed",))
    rows = _q(
        "WITH nights AS ("
        f"  SELECT {_NIGHT_EXPR} AS night, "
        "    min(start_ts) AS win_start, max(end_ts) AS win_end "
        f"  FROM sleep WHERE stage IN ({asleep_in}) "
        f"  GROUP BY night HAVING night >= CAST(? AS DATE) "
        "    AND night <= CAST(? AS DATE)"
        ") "
        "SELECT n.night AS night, avg(r.value) AS value, "
        "count(r.value) AS samples "
        "FROM nights n LEFT JOIN records_dedup r "
        "  ON r.type = ? AND r.start_ts >= n.win_start "
        "  AND r.start_ts <= n.win_end "
        "GROUP BY n.night ORDER BY n.night",
        (start_night, end_night, metric_type))
    return rows


def _sleeping_hr_series(start_night: str, end_night: str) -> list[dict]:
    """Per-night sleeping heart rate: the lowest *sustained* window of overnight
    heart_rate, robust to awakenings and gaps by construction.

    Method (per wake-day night, inclusive bounds):
      1. Stage restriction — keep only heart_rate samples inside core/deep/rem
         segments (awake / in_bed excluded), so a mid-night awakening drops out.
      2. Sustained minimum — a forward SLEEPING_HR_WINDOW_MIN rolling average over
         the retained samples; the night's value is the MIN over windows holding
         >= SLEEPING_HR_MIN_SAMPLES_PER_WINDOW samples (a lone low blip can't win).
      3. Validity gate — the night must have >= SLEEPING_HR_MIN_ASLEEP_H hours in
         core/deep/rem AND >= SLEEPING_HR_MIN_VALID_WINDOWS valid windows, else its
         value is None (it must not pollute the baseline).
      4. Fallback — a night with only generic 'asleep' staging (no core/deep/rem)
         uses p5 of heart_rate over the asleep segments, tagged p5_fallback.

    Each row: {night, value, source, asleep_hours, valid_windows}. `source` is
    'stage_restricted' or 'p5_fallback'; value is None when a night is gated out.
    """
    win = int(SLEEPING_HR_WINDOW_MIN)
    minspw = int(SLEEPING_HR_MIN_SAMPLES_PER_WINDOW)
    staged = _q(
        "WITH seg AS ("
        f"  SELECT {_NIGHT_EXPR} AS night, start_ts AS s, end_ts AS e, "
        "    date_diff('second', start_ts, end_ts) AS dur "
        "  FROM sleep WHERE stage IN ('core','deep','rem')"
        "), "
        "asleep AS ("
        "  SELECT night, sum(dur) / 3600.0 AS asleep_h FROM seg "
        "  WHERE night >= CAST(? AS DATE) AND night <= CAST(? AS DATE) "
        "  GROUP BY night"
        "), "
        "hr AS ("
        "  SELECT seg.night AS night, r.start_ts AS ts, r.value AS v "
        "  FROM records_dedup r JOIN seg "
        "    ON r.start_ts >= seg.s AND r.start_ts < seg.e "
        "  WHERE r.type = 'heart_rate' "
        "    AND seg.night >= CAST(? AS DATE) AND seg.night <= CAST(? AS DATE)"
        "), "
        "roll AS ("
        "  SELECT night, avg(v) OVER w AS win_avg, count(*) OVER w AS win_n "
        "  FROM hr "
        f"  WINDOW w AS (PARTITION BY night ORDER BY ts "
        f"    RANGE BETWEEN CURRENT ROW AND INTERVAL {win} MINUTE FOLLOWING)"
        ") "
        "SELECT a.night AS night, a.asleep_h AS asleep_h, "
        f"  min(roll.win_avg) FILTER (WHERE roll.win_n >= {minspw}) AS shr, "
        f"  count(*) FILTER (WHERE roll.win_n >= {minspw}) AS valid_windows "
        "FROM asleep a LEFT JOIN roll ON roll.night = a.night "
        "GROUP BY a.night, a.asleep_h ORDER BY a.night",
        (start_night, end_night, start_night, end_night))

    out: list[dict] = []
    for r in staged:
        asleep_h = r["asleep_h"] or 0.0
        vw = r["valid_windows"] or 0
        valid = (r["shr"] is not None
                 and asleep_h >= SLEEPING_HR_MIN_ASLEEP_H
                 and vw >= SLEEPING_HR_MIN_VALID_WINDOWS)
        out.append({
            "night": r["night"],
            "value": r["shr"] if valid else None,
            "source": "stage_restricted",
            "asleep_hours": asleep_h,
            "valid_windows": vw,
        })

    # Fallback: nights with only generic 'asleep' staging (no core/deep/rem).
    fb = _q(
        "WITH staged AS ("
        f"  SELECT DISTINCT {_NIGHT_EXPR} AS night FROM sleep "
        "  WHERE stage IN ('core','deep','rem')"
        "), "
        "aseg AS ("
        f"  SELECT {_NIGHT_EXPR} AS night, start_ts AS s, end_ts AS e, "
        "    date_diff('second', start_ts, end_ts) AS dur "
        "  FROM sleep WHERE stage = 'asleep'"
        "), "
        "asleep AS ("
        "  SELECT night, sum(dur) / 3600.0 AS asleep_h FROM aseg "
        "  WHERE night >= CAST(? AS DATE) AND night <= CAST(? AS DATE) "
        "    AND night NOT IN (SELECT night FROM staged) "
        "  GROUP BY night"
        "), "
        "hr AS ("
        "  SELECT aseg.night AS night, r.value AS v "
        "  FROM records_dedup r JOIN aseg "
        "    ON r.start_ts >= aseg.s AND r.start_ts < aseg.e "
        "  WHERE r.type = 'heart_rate'"
        ") "
        "SELECT a.night AS night, a.asleep_h AS asleep_h, "
        f"  quantile_cont(hr.v, {SLEEPING_HR_FALLBACK_PCT}) AS shr "
        "FROM asleep a LEFT JOIN hr ON hr.night = a.night "
        "GROUP BY a.night, a.asleep_h ORDER BY a.night",
        (start_night, end_night))
    for r in fb:
        asleep_h = r["asleep_h"] or 0.0
        valid = r["shr"] is not None and asleep_h >= SLEEPING_HR_MIN_ASLEEP_H
        out.append({
            "night": r["night"],
            "value": r["shr"] if valid else None,
            "source": "p5_fallback",
            "asleep_hours": asleep_h,
            "valid_windows": None,
        })

    out.sort(key=lambda x: str(x["night"]))
    return out


def _metric_series(name: str, metric_type: str, start_night: str,
                   end_night: str) -> list[dict]:
    """Dispatch to the extraction strategy declared for `name` in _METRIC_SOURCE."""
    if _METRIC_SOURCE.get(name) == "stage_restricted":
        return _sleeping_hr_series(start_night, end_night)
    return _night_metric_series(metric_type, start_night, end_night)


def _sleep_night_stats(start_night: str, end_night: str) -> list[dict]:
    """Per-night sleep quality inputs (hours asleep, deep+REM fraction, awakenings,
    bedtime) over an inclusive wake-day range."""
    asleep_in = ", ".join(f"'{s}'" for s in _ASLEEP_STAGES)
    rows = _q(
        f"SELECT {_NIGHT_EXPR} AS night, "
        "  sum(date_diff('second', start_ts, end_ts)) "
        f"    FILTER (WHERE stage IN ({asleep_in})) / 3600.0 AS hours_asleep, "
        "  sum(date_diff('second', start_ts, end_ts)) "
        "    FILTER (WHERE stage IN ('deep','rem')) / 3600.0 AS deep_rem_h, "
        "  count(*) FILTER (WHERE stage = 'awake') AS awakenings, "
        "  min(start_ts) AS bed_start "
        f"FROM sleep WHERE {_NIGHT_EXPR} >= CAST(? AS DATE) "
        f"  AND {_NIGHT_EXPR} <= CAST(? AS DATE) "
        "GROUP BY night ORDER BY night",
        (start_night, end_night))
    return rows


def _trailing_values(series: list[dict], target_day: str,
                     window_days: int = BASELINE_WINDOW_DAYS,
                     key: str = "night", value_key: str = "value") -> list[float]:
    """Values from the `window_days` before `target_day` (target excluded)."""
    from datetime import date
    td = date.fromisoformat(str(target_day))
    lo = td - timedelta(days=window_days)
    out: list[float] = []
    for r in series:
        d = date.fromisoformat(str(r[key]))
        if lo <= d < td and r.get(value_key) is not None:
            out.append(r[value_key])
    return out


def _bedtime_regularity(sleep_rows: list[dict], target_day: str) -> Optional[float]:
    """SD (minutes) of bedtime over the trailing REGULARITY_WINDOW_NIGHTS.

    Bedtime is measured as minutes past 18:00 (the sleep-day boundary), so a
    23:30 and a 00:30 bedtime read as 60 min apart rather than wrapping across
    midnight. Needs >= 2 nights; else None (regularity component omitted)."""
    from datetime import date
    td = date.fromisoformat(str(target_day))
    lo = td - timedelta(days=REGULARITY_WINDOW_NIGHTS)
    mins: list[float] = []
    for r in sleep_rows:
        if r.get("bed_start") is None:
            continue
        d = date.fromisoformat(str(r["night"]))
        if not (lo <= d <= td):
            continue
        bt = r["bed_start"]
        # Minutes past 18:00 local, wrapped into [0, 1440).
        m = (bt.hour * 60 + bt.minute - 18 * 60) % 1440
        mins.append(float(m))
    _, sd = scoring.baseline(mins)
    return sd if len(mins) >= 2 else None


def _latest_recovery_night() -> Optional[str]:
    rows = _q(f"SELECT max({_NIGHT_EXPR}) AS night FROM sleep")
    n = rows[0]["night"] if rows else None
    return str(n) if n else None


def _earliest_recovery_night() -> Optional[str]:
    rows = _q(f"SELECT min({_NIGHT_EXPR}) AS night FROM sleep")
    n = rows[0]["night"] if rows else None
    return str(n) if n else None


def _compute_recovery(date: Optional[str] = None) -> dict:
    """Shared recovery computation used by get_recovery and get_readiness."""
    _ensure_ready()
    target = date or _latest_recovery_night()
    if not target:
        return {"date": date, "state": "insufficient_data", "score": None,
                "band": None, "subscores": {}, "metrics": {},
                "low_confidence_metrics": [],
                "note": "No sleep data — recovery needs nightly sleep windows."}

    earliest = _earliest_recovery_night()
    history_days = 0
    if earliest:
        from datetime import date as _date
        history_days = (_date.fromisoformat(target)
                        - _date.fromisoformat(earliest)).days

    win_start = (_to_dt(target).date()
                 - timedelta(days=BASELINE_WINDOW_DAYS)).isoformat()

    # Physiological metrics: today's value (sourced per _METRIC_SOURCE) vs its own
    # trailing baseline. low_confidence flags a thin baseline (see MIN_BASELINE_N).
    subscores: dict[str, Optional[float]] = {}
    metrics: dict[str, dict] = {}
    low_confidence_metrics: list[str] = []
    confidences: dict[str, float] = {}
    hrv_base_for_cv: list[float] = []
    hrv_recent_for_cv: list[float] = []
    # sleeping_hr reuses rhr_score's sign convention (higher HR than baseline
    # is worse); the metric name and extraction differ, the math does not.
    score_fns = {"hrv": scoring.hrv_score, "sleeping_hr": scoring.rhr_score,
                 "resp": scoring.resp_score, "temp": scoring.temp_score}
    for name, rtype in _RECOVERY_METRICS.items():
        series = _metric_series(name, rtype, win_start, target)
        target_row = next((r for r in series if str(r["night"]) == target), None)
        today = target_row["value"] if target_row else None
        base_vals = _trailing_values(series, target)
        cfg = scoring.DEFAULT_CONFIG
        # HRV is z-scored in log space over a robust/EWMA baseline (Phase 1.1/2),
        # against a 7-day rolling average of the (log) input (Phase 2.1). We still
        # report the raw-space baseline mean/sd for HRV (intuitive ms for the UI)
        # but score off the log-space z and its log-space scale. Everything else
        # uses the robust/EWMA baseline on the raw scale.
        if name == "hrv":
            mean, sd = scoring.baseline(base_vals)          # raw display
            recent = _trailing_values(series, target,
                                      window_days=cfg.hrv_smoothing_days - 1)
            if today is not None:
                recent = recent + [today]
            _, sig_scale, z = scoring.hrv_baseline_z(today, base_vals,
                                                     recent_values=recent)
            hrv_base_for_cv, hrv_recent_for_cv = base_vals, recent
        else:
            mean, sd, _ = scoring.baseline_stats(base_vals)
            sig_scale = sd
            z = scoring.zscore(today, mean, sd)
        sub = score_fns[name](z)
        swc, meaningful, significant = scoring.significance(z, sig_scale)
        subscores[name] = sub
        confidences[name] = scoring.metric_confidence(len(base_vals))
        # Only a scored metric with a thin baseline is "low confidence"; a metric
        # with no value at all is simply absent (omitted from the weighted mean).
        low_conf = sub is not None and len(base_vals) < MIN_BASELINE_N
        if low_conf:
            low_confidence_metrics.append(name)
        block = {
            "metric": rtype,
            "source": _METRIC_SOURCE[name],
            "today": round(today, 2) if today is not None else None,
            "baseline_mean": round(mean, 2) if mean is not None else None,
            "baseline_sd": round(sd, 2) if sd else None,
            "baseline_n": len(base_vals),
            "z": round(z, 2) if z is not None else None,
            "subscore": sub,
            "low_confidence": low_conf,
            "swc": round(swc, 3) if swc is not None else None,
            "meaningful_change": meaningful,
            "significant_change": significant,
        }
        # Sleeping HR carries its actual per-night source + how much asleep data
        # the night had, so a thin/gated night is visible to the caller.
        if name == "sleeping_hr" and target_row:
            block["source"] = target_row.get("source", _METRIC_SOURCE[name])
            ah = target_row.get("asleep_hours")
            block["asleep_hours"] = round(ah, 2) if ah is not None else None
            block["valid_windows"] = target_row.get("valid_windows")
        metrics[name] = block

    # Sleep sub-score from the night's stage breakdown.
    sleep_rows = _sleep_night_stats(win_start, target)
    tonight = next((r for r in sleep_rows if str(r["night"]) == target), None)
    if tonight:
        hours = tonight["hours_asleep"]
        deep_rem = tonight["deep_rem_h"]
        # A night with no deep/rem staging (e.g. only generic 'asleep') has no
        # deep_rem fraction — omit that component rather than crash.
        deep_rem_frac = (deep_rem / hours) if (hours and deep_rem is not None) \
            else None
        regularity = _bedtime_regularity(sleep_rows, target)
        # Personalize nightly need to the user's own baseline median (Phase 3.3).
        base_hours = _trailing_values(sleep_rows, target, value_key="hours_asleep")
        need_h = scoring.personalized_sleep_need(base_hours)
        sleep_sub = scoring.sleep_score(
            hours, need=need_h, deep_rem_frac=deep_rem_frac,
            awakenings=tonight["awakenings"], regularity=regularity)
        subscores["sleep"] = sleep_sub
        confidences["sleep"] = scoring.metric_confidence(len(base_hours))
        metrics["sleep"] = {
            "need_h": round(need_h, 2),
            "hours_asleep": round(hours, 2) if hours else None,
            "deep_rem_frac": round(deep_rem_frac, 3)
            if deep_rem_frac is not None else None,
            "awakenings": tonight["awakenings"],
            "bedtime_regularity_min": round(regularity, 1)
            if regularity is not None else None,
            "subscore": sleep_sub,
        }
    else:
        subscores["sleep"] = None

    # HRV variability-collapse contributor (Phase 3.1): CV of the recent (log)
    # HRV window scored against the baseline distribution of rolling CVs.
    if scoring.DEFAULT_CONFIG.hrv_cv_enabled:
        def _logv(xs):
            if scoring.DEFAULT_CONFIG.hrv_log_transform:
                return [math.log(v) for v in xs if v is not None and v > 0]
            return [v for v in xs if v is not None]
        cv_today = scoring.cv(_logv(hrv_recent_for_cv))
        base_cvs = scoring.rolling_cvs(_logv(hrv_base_for_cv),
                                       scoring.DEFAULT_CONFIG.hrv_smoothing_days)
        cv_sub = scoring.hrv_cv_score(cv_today, base_cvs)
        subscores["hrv_cv"] = cv_sub
        confidences["hrv_cv"] = scoring.metric_confidence(len(base_cvs))
        metrics["hrv_cv"] = {
            "metric": "hrv_cv",
            "cv_today": round(cv_today, 4) if cv_today is not None else None,
            "baseline_n": len(base_cvs),
            "subscore": cv_sub,
            "note": "Day-to-day HRV variability vs baseline; a collapse is an "
                    "early overreaching sign.",
        }

    rec = scoring.recovery_score(subscores, confidences=confidences)
    present = [k for k, v in subscores.items() if v is not None]
    # State: calibrating when history is short OR the aggregate confidence is below
    # the OK threshold (Phase 4.1), not any-single-metric-below-10.
    agg_conf = rec.get("confidence")
    low_conf_agg = (agg_conf is not None
                    and agg_conf < scoring.DEFAULT_CONFIG.confidence_ok_threshold)
    if not present:
        state = "insufficient_data"
    elif history_days < MIN_HISTORY_DAYS or low_conf_agg:
        state = "calibrating"
    else:
        state = "ok"
    return {
        "date": target,
        "state": state,
        "history_days": history_days,
        "score": rec["score"],
        "band": rec["band"],
        "confidence": rec["confidence"],
        "ci": rec["ci"],
        "contributors": rec["contributors"],
        "subscores": subscores,
        "metrics": metrics,
        "low_confidence_metrics": low_confidence_metrics,
        "baseline_window_days": BASELINE_WINDOW_DAYS,
    }


@mcp.tool(annotations=RO,
          description="Daily Recovery score (0-100): how recovered the body is, "
                      "scored against the user's own rolling baseline (never "
                      "population norms — Apple HRV is SDNN). Combines nightly HRV, "
                      "sleeping HR (lowest sustained overnight window from "
                      "core/deep/rem stages), respiratory rate, wrist temperature "
                      "and sleep quality, weighted and "
                      "renormalized over whatever metrics are present. Defaults to "
                      "the latest night with data. Bands: green >=67, yellow "
                      "34-66, red <34. state is 'ok', 'calibrating' (<14 days "
                      "history) or 'insufficient_data'. Returns each sub-score "
                      "with its today-value, baseline mean and z.")
def get_recovery(date: Optional[str] = None) -> dict:
    out = _compute_recovery(date)
    note = ("Recovery vs personal baseline over a "
            f"{BASELINE_WINDOW_DAYS}-day trailing window. 50 == on baseline.")
    if out["state"] == "calibrating":
        note += (f" Only {out.get('history_days', 0)} days of history "
                 f"(< {MIN_HISTORY_DAYS}) — baseline still calibrating.")
    elif out["state"] == "insufficient_data":
        note += " Not enough data to score recovery yet."
    note += _low_confidence_note(out.get("low_confidence_metrics", []))
    out["note"] = note
    return out


def _low_confidence_note(names: list[str]) -> str:
    """Trailing note fragment when some baselines are thin (empty if none)."""
    if not names:
        return ""
    return (f" {', '.join(names)} baseline(s) are thin (< {MIN_BASELINE_N} "
            "samples) — those sub-scores are provisional.")


def _recovery_time_hours(target: str, hr_max: float, rest: float) -> float:
    """Rough unresolved recovery time (hours) owed by the last hard session on or
    before `target`: its load scaled by RECOVERY_TIME_PER_TRIMP_H, minus the hours
    elapsed to `target` morning (08:00 local). Clamped to [0, RECOVERY_TIME_MAX_H].
    """
    rows = _q(
        "SELECT start_ts, end_ts, duration, avg_hr, energy "
        "FROM workouts WHERE end_ts <= CAST(? AS TIMESTAMPTZ) + INTERVAL 1 DAY "
        "ORDER BY end_ts DESC LIMIT 1",
        (target,))
    if not rows:
        return 0.0
    w = rows[0]
    load = analytics.trimp(w["duration"] or 0, w["avg_hr"], rest, hr_max)
    if load is None:
        load = (w["energy"] or 0) * analytics.ENERGY_LOAD_K
    needed = min(RECOVERY_TIME_MAX_H, load * RECOVERY_TIME_PER_TRIMP_H)
    morning = _to_dt(target).replace(hour=8, minute=0, second=0)
    end = w["end_ts"]
    if end.tzinfo is not None and morning.tzinfo is None:
        morning = morning.replace(tzinfo=end.tzinfo)
    elapsed_h = max(0.0, (morning - end).total_seconds() / 3600.0)
    return max(0.0, needed - elapsed_h)


def _acwr_for_date(target: str) -> Optional[dict]:
    """The ACWR entry (ratio + acute load + flag) for `target`, seeded with the
    prior 28 days so acute/chronic reflect real history."""
    hr_max, _ = analytics.resolve_max_hr(_q, None)
    rest, _ = analytics.resolve_resting_hr(_q, None)
    calc_start = (_to_dt(target).date() - timedelta(days=28)).isoformat()
    daily_ext = _training_daily(calc_start, target, hr_max, rest)
    if not daily_ext:
        return None
    first = min(d["date"] for d in daily_ext)
    span = analytics.daily_date_range(_to_dt(first), _to_dt(target))
    load_by_day = {d["date"]: d["load"] for d in daily_ext}
    dates = [str(d) for d in span]
    loads = [load_by_day.get(str(d), 0.0) for d in span]
    cfg = scoring.DEFAULT_CONFIG
    series = analytics.acwr(dates, loads, acute_days=cfg.acwr_acute_days,
                            chronic_days=cfg.acwr_chronic_days,
                            min_history_date=_dataset_start(),
                            uncoupled=cfg.acwr_uncoupled)
    return next((a for a in series if a["date"] == target), None)


@mcp.tool(annotations=RO,
          description="Daily Readiness score (0-100): Recovery adjusted down for "
                      "accumulated training load. Builds on get_recovery, then "
                      "applies a load penalty from ACWR (no penalty in the 0.8-1.3 "
                      "sweet spot, ramping up above 1.5) and recovery still owed "
                      "from the last hard session. Defaults to the latest night "
                      "with data. Returns the readiness score+band, the recovery "
                      "it built on, the load inputs (ACWR, acute load, recovery "
                      "time), and a short training recommendation.")
def get_readiness(date: Optional[str] = None) -> dict:
    _ensure_ready()
    rec = _compute_recovery(date)
    target = rec["date"]
    if not target or rec["score"] is None:
        out = scoring.readiness_score(None, None)
        return {"date": target, "state": rec["state"], **out,
                "recovery": rec,
                "low_confidence_metrics": rec.get("low_confidence_metrics", []),
                "note": "Readiness needs a recovery score first."}

    acwr_entry = _acwr_for_date(target)
    acwr_val = acwr_entry["acwr"] if acwr_entry else None
    acute_load = acwr_entry["acute_7d"] if acwr_entry else None
    hr_max, _ = analytics.resolve_max_hr(_q, None)
    rest, _ = analytics.resolve_resting_hr(_q, None)
    rec_time = _recovery_time_hours(target, hr_max, rest)

    # Directional z-scores for the Phase 1.3 illness detector (temp/resp/
    # sleeping-HR up + HRV down co-elevation caps readiness advisorily).
    illness_z = {m: rec["metrics"].get(m, {}).get("z")
                 for m in ("hrv", "sleeping_hr", "resp", "temp")}
    out = scoring.readiness_score(rec["score"], acwr_val,
                                  acute_load=acute_load,
                                  recovery_time_hours=rec_time,
                                  illness_zscores=illness_z)
    return {
        "date": target,
        "state": rec["state"],
        "score": out["score"],
        "band": out["band"],
        "recovery_score": rec["score"],
        "recovery_band": rec["band"],
        "load": {
            "acwr": acwr_val,
            "acwr_flag": acwr_entry["flag"] if acwr_entry else None,
            "acute_7d": acute_load,
            "recovery_time_hours": round(rec_time, 1),
            "penalty": out["penalty"],
        },
        "recommendation": out["recommendation"],
        "illness": out.get("illness"),
        "recovery": rec,
        "low_confidence_metrics": rec.get("low_confidence_metrics", []),
        "note": ("Readiness = Recovery - load penalty (ACWR + recovery owed)."
                 + _low_confidence_note(rec.get("low_confidence_metrics", []))),
    }


# --- read-only SQL escape hatch -------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|create|alter|attach|detach|copy|pragma|"
    r"install|load|export|import|call|set|reset|vacuum|checkpoint|begin|"
    r"commit|rollback|replace)\b",
    re.IGNORECASE,
)


def _validate_select(query: str) -> Optional[str]:
    stripped = re.sub(r"--[^\n]*", " ", query)              # line comments
    stripped = re.sub(r"/\*.*?\*/", " ", stripped, flags=re.S)  # block comments
    stripped = stripped.strip().rstrip(";").strip()
    if not stripped:
        return "Empty query."
    if ";" in stripped:
        return "Only a single statement is allowed (no ';')."
    low = stripped.lower()
    if not (low.startswith("select") or low.startswith("with")):
        return "Only SELECT/WITH queries are permitted."
    m = _FORBIDDEN.search(stripped)
    if m:
        return f"Statement type '{m.group(0)}' is not allowed (read-only server)."
    return None


@mcp.tool(annotations=RO,
          description="Run a read-only SELECT query against the health database "
                      "for anything the dedicated tools don't cover. "
                      "Tables: records, records_dedup, workouts, sleep, "
                      "activity_summary, clinical. DDL/DML is rejected.")
def run_sql(query: str) -> dict:
    _ensure_ready()
    err = _validate_select(query)
    if err:
        return {"error": err}
    safe = query.strip().rstrip(";")
    # Second belt: connection itself is read-only, so writes fail even if the
    # validator is somehow bypassed.
    try:
        # Closing paren on its own line so a trailing line-comment in the user's
        # query can't comment out the wrapper.
        rows = _q(f"SELECT * FROM (\n{safe}\n) AS _sub LIMIT 1000")
    except Exception as exc:  # surface a clean message to the model
        return {"error": f"Query failed: {exc}"}
    return {"row_count": len(rows), "rows": rows,
            "note": "Results capped at 1000 rows."}


@mcp.tool(annotations=WRITE,
          description="Import the newest Apple Health export from the "
                      "drop-folder (~/Documents/AppleHealthExport) into the "
                      "database right now, so data you "
                      "just exported from the iPhone Health app becomes "
                      "queryable. Call this after the user says they exported "
                      "fresh data. Idempotent — re-importing the same "
                      "export changes nothing. Pass force=true to re-import an "
                      "export that was already loaded. Returns a status: "
                      "'imported', 'already_current', 'empty', 'busy', or "
                      "'error', with row-count totals.")
def reload_data(force: bool = False) -> dict:
    return import_pipeline.reload(force=force)


def main() -> None:
    config.ensure_dirs()
    _ensure_ready()
    mcp.run()


if __name__ == "__main__":
    main()
