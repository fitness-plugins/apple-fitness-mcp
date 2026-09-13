"""FastMCP server exposing read-only tools over the local Apple Health DuckDB.

Every tool is annotated read-only. Data never leaves the machine: these tools
run queries against a local DuckDB file and return rows to the Claude Desktop
client over stdio.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from . import (analytics, config, dashboard, import_pipeline, scoring,
               storage, sync_import, sync_pairing, sync_receiver, sync_spool,
               zones)

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

# Sections `get_workout_detail` can emit. `include=None` means all of them, so a
# caller that passes nothing gets exactly the response it got before.
_DETAIL_SECTIONS = ("series", "zones", "splits", "decoupling", "reps")

# Structured-interval steps live in `workout_events`. Filtering is on
# `workout_start_ts`, NOT `workout_hash`: the export stores one session 2-3
# times under different row_hashes (repeated exports, watch renames), so the
# copy the `DISTINCT ON (type, start minute)` dedup picks for `workouts` is not
# necessarily the copy the events hang off. The flip side is that those copies
# duplicate the events too, so steps are de-duplicated here on Apple's own
# per-activity uuid plus document order. Only `event_kind = 'activity'` rows are
# repetitions; the top-level `event` rows (segment/pause/resume/marker) have
# arbitrarily overlapping durations and are not usable as reps.
# Written in the shape `dashboard._q_workouts` / `zones.per_run_maxima` use and
# that the DuckDB build the server ships is known to run: the DISTINCT ON
# expressions are selected explicitly, the subquery is left unaliased, and
# nothing is aliased at all (`hours` taught us not to trust alias names here).
_REP_COLS = (
    "step_index, step_key_path, step_block, step_repeat, step_slot, "
    "step_successful, start_ts, end_ts, duration, duration_unit, "
    "distance, distance_unit, avg_hr, min_hr, max_hr, avg_speed, speed_unit"
)
_REP_SQL = (
    f"SELECT {_REP_COLS} FROM ("
    "  SELECT DISTINCT ON (activity_uuid, step_index) "
    "         activity_uuid, workout_source_name, " + _REP_COLS +
    "  FROM workout_events "
    "  WHERE event_kind = 'activity' "
    "    AND workout_start_ts = CAST(? AS TIMESTAMPTZ) "
    "  ORDER BY activity_uuid, step_index, workout_source_name"
    ") ORDER BY step_index"
)

# Apple writes durationUnit/unit alongside every quantity; normalise rather than
# assume, so a step measured in seconds or metres still yields a real min/km.
_STEP_DURATION_TO_MIN = {
    "min": 1.0, "mins": 1.0, "minute": 1.0, "minutes": 1.0,
    "s": 1.0 / 60.0, "sec": 1.0 / 60.0, "secs": 1.0 / 60.0,
    "second": 1.0 / 60.0, "seconds": 1.0 / 60.0,
    "h": 60.0, "hr": 60.0, "hour": 60.0, "hours": 60.0,
}
_STEP_DISTANCE_TO_KM = {
    "km": 1.0, "m": 0.001, "mi": 1.609344, "ft": 0.0003048, "yd": 0.0009144,
}


def _as_bpm(value: float) -> int:
    """Whole bpm for a resolved HR anchor.

    `zones` returns a float; the tools have always surfaced `max_hr_used` /
    `resting_hr_used` as an int, and every downstream boundary is a whole bpm,
    so the JSON shape is preserved by rounding once, here.
    """
    return int(round(value))


def _hr_max_anchor(max_hr: Optional[float] = None) -> tuple[int, str]:
    """(bpm, source) for HRmax — the single resolution path for every tool.

    Routed through `zones.resolve_hr_max` so the observed 210 bpm artefact does
    not silently define every zone boundary; the calibrated / functional value
    (p95 of the deduplicated per-run maxima) wins when one is available.
    """
    value, source = zones.resolve_hr_max(_q, max_hr)
    return _as_bpm(value), source


def _resting_hr_anchor(resting_hr: Optional[float] = None) -> tuple[int, str]:
    """(bpm, source) for resting HR, via `zones.resolve_resting_hr`."""
    value, source = zones.resolve_resting_hr(_q, resting_hr)
    return _as_bpm(value), source


def _training_bounds() -> tuple[Optional[list[dict]], Optional[str]]:
    """(bounds, error). A malformed personal zone file must not 500 a tool."""
    try:
        return zones.training_zone_bounds(), None
    except ValueError as exc:
        return None, str(exc)


def _include_sections(include: Any) -> tuple[set, Optional[str]]:
    """(wanted sections, error). None/empty means every section (the default)."""
    if include is None:
        return set(_DETAIL_SECTIONS), None
    if isinstance(include, str):
        raw = [p for p in re.split(r"[,\s]+", include) if p]
    else:
        raw = [str(p).strip() for p in include if str(p).strip()]
    if not raw:
        return set(_DETAIL_SECTIONS), None
    wanted, unknown = set(), []
    for name in raw:
        low = name.lower()
        if low == "all":
            wanted.update(_DETAIL_SECTIONS)
        elif low in _DETAIL_SECTIONS:
            wanted.add(low)
        else:
            unknown.append(name)
    if unknown:
        return set(), (f"Unknown include section(s): {', '.join(unknown)}. "
                       f"Valid sections: {', '.join(_DETAIL_SECTIONS)}.")
    return wanted, None


def _step_minutes(duration, unit, start, end) -> Optional[float]:
    """A step's length in minutes, from its own quantity or its timestamps."""
    if duration is not None:
        factor = _STEP_DURATION_TO_MIN.get((unit or "min").strip().lower())
        if factor is not None:
            return float(duration) * factor
    if start is not None and end is not None:
        return (end - start).total_seconds() / 60.0
    return None


