"""FastMCP server exposing read-only tools over the local Apple Health DuckDB.

Every tool is annotated read-only. Data never leaves the machine: these tools
run queries against a local DuckDB file and return rows to the Claude Desktop
client over stdio.
"""
from __future__ import annotations

import re
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import analytics, config, import_pipeline, storage

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
                      "athlete's observed max across all workouts. Metrics absent "
                      "from a given workout (e.g. power on a walk) degrade to null.")
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
        "  SELECT start_ts, "
        "    sum(value) OVER (ORDER BY start_ts) AS cum_km "
        "  FROM records_dedup "
        "  WHERE type = 'distance_walking_running' "
        "    AND start_ts >= ? AND start_ts <= ?"
        ") "
        # round before ceil so a float artifact at an exact km boundary (e.g.
        # cum_km = 2.0000000004) doesn't spawn a spurious trailing split.
        "SELECT cast(ceil(round(cum_km, 6)) AS INT) AS km, "
        "min(start_ts) AS t_start, sum(1) AS samples "
        "FROM d WHERE cum_km > 0 GROUP BY km ORDER BY km",
        (start, end))
    if not rows:
        return []
    splits = []
    for i, r in enumerate(rows):
        t0 = r["t_start"]
        t1 = rows[i + 1]["t_start"] if i + 1 < len(rows) else end
        dur = (t1 - t0).total_seconds()
        splits.append({
            "km": r["km"],
            "duration_sec": round(dur, 1),
            "pace_min_per_km": round(dur / 60.0, 2) if dur else None,
        })
    return splits


@mcp.tool(annotations=RO,
          description="Time spent in heart-rate zones Z1-Z5 aggregated over a "
                      "period (not a single workout). scope='workouts' counts "
                      "only HR recorded inside logged workout windows; "
                      "scope='all' counts every heart_rate sample in range. "
                      "max_hr defaults to the observed workout max. Zones: "
                      "Z1<60% Z2 60-70 Z3 70-80 Z4 80-90 Z5>=90% of max.")
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
    rest, rest_src = analytics.resolve_resting_hr(_q, resting_hr,
                                                  start_date, end_date)
    daily = _training_daily(start_date, end_date, hr_max, rest)
    # Build a contiguous daily series for ACWR over the covered span.
    acwr_series: list[dict] = []
    if daily:
        first = min(d["date"] for d in daily)
        last = max(d["date"] for d in daily)
        span = analytics.daily_date_range(
            _to_dt(first), _to_dt(last))
        load_by_day = {d["date"]: d["load"] for d in daily}
        dates = [str(d) for d in span]
        loads = [load_by_day.get(str(d), 0.0) for d in span]
        acwr_series = analytics.acwr(dates, loads)
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
