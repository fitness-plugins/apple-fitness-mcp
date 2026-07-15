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

from . import config, import_pipeline, storage

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