def _step_km(distance, unit) -> Optional[float]:
    """A step's distance in km, or None when the unit is not one we know."""
    if distance is None:
        return None
    factor = _STEP_DISTANCE_TO_KM.get((unit or "km").strip().lower())
    return float(distance) * factor if factor is not None else None


def _rep_role(block, slot, block_sizes: dict, blocks: list) -> str:
    """warmup / work / recovery / cooldown for one structured step.

    `step_key_path` is Apple's `block.repetition.step`: within a repeat block
    slot 0 is the work interval and slot 1 the recovery. Warm-up is the first
    block and cool-down the last — but only for blocks holding a *single* step,
    because a repeat block is also first or last whenever the session has no
    separate warm-up or cool-down, and its slots must still win.
    """
    by_slot = "work" if slot == 0 else ("recovery" if slot == 1 else "step")
    if block is None or block not in block_sizes:
        return by_slot
    if block_sizes[block] > 1 or len(blocks) < 2:
        return by_slot
    if block == blocks[0]:
        return "warmup"
    if block == blocks[-1]:
        return "cooldown"
    return by_slot


def _no_reps() -> dict:
    """The explicit empty marker for a workout with no structured intervals."""
    return {
        "structured": False,
        "interval_structure": False,
        "count": 0,
        "roles": {},
        "work_summary": None,
        "steps": [],
        "note": "This workout has no structured-interval data (no "
                "WorkoutActivity rows in workout_events); it was not built in "
                "the Workout app's interval builder. This is the normal case.",
    }


def _build_reps(rows: list[dict], bounds: Optional[list[dict]]) -> dict:
    """Decode `workout_events` activity rows into labelled repetitions.

    Pure: takes rows and zone bounds, touches no database. Pace is derived from
    the step's own distance and duration (min/km), not from avg_speed, so a step
    without a speed statistic still reports one.
    """
    if not rows:
        return _no_reps()
    blocks = sorted({r["step_block"] for r in rows if r["step_block"] is not None})
    block_sizes: dict = {}
    for r in rows:
        b = r["step_block"]
        if b is not None:
            block_sizes[b] = block_sizes.get(b, 0) + 1

    steps: list[dict] = []
    roles: dict = {}
    for r in rows:
        role = _rep_role(r["step_block"], r["step_slot"], block_sizes, blocks)
        roles[role] = roles.get(role, 0) + 1
        minutes = _step_minutes(r["duration"], r["duration_unit"],
                                r["start_ts"], r["end_ts"])
        km = _step_km(r["distance"], r["distance_unit"])
        pace = None
        if minutes and km and km > 0 and minutes > 0:
            pace = round(minutes / km, 2)
        steps.append({
            "step_index": r["step_index"],
            "role": role,
            "key_path": r["step_key_path"],
            "block": r["step_block"],
            "repeat": r["step_repeat"],
            "slot": r["step_slot"],
            "successful": r["step_successful"],
            "start_ts": r["start_ts"],
            "end_ts": r["end_ts"],
            "duration_min": round(minutes, 2) if minutes is not None else None,
            "distance_km": round(km, 3) if km is not None else None,
            "pace_min_per_km": pace,
            "avg_hr": round(r["avg_hr"], 0) if r["avg_hr"] else None,
            "min_hr": round(r["min_hr"], 0) if r["min_hr"] else None,
            "max_hr": round(r["max_hr"], 0) if r["max_hr"] else None,
            "avg_speed_kmh": round(r["avg_speed"], 2) if r["avg_speed"] else None,
            "training_zone": (zones.classify(r["avg_hr"], bounds)
                              if bounds else None),
        })

    work = [s for s in steps if s["role"] == "work"]
    # An interval session = repeated work efforts, or work separated by an
    # explicit recovery step. That is the condition under which first-half vs
    # second-half decoupling stops meaning anything.
    interval = len(work) >= 2 or roles.get("recovery", 0) >= 1
    return {
        "structured": True,
        "interval_structure": interval,
        "count": len(steps),
        "roles": roles,
        "work_summary": _work_summary(work),
        "steps": steps,
        "note": "step_key_path is Apple's block.repetition.step. Within a "
                "repeat block slot 0 is the work interval and slot 1 the "
                "recovery; the first/last single-step blocks are the warm-up "
                "and cool-down. Read 'role' to tell reps from recoveries.",
    }


def _work_summary(work: list[dict]) -> Optional[dict]:
    """Aggregate over the work reps only, so the session reads at a glance."""
    if not work:
        return None
    paces = [s["pace_min_per_km"] for s in work if s["pace_min_per_km"]]
    hrs = [s["avg_hr"] for s in work if s["avg_hr"]]
    peaks = [s["max_hr"] for s in work if s["max_hr"]]
    dists = [s["distance_km"] for s in work if s["distance_km"]]
    by_zone: dict = {}
    for s in work:
        z = s["training_zone"]
        if z:
            by_zone[z] = by_zone.get(z, 0) + 1
    return {
        "count": len(work),
        "distance_km": round(sum(dists), 3) if dists else None,
        "avg_pace_min_per_km": round(sum(paces) / len(paces), 2) if paces else None,
        "fastest_pace_min_per_km": min(paces) if paces else None,
        "slowest_pace_min_per_km": max(paces) if paces else None,
        "avg_hr": round(sum(hrs) / len(hrs), 0) if hrs else None,
        "max_hr": max(peaks) if peaks else None,
        "training_zone_counts": by_zone,
    }


@mcp.tool(annotations=RO,
          description="Full intraday breakdown of one workout: binned HR / power "
                      "/ speed (pace) / cadence series, time in HR zones under "
                      "both zone models, aerobic decoupling (cardiac drift), "
                      "per-km splits, and the individual repetitions of a "
                      "structured interval session. Picks the workout by "
                      "workout_id (row_hash), or the most recent one matching "
                      "optional type/date filters. max_hr defaults to the "
                      "calibrated / functional HRmax (p95 of per-run maxima), "
                      "not the single highest observed reading. Two zone models "
                      "are returned side by side: 'hr_zones' is the %-of-max "
                      "scheme (Z1 <60%, Z2 60-70%, Z3 70-80%, Z4 80-90%, "
                      "Z5 >=90% of max_hr) and 'training_zones' the athlete's "
                      "named absolute bands (recovery <=149, easy 150-165, grey "
                      "166-177, threshold 178-186, vo2max 187+) — use the latter "
                      "to tell real threshold work from grey-zone time, which "
                      "Z4 lumps together. 'reps' lists each interval step in "
                      "order with role (warmup/work/recovery/cooldown), "
                      "duration, distance, pace in min/km, avg/min/max HR and "
                      "its training zone; it reports structured=false for an "
                      "ordinary run, and for an interval session decoupling is "
                      "marked not applicable (the HR:power ratio is meant to "
                      "move between reps). Pass include=['zones'] — any of "
                      "series, zones, splits, decoupling, reps — to trim the "
                      "payload; the series alone is ~200 points for a "
                      "100-minute run at bin_seconds=30. Metrics absent from a "
                      "workout (e.g. power on a walk) degrade to null.")
def get_workout_detail(workout_id: Optional[str] = None,
                       type: Optional[str] = None, date: Optional[str] = None,
                       bin_seconds: int = 30,
                       max_hr: Optional[int] = None,
                       include: Optional[list[str]] = None) -> dict:
    _ensure_ready()
    want, bad_include = _include_sections(include)
    if bad_include:
        return {"error": bad_include}
    w = analytics.select_workout(_q, workout_id=workout_id, type=type, date=date)
    if not w:
        return {"error": "No matching workout found.",
                "note": "Adjust workout_id/type/date, or import data first."}
    hr_max, hr_src = _hr_max_anchor(max_hr)
    start, end = w["start_ts"], w["end_ts"]
    # The named bands are read once and shared by the zone totals and the reps.
    tz_bounds, tz_err = _training_bounds()

    # 2. binned series with derived pace + cadence.
    series = None
    if "series" in want:
        points = analytics.binned_series(_q, start, end, bin_seconds,
                                         analytics.SERIES_METRICS)
        for p in points:
            p["pace_min_per_km"] = analytics.pace_min_per_km(p.get("speed"))
            p["cadence_spm"] = analytics.cadence_spm(p.get("speed"),
                                                     p.get("stride"))
            for k in ("hr", "power", "speed", "stride"):
                if p.get(k) is not None:
                    p[k] = round(p[k], 1)
        series = {"bin_seconds": bin_seconds, "count": len(points),
                  "points": points}

    # 3. HR zones over the window, under both models.
    hr_zones = training_zones = None
    if "zones" in want:
        where_sql = "type = 'heart_rate' AND start_ts >= ? AND start_ts <= ?"
        params = (start, end)
        hr_zones = zones.zone_time(_q, where_sql, params,
                                   zones.percent_zone_bounds(hr_max))
        training_zones = (
            zones.zone_time(_q, where_sql, params, tz_bounds,
                            model=zones.TRAINING_MODEL)
            if tz_bounds else {"model": zones.TRAINING_MODEL, "error": tz_err})

    # 4. repetitions. Read whenever decoupling is wanted too: whether the
    # session has interval structure decides whether decoupling means anything.
    reps = None
    if "reps" in want or "decoupling" in want:
        reps = _build_reps(_q(_REP_SQL, (start,)), tz_bounds)

    # 5. decoupling: split the window in half, HR-to-power (or HR-to-speed)
    # drift — but only where that is a statement about aerobic control.
    decoup = None
    if "decoupling" in want:
        if reps and reps["interval_structure"]:
            decoup = _decoupling_not_applicable(reps)
        else:
            mid = start + (end - start) / 2
            halves = _q(
                "SELECT CASE WHEN start_ts < ? THEN 1 ELSE 2 END AS half, "
                "avg(value) FILTER (WHERE type = 'heart_rate') AS hr, "
                "avg(value) FILTER (WHERE type = 'running_power') AS power, "
                "avg(value) FILTER (WHERE type = 'running_speed') AS speed "
                "FROM records_dedup WHERE start_ts >= ? AND start_ts <= ? "
                "GROUP BY half ORDER BY half",
                (mid, start, end))
            decoup = _decoupling_from_halves(halves)

    # 6. per-km splits from cumulative running distance.
    splits = _km_splits(start, end) if "splits" in want else None

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
    note = (f"max_hr {hr_max} bpm ({hr_src}). Missing metrics are null "
            "(e.g. no power/cadence when the device didn't record them).")
    if want != set(_DETAIL_SECTIONS):
        note += (" Sections limited by include=" +
                 f"{sorted(want)}; omitted sections are absent, not empty.")

    # Assembled in the historical key order, with the new sections appended.
    out: dict[str, Any] = {"summary": summary}
    if series is not None:
        out["series"] = series
    if hr_zones is not None:
        out["hr_zones"] = hr_zones
        out["training_zones"] = training_zones
    if decoup is not None:
        out["decoupling"] = decoup
    if splits is not None:
        out["splits"] = splits
    if "reps" in want:
        out["reps"] = reps
    out["note"] = note
    return out


def _decoupling_not_applicable(reps: dict) -> dict:
    """Decoupling suppressed for an interval session, with the reason why.

    Five hard reps with walking recoveries move the HR:power ratio by design, so
    a first-half/second-half drift number describes the session's structure and
    reads as a finding ("13.2%, poor aerobic control") when it is an artefact.
    Same key set as `_decoupling_from_halves` so callers need no special case;
    `good_aerobic_control` is None (unknown), never False.
    """
    n_work = reps["roles"].get("work", 0)
    n_rec = reps["roles"].get("recovery", 0)
    return {
        "drift_pct": None,
        "applicable": False,
        "basis": None,
        "first_half_ratio": None,
        "second_half_ratio": None,
        "good_aerobic_control": None,
        "reason": (f"Interval session ({n_work} work rep{'' if n_work == 1 else 's'}"
                   f", {n_rec} recover{'y' if n_rec == 1 else 'ies'}): "
                   "the HR:power ratio is supposed to move between hard reps and "
                   "recoveries, so half-vs-half drift measures the session "
                   "design, not aerobic control. Compare the per-rep HR and pace "
                   "in 'reps' instead. Decoupling is meaningful on a steady "
                   "continuous effort."),
    }


def _decoupling_from_halves(halves: list[dict]) -> dict:
    """Aerobic decoupling from the two half-window aggregate rows."""
    by_half = {r["half"]: r for r in halves}
    h1, h2 = by_half.get(1), by_half.get(2)
    if not h1 or not h2:
        return {"drift_pct": None,
                "applicable": True,
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
        "applicable": True,
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
          description="Time spent in heart-rate zones aggregated over a period "
                      "(not a single workout), under both zone models at once. "
                      "scope='workouts' counts only HR recorded inside logged "
                      "workout windows; scope='all' counts every heart_rate "
                      "sample in range. max_hr defaults to the calibrated / "
                      "functional HRmax (p95 of per-run maxima), not the single "
                      "highest observed reading. The top-level 'zones' is the "
                      "%-of-max model (Z1 <60%, Z2 60-70%, Z3 70-80%, "
                      "Z4 80-90%, Z5 >=90% of max_hr; Z1 has no lower gap, so "
                      "scope='all' totals include rest/sleep HR). "
                      "'training_zones' is the athlete's named absolute-bpm "
                      "model (recovery <=149, easy 150-165, grey 166-177, "
                      "threshold 178-186, vo2max 187+); read its "
                      "minutes_by_zone to answer how many minutes were actually "
                      "at threshold rather than in the grey zone — a split the "
                      "percentage model cannot make, because Z4 spans both.")
def get_hr_zones(start_date: Optional[str] = None, end_date: Optional[str] = None,
                 max_hr: Optional[int] = None, scope: str = "workouts") -> dict:
    _ensure_ready()
    hr_max, hr_src = _hr_max_anchor(max_hr)
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
    where_params = tuple(params)

    percent = zones.zone_time(_q, where_sql, where_params,
                              zones.percent_zone_bounds(hr_max))
    bounds, zone_err = _training_bounds()
    if bounds:
        training = zones.zone_time(_q, where_sql, where_params, bounds,
                                   model=zones.TRAINING_MODEL)
        named = ", ".join(f"{k} {v} min"
                          for k, v in training["minutes_by_zone"].items())
        named_note = f" Named bands: {named}."
    else:
        training = {"model": zones.TRAINING_MODEL, "error": zone_err}
        named_note = f" Named bands unavailable: {zone_err}"
    return {
        "scope": scope,
        "max_hr_used": hr_max, "max_hr_source": hr_src,
        **percent,
        "training_zones": training,
        "note": f"max_hr {hr_max} bpm ({hr_src}). Time weighted by gaps between "
                "samples, capped at 60s. scope='all' includes non-workout HR."
                + named_note,
    }


@mcp.tool(annotations=RO,
          description="Daily training load and acute:chronic workload ratio "
                      "(ACWR) over a period. Load is Banister TRIMP from a "
                      "workout's HR reserve where avg_hr exists, otherwise an "
                      "active-energy proxy so unlogged effort still counts. ACWR "
                      "= 7-day acute load vs 28-day chronic; 0.8-1.3 is the "
                      "sweet spot, >1.5 flags elevated injury risk. max_hr "
                      "defaults to the calibrated / functional HRmax and "
                      "resting_hr to a low percentile (p10) of recent "
                      "resting_heart_rate; both report the source they "
                      "resolved from.")
def get_training_load(start_date: Optional[str] = None,
                      end_date: Optional[str] = None, max_hr: Optional[int] = None,
                      resting_hr: Optional[int] = None) -> dict:
    _ensure_ready()
    hr_max, hr_src = _hr_max_anchor(max_hr)
    rest, rest_src = _resting_hr_anchor(resting_hr)

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
    hr_max, _ = _hr_max_anchor()
    rest, _ = _resting_hr_anchor()
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
    hr_max, _ = _hr_max_anchor()
    rest, _ = _resting_hr_anchor()
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
                      "Tables: records, records_dedup, workouts, "
                      "workout_events, sleep, activity_summary, clinical. "
                      "workout_events holds the structural children of a "
                      "workout: event_kind='activity' rows are the "
                      "repetitions of a structured interval session (join "
                      "them on workout_start_ts, not workout_hash, and note "
                      "step_key_path = block.repetition.step, where slot 0 "
                      "is the work interval and slot 1 the recovery); "
                      "event_kind='event' rows are segment/pause/resume/"
                      "marker boundaries whose durations overlap and are "
                      "NOT usable as repetitions. DDL/DML is rejected.")
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


# --- weekly training plan (write to a local folder the user shares from) --------

_WEEKDAYS = {"monday", "tuesday", "wednesday", "thursday", "friday",
             "saturday", "sunday"}


def _is_iso_date(value: Any) -> bool:
    """True if value is a 'YYYY-MM-DD' string (a calendar date, no time)."""
    if not isinstance(value, str):
        return False
    try:
        from datetime import date as _date
        _date.fromisoformat(value)
        return True
    except ValueError:
        return False


def validate_and_fill_plan(plan: dict) -> dict:
    """Validate a WeeklyPlan against the MCP<->app contract and fill identity
    fields. Returns a NEW filled dict; raises ValueError listing every problem on
    invalid input.

    Contract (see apple-fitness-ios/IOS_TRAINING_PLAN_PROMPTS.md, "WeeklyPlan JSON
    schema"):
      required: schema_version (int), week_of (ISO date, the Monday), planned
        (non-empty list); each item has id (non-empty, unique str) + constraints
        (an object — an OPEN map, contents not validated here, the app owns that).
      optional per item: title, notes, day (monday..sunday or null), generated_by.
      plan_id (UUID) and generated_at (ISO-8601 UTC) are FILLED if absent; a
        provided plan_id is preserved (lets a caller re-save a specific version).
    """
    if not isinstance(plan, dict):
        raise ValueError("plan must be a JSON object (dict).")

    errors: list[str] = []
    out = dict(plan)  # shallow copy; we don't mutate the caller's dict

    sv = out.get("schema_version")
    if sv is None:
        errors.append("schema_version is required (int).")
    elif not isinstance(sv, int) or isinstance(sv, bool):
        errors.append("schema_version must be an int.")

    week_of = out.get("week_of")
    if week_of is None:
        errors.append("week_of is required (ISO date 'YYYY-MM-DD', the Monday of "
                      "the week).")
    elif not _is_iso_date(week_of):
        errors.append("week_of must be an ISO date string 'YYYY-MM-DD'.")

    planned = out.get("planned")
    if not isinstance(planned, list) or not planned:
        errors.append("planned is required and must be a non-empty list.")
    else:
        seen_ids: set[str] = set()
        for i, item in enumerate(planned):
            if not isinstance(item, dict):
                errors.append(f"planned[{i}] must be an object.")
                continue
            pid = item.get("id")
            if not isinstance(pid, str) or not pid.strip():
                errors.append(f"planned[{i}].id is required and must be a "
                              "non-empty string.")
            elif pid in seen_ids:
                errors.append(f"planned[{i}].id '{pid}' is duplicated; ids must be "
                              "unique within the plan.")
            else:
                seen_ids.add(pid)
            if not isinstance(item.get("constraints"), dict):
                errors.append(f"planned[{i}].constraints is required and must be "
                              "an object (map of constraint name -> params).")
            day = item.get("day")
            if day is not None and day not in _WEEKDAYS:
                errors.append(f"planned[{i}].day must be one of monday..sunday or "
                              "null.")

    # plan_id: keep a provided one (must be a valid UUID); else mint a new one.
    plan_id = out.get("plan_id")
    if plan_id is None:
        out["plan_id"] = str(uuid.uuid4())
    else:
        try:
            out["plan_id"] = str(uuid.UUID(str(plan_id)))
        except (ValueError, AttributeError, TypeError):
            errors.append("plan_id, if provided, must be a valid UUID.")

    # generated_at: fill with now (UTC) if absent; keep a provided value as-is.
    if not out.get("generated_at"):
        out["generated_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ")

    if errors:
        raise ValueError("Invalid WeeklyPlan: " + "; ".join(errors))
    return out


def _atomic_write_json(path: Path, obj: dict) -> None:
    """Write pretty UTF-8 JSON to `path` atomically (temp file in the same dir,
    then os.replace) so a reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".plan-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@mcp.tool(annotations=WRITE,
          description="Save a WeeklyPlan JSON to a local folder so the user can "
                      "share it to the iOS training app (writes "
                      "~/Documents/AppleFitnessPlans/plan.json by default — a "
                      "plain local file, no iCloud, no network calls from here). "
                      "Call this AFTER you have composed a WeeklyPlan from the "
                      "user's recovery/training-load/workout data. The plan is a "
                      "JSON object: schema_version (int), week_of (ISO date, the "
                      "Monday), and a non-empty 'planned' list of {id, "
                      "constraints{...}, optional title/notes/day}. plan_id and "
                      "generated_at are filled automatically if omitted. "
                      "IMPORTANT: omitting plan_id mints a NEW plan version the "
                      "app re-matches from scratch (crediting already-done "
                      "workouts); pass the SAME plan_id to re-save a specific "
                      "version idempotently (the app dedupes by plan_id). Returns "
                      "the written path plus the filled plan_json so you can show "
                      "it or the user can AirDrop / share the file to their phone.")
def save_weekly_plan(plan: Any, path: Optional[str] = None) -> dict:
    # MCP passes JSON objects as dicts; also accept a JSON string defensively.
    if isinstance(plan, str):
        try:
            plan = json.loads(plan)
        except json.JSONDecodeError as exc:
            return {"status": "invalid", "errors": [f"plan is not valid JSON: {exc}"]}

    try:
        filled = validate_and_fill_plan(plan)
    except ValueError as exc:
        # Strip the "Invalid WeeklyPlan: " prefix and split back into a list.
        msg = str(exc)
        detail = msg.split("Invalid WeeklyPlan: ", 1)[-1]
        errors = [e.strip() for e in detail.split(";") if e.strip()]
        return {"status": "invalid", "errors": errors or [msg]}

    out_path = Path(path) if path else config.PLAN_OUTPUT_PATH

    # The destination is a plain local folder; _atomic_write_json creates parent
    # dirs and writes atomically. If the write genuinely fails (e.g. permissions),
    # hand back the filled plan so the user can still share it manually.
    try:
        _atomic_write_json(out_path, filled)
    except OSError as exc:
        return {
            "status": "error",
            "hint": f"Could not write to {out_path}: {exc}. Share plan_json "
                    "manually instead.",
            "plan_json": filled,
        }

    return {
        "status": "saved",
        "plan_id": filled["plan_id"],
        "week_of": filled["week_of"],
        "planned_count": len(filled["planned"]),
        "path": str(out_path),
        "plan_json": filled,
    }


@mcp.tool(annotations=RO,
          description="Read back the currently-saved WeeklyPlan (the plan.json "
                      "save_weekly_plan last wrote to the local plans folder, "
                      "~/Documents/AppleFitnessPlans/ by default). Use it to "
                      "answer 'what's my current plan?'. Returns {status:'none'} "
                      "when no plan has been saved yet.")
def get_weekly_plan(path: Optional[str] = None) -> dict:
    in_path = Path(path) if path else config.PLAN_OUTPUT_PATH
    if not in_path.exists():
        return {"status": "none",
                "note": f"No saved plan at {in_path}. Compose one and call "
                        "save_weekly_plan."}
    try:
        with open(in_path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "error", "path": str(in_path),
                "error": f"Could not read saved plan: {exc}"}
    return {"status": "saved", "path": str(in_path), "plan_json": plan}


@mcp.tool(annotations=WRITE,
          description="Regenerate the training-progress dashboard: a single "
                      "self-contained HTML file (all data inlined, no network, "
                      "opens offline from disk) written to "
                      "~/Documents/AppleFitnessPlans/dashboard.html by default. "
                      "Four tabs: Last 30 days, Running, The engine, Load & "
                      "recovery. Call this after reload_data to refresh it with "
                      "new export data, or whenever the user asks to see or "
                      "update their dashboard. Corrects three defects in the raw "
                      "export that materially change the numbers — duplicate "
                      "workout records, sleep segments double-counted across "
                      "iPhone and Watch, and steps/energy summed over every "
                      "device — so its figures will not match a naive SUM over "
                      "the tables. Pass `path` to write elsewhere. Returns the "
                      "written path plus headline figures; tell the user to open "
                      "the path in a browser.")
def build_dashboard(path: Optional[str] = None) -> dict:
    _ensure_ready()
    try:
        return dashboard.build(path)
    except ValueError as exc:
        # Empty / unimported DB — actionable, not an internal error.
        return {"status": "empty", "error": str(exc),
                "hint": "Run reload_data (or `uv run apple-health-import`) first."}
    except OSError as exc:
        return {"status": "error",
                "error": f"Could not write the dashboard: {exc}"}


# --- LAN delta sync from the iPhone app ---------------------------------------
# The receiver (sync_receiver) runs on a background thread in THIS process, so
# receive and import never contend for DuckDB's single writer. The accepted
# consequence: the phone can only deliver while Claude Desktop is running. No
# data is lost when it is not — the app advances its HealthKit anchor only
# after a 200, so the samples simply arrive with the next successful sync.

_QR_BLOCKS = ("█", "▀", "▄", " ")   # full, upper, lower, empty


def _qr_matrix(data: str):
    """QR modules for `data`, with quiet zone, as rows of 0/1. Needs segno."""
    import segno

    qr = segno.make(data, error="m", micro=False)
    try:
        rows = [list(row) for row in qr.matrix_iter(border=4)]
    except Exception:                       # older/newer segno: build the border
        body = [list(row) for row in qr.matrix]
        width = len(body[0]) + 8
        rows = [[0] * width for _ in range(4)]
        rows += [[0] * 4 + row + [0] * 4 for row in body]
        rows += [[0] * width for _ in range(4)]
    return rows, getattr(qr, "version", None), getattr(qr, "error", None)


def _qr_lines(rows, invert: bool = False) -> list[str]:
    """Render 0/1 rows as half-block text: one character = 1x2 modules.

    Half blocks keep the modules square in a monospace font, which a scanner
    needs. Dark modules are drawn in the *foreground* colour, so the code reads
    correctly on a light background; `invert=True` swaps it for a dark one.
    """
    full, upper, lower, empty = _QR_BLOCKS
    out = []
    width = max(len(r) for r in rows)
    for y in range(0, len(rows), 2):
        top = rows[y]
        bottom = rows[y + 1] if y + 1 < len(rows) else [0] * width
        line = []
        for x in range(width):
            t = bool(top[x] if x < len(top) else 0) ^ invert
            b = bool(bottom[x] if x < len(bottom) else 0) ^ invert
            line.append(full if t and b else upper if t else lower if b else empty)
        out.append("".join(line))
    return out


def _listener_port() -> tuple[Optional[int], Optional[str]]:
    """(port, warning) — the port the pairing payload should advertise."""
    receiver = sync_receiver.get_receiver()
    if receiver is not None and receiver.running and receiver.port:
        return receiver.port, None
    detail = (receiver.listen_error if receiver is not None
              else sync_receiver.start_error()) or "the listener is not running"
    return None, (f"The sync listener is not up ({detail}), so this payload "
                  "carries no usable port. Fix it, restart Claude Desktop, and "
                  "call pair_device() again — the token itself stays valid.")


@mcp.tool(annotations=WRITE,
          description="Pair the Readiness iPhone app with this Mac for direct "
                      "LAN sync (the phone pushes HealthKit deltas straight "
                      "into the database, instead of the manual full export). "
                      "Generates the shared token if there is none, stores it "
                      "0600, and returns the pairing payload both as a QR to "
                      "scan and as raw JSON for manual entry. Safe to call "
                      "again — it re-displays the existing pairing. "
                      "rotate=true issues a NEW token and immediately locks out "
                      "the phone paired now, which must scan again. "
                      "invert=true redraws the QR for a dark background if a "
                      "scanner will not read the first one.")
def pair_device(rotate: bool = False, invert: bool = False) -> dict:
    existed = sync_pairing.is_paired()
    try:
        pairing = sync_pairing.ensure(rotate=rotate)
    except OSError as exc:
        return {"status": "error",
                "error": f"Could not write the pairing file: {exc}",
                "path": str(config.sync_pairing_path())}
    port, warning = _listener_port()
    payload = sync_pairing.pairing_payload(port or 0, pairing=pairing)
    payload_json = sync_pairing.pairing_json(port or 0, pairing=pairing)

    qr_lines: list[str] = []
    qr_error = None
    try:
        rows, version, _level = _qr_matrix(payload_json)
        qr_lines = _qr_lines(rows, invert=invert)
    except ImportError as exc:
        qr_error = (f"QR rendering needs the `segno` package ({exc}); run "
                    "`uv sync` in the project. Type the JSON payload into the "
                    "app's manual field instead.")
        version = None
    except Exception as exc:                      # never fail the pairing itself
        qr_error = f"QR rendering failed ({exc!r}); use the JSON payload."
        version = None

    result = {
        "status": "rotated" if (rotate and existed) else
                  ("existing" if existed and not rotate else "paired"),
        "device_id": pairing["device_id"],
        "token_fingerprint": sync_pairing.fingerprint(pairing["token"]),
        "pairing_json": payload_json,
        "payload": payload,
        "qr": "\n".join(qr_lines) if qr_lines else None,
        "qr_version": version,
        "qr_error": qr_error,
        "listener": {"port": port, "host": payload["host"],
                     "url": (sync_receiver.get_receiver().base_url()
                             if sync_receiver.get_receiver() else None)},
        "pairing_file": str(config.sync_pairing_path()),
        "next_steps": [
            "In the Readiness app, tap Pair and scan the QR (or paste the JSON).",
            "iOS will ask for Local Network permission the first time — it must "
            "be allowed or discovery silently finds nothing.",
            "Bring the app to the foreground to push; then call "
            "import_from_app() here.",
        ],
    }
    if warning:
        result["warning"] = warning
    if rotate and existed:
        result["note"] = ("The previous token is dead. The phone will get 401s "
                          "until it scans this code.")
    if qr_lines and not invert:
        result["qr_note"] = ("If the scanner will not read it, the chat theme "
                             "is inverting the code — call "
                             "pair_device(invert=true).")
    return result


@mcp.tool(annotations=WRITE,
          description="Import the delta batches the iPhone app has already "
                      "pushed to this Mac (they are spooled in "
                      "~/Documents/AppleHealthExport/deltas/pending) into the "
                      "database. This is the fast path — seconds, not the "
                      "minutes a full export re-parse takes. Idempotent: the "
                      "same batch imported twice adds nothing. With "
                      "wait_seconds>0 it polls the spool first, so you can say "
                      "'open the Readiness app now' and block briefly for the "
                      "result (cap 300s). Returns per-table added counts. "
                      "Status: 'imported', 'partial', 'empty', 'busy', "
                      "'error'. For a full export .zip use reload_data "
                      "instead.")
def import_from_app(wait_seconds: int = 0) -> dict:
    _ensure_ready()
    try:
        result = sync_import.import_pending(wait_seconds=wait_seconds)
    except Exception as exc:                      # never surface a traceback
        return {"status": "error", "error": f"Delta import failed: {exc!r}",
                "hint": "Call sync_status() — the batches are still spooled."}
    if result.get("status") == "empty":
        receiver = sync_receiver.get_receiver()
        result["listener_running"] = bool(receiver and receiver.running)
        result["paired"] = sync_pairing.is_paired()
        if not result["paired"]:
            result["hint"] = ("No device is paired yet — call pair_device() and "
                              "scan the QR with the Readiness app.")
        elif not result["listener_running"]:
            result["hint"] = ("The listener is not running, so the phone cannot "
                              "deliver. See sync_status().")
    return result


def _latest_samples(limit: int = 60) -> dict:
    """Newest sample per metric plus table totals, from one read-only handle."""
    out: dict[str, Any] = {}
    con = storage.connect_readonly()
    try:
        # The limit is interpolated, not bound: a parameter inside LIMIT is
        # not portable across DuckDB builds (cf. the `hours` reserved-word
        # trap), and this one is an int we clamp ourselves.
        cap = max(1, min(int(limit), 500))
        cur = con.execute(
            "SELECT type AS metric, max(start_ts) AS last_sample, "
            "count(*) AS rows_total FROM records GROUP BY type "
            f"ORDER BY last_sample DESC LIMIT {cap}")
        cols = [d[0] for d in cur.description]
        out["latest_per_type"] = [
            {c: (v.isoformat() if hasattr(v, "isoformat") else v)
             for c, v in zip(cols, row)} for row in cur.fetchall()]
        for name, table in (("workouts", "workouts"), ("sleep", "sleep"),
                            ("workout_events", "workout_events")):
            row = con.execute(
                f"SELECT max(start_ts) FROM {table}").fetchone()
            value = row[0] if row else None
            out[f"latest_{name}"] = (value.isoformat()
                                     if hasattr(value, "isoformat") else value)
        out["totals"] = storage.table_counts(con)
        out["last_delta_import"] = sync_import.last_delta_import(con)
    finally:
        con.close()
    return out


@mcp.tool(annotations=RO,
          description="Diagnose LAN sync with the iPhone app in one call: is "
                      "the listener bound and on which port, is Bonjour "
                      "advertising, is a device paired, when did the last "
                      "batch arrive and from where, how many batches are "
                      "waiting to be imported, how many failed, and the newest "
                      "sample timestamp per metric now in the database. Use it "
                      "whenever the user says data did not arrive — it also "
                      "returns a plain-language diagnosis of what is wrong and "
                      "what to do next.")
def sync_status() -> dict:
    _ensure_ready()
    receiver = sync_receiver.get_receiver()
    listener = (receiver.status() if receiver is not None else
                {"running": False, "port": None,
                 "error": sync_receiver.start_error() or
                          "the receiver was never started (is this an old "
                          "server process? restart Claude Desktop)",
                 "bonjour": {"advertising": False}, "counters": {}, "last": {}})
    pairing = sync_pairing.load()
    spool = sync_spool.Spool()
    try:
        spool_stats = spool.stats()
    except OSError as exc:
        spool_stats = {"error": str(exc), "dir": str(spool.root)}
    try:
        data = _latest_samples()
    except Exception as exc:
        data = {"error": f"Could not read the database: {exc}"}

    counters = listener.get("counters") or {}
    last = listener.get("last") or {}
    notes: list[str] = []
    if not pairing:
        notes.append("No device is paired. Call pair_device() and scan the QR "
                     "with the Readiness app.")
    if not listener.get("running"):
        notes.append(f"The listener is NOT running ({listener.get('error')}). "
                     "Nothing the phone sends can arrive until it is.")
    elif not (listener.get("bonjour") or {}).get("advertising"):
        notes.append("Bonjour is not advertising "
                     f"({(listener.get('bonjour') or {}).get('error')}). "
                     "Discovery will find nothing; enter the host and port from "
                     "pair_device() manually in the app.")
    if listener.get("running") and not counters.get("requests"):
        notes.append("No request has reached this listener at all. Check the "
                     "phone is on the same Wi-Fi, that iOS Local Network "
                     "permission was granted to the app, and that the app was "
                     "brought to the foreground.")
    if counters.get("unauthorized") and not counters.get("accepted"):
        notes.append("Requests arrived but every one was rejected as "
                     "unauthenticated — the phone is holding an old token. "
                     "Call pair_device(rotate=true) and scan again.")
    if counters.get("malformed"):
        notes.append(f"{counters['malformed']} request(s) were rejected as "
                     f"malformed; the last was {last.get('malformed')}.")
    if spool_stats.get("pending"):
        notes.append(f"{spool_stats['pending']} batch(es) are received but not "
                     "imported — call import_from_app().")
    if spool_stats.get("failed"):
        notes.append(f"{spool_stats['failed']} batch(es) failed to import and "
                     f"are kept in {spool_stats.get('dir')}/failed.")
    if not notes:
        notes.append("Sync looks healthy. Remember the receiver lives inside "
                     "this MCP process: the phone can only deliver while Claude "
                     "Desktop is running (nothing is lost meanwhile — the app "
                     "advances its HealthKit anchor only after a 200).")

    return {
        "listener": listener,
        "pairing": {
            "paired": bool(pairing),
            "device_id": (pairing or {}).get("device_id"),
            "token_fingerprint": sync_pairing.fingerprint((pairing or {}).get("token")),
            "created_at": (pairing or {}).get("created_at"),
            "rotated_at": (pairing or {}).get("rotated_at"),
            "file": str(config.sync_pairing_path()),
        },
        "spool": spool_stats,
        "data": data,
        "diagnosis": notes,
    }


def _start_sync_listener() -> None:
    """Bring up the LAN delta receiver. A failure here must never stop the
    MCP server from serving queries — it degrades to "listener unavailable",
    reported by sync_status()."""
    try:
        sync_receiver.start_receiver()
        atexit.register(sync_receiver.stop_receiver)
    except Exception:                       # pragma: no cover - belt and braces
        pass


def main() -> None:
    config.ensure_dirs()
    _ensure_ready()
    _start_sync_listener()
    try:
        mcp.run()
    finally:
        sync_receiver.stop_receiver()


if __name__ == "__main__":
    main()
