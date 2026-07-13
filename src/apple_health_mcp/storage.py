"""DuckDB storage layer: schema, idempotent import, dedup view."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Iterable, Tuple

import duckdb
import pyarrow as pa

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    row_hash        VARCHAR PRIMARY KEY,
    type            VARCHAR,
    raw_type        VARCHAR,
    source_name     VARCHAR,
    source_version  VARCHAR,
    device          VARCHAR,
    unit            VARCHAR,
    value           DOUBLE,
    value_str       VARCHAR,
    start_ts        TIMESTAMPTZ,
    end_ts          TIMESTAMPTZ,
    created_ts      TIMESTAMPTZ,
    source_priority INTEGER
);

CREATE TABLE IF NOT EXISTS workouts (
    row_hash        VARCHAR PRIMARY KEY,
    type            VARCHAR,
    raw_type        VARCHAR,
    source_name     VARCHAR,
    device          VARCHAR,
    duration        DOUBLE,
    duration_unit   VARCHAR,
    distance        DOUBLE,
    distance_unit   VARCHAR,
    energy          DOUBLE,
    energy_unit     VARCHAR,
    avg_hr          DOUBLE,
    max_hr          DOUBLE,
    start_ts        TIMESTAMPTZ,
    end_ts          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS sleep (
    row_hash        VARCHAR PRIMARY KEY,
    source_name     VARCHAR,
    device          VARCHAR,
    stage           VARCHAR,
    raw_value       VARCHAR,
    start_ts        TIMESTAMPTZ,
    end_ts          TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS activity_summary (
    date            VARCHAR PRIMARY KEY,
    active_energy   DOUBLE,
    active_energy_goal DOUBLE,
    active_energy_unit VARCHAR,
    exercise_minutes   DOUBLE,
    exercise_goal      DOUBLE,
    stand_hours     DOUBLE,
    stand_goal      DOUBLE
);

CREATE TABLE IF NOT EXISTS clinical (
    row_hash        VARCHAR PRIMARY KEY,
    type            VARCHAR,
    raw_type        VARCHAR,
    identifier      VARCHAR,
    source_name     VARCHAR,
    fhir_version    VARCHAR,
    received_ts     TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS import_runs (
    archive_name    VARCHAR,
    imported_at     TIMESTAMPTZ DEFAULT now(),
    records_total   BIGINT,
    workouts_total  BIGINT,
    sleep_total     BIGINT
);
"""

# Date-indexed for fast range queries on large datasets.
INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_records_type_start ON records(type, start_ts)",
    "CREATE INDEX IF NOT EXISTS idx_records_start ON records(start_ts)",
    "CREATE INDEX IF NOT EXISTS idx_workouts_start ON workouts(start_ts)",
    "CREATE INDEX IF NOT EXISTS idx_sleep_start ON sleep(start_ts)",
]

# Deduplicated view over raw records: when several sources report the same
# metric for the same time window, keep the highest-priority source only.
DEDUP_VIEW = """
CREATE OR REPLACE VIEW records_dedup AS
SELECT * EXCLUDE (rn) FROM (
    SELECT *,
        row_number() OVER (
            PARTITION BY type, start_ts, end_ts
            ORDER BY source_priority DESC, source_name
        ) AS rn
    FROM records
) WHERE rn = 1;
"""

BATCH = 50000


def connect(read_only: bool = False, retries: int = 8, delay: float = 0.5
            ) -> duckdb.DuckDBPyConnection:
    """Open the database, retrying briefly on a lock conflict.

    DuckDB allows a single read-write process OR multiple read-only processes,
    never both at once. The MCP server's read-only queries and an import
    (read-write, e.g. via the reload_data tool) can momentarily contend, so we
    retry with backoff instead of failing outright.
    """
    import time

    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(retries):
        try:
            return duckdb.connect(str(config.DB_PATH), read_only=read_only)
        except (duckdb.IOException, duckdb.Error) as exc:  # lock contention
            last = exc
            time.sleep(delay * (attempt + 1))
    raise last  # type: ignore[misc]


def connect_readonly(**kw) -> duckdb.DuckDBPyConnection:
    return connect(read_only=True, **kw)


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)
    for idx in INDEXES:
        con.execute(idx)
    con.execute(DEDUP_VIEW)


# --- hash expressions used for idempotent upserts (computed in DuckDB) ---
_REC_HASH = (
    "md5(concat_ws('|', coalesce(type,''), coalesce(source_name,''), "
    "coalesce(cast(start_ts as varchar),''), coalesce(cast(end_ts as varchar),''), "
    "coalesce(unit,''), coalesce(value_str,'')))"
)
_WORKOUT_HASH = (
    "md5(concat_ws('|', coalesce(type,''), coalesce(source_name,''), "
    "coalesce(cast(start_ts as varchar),''), coalesce(cast(end_ts as varchar),'')))"
)
_SLEEP_HASH = (
    "md5(concat_ws('|', coalesce(source_name,''), coalesce(raw_value,''), "
    "coalesce(cast(start_ts as varchar),''), coalesce(cast(end_ts as varchar),'')))"
)
_CLINICAL_HASH = (
    "md5(concat_ws('|', coalesce(type,''), coalesce(identifier,''), "
    "coalesce(source_name,'')))"
)


def _to_utc(v):
    """Normalize tz-aware datetimes to UTC so a batch has one timezone.

    Apple timestamps carry varying offsets (DST/travel); Arrow needs a single
    timezone per column. DuckDB TIMESTAMPTZ stores a UTC instant anyway, so this
    preserves the moment while making the column uniform.
    """
    if isinstance(v, datetime) and v.tzinfo is not None:
        return v.astimezone(timezone.utc)
    return v


def _flush(con, table: str, cols: list[str], rows: list[tuple], hash_expr: str,
           conflict_col: str = "row_hash") -> None:
    """Insert a batch of rows into `table`, ignoring duplicates.

    Uses DuckDB's Arrow zero-copy scan (columnar) rather than executemany — the
    latter is pathologically slow on real exports (millions of rows). The row
    hash is computed in-database and ON CONFLICT DO NOTHING gives idempotency.
    """
    if not rows:
        return
    col_defs = ", ".join(cols)
    columns = list(zip(*rows))  # transpose to columnar
    data = {col: [_to_utc(v) for v in columns[i]] for i, col in enumerate(cols)}
    arrow_tbl = pa.table(data)
    con.register("_stg_arrow", arrow_tbl)
    try:
        con.execute(
            f"INSERT INTO {table} "
            f"SELECT {hash_expr} AS {conflict_col}, {col_defs} FROM _stg_arrow "
            f"ON CONFLICT ({conflict_col}) DO NOTHING"
        )
    finally:
        con.unregister("_stg_arrow")


def import_stream(
    con: duckdb.DuckDBPyConnection,
    events: Iterable[Tuple[str, dict]],
    archive_name: str = "",
) -> dict:
    """Consume (kind, payload) tuples from the parser and upsert them.

    Returns a dict of per-table inserted-vs-seen counts. Idempotent: importing
    the same archive twice inserts nothing new the second time.
    """
    rec_cols = ["type", "raw_type", "source_name", "source_version", "device",
                "unit", "value", "value_str", "start_ts", "end_ts", "created_ts",
                "source_priority"]
    wk_cols = ["type", "raw_type", "source_name", "device", "duration",
               "duration_unit", "distance", "distance_unit", "energy",
               "energy_unit", "avg_hr", "max_hr", "start_ts", "end_ts"]
    sl_cols = ["source_name", "device", "stage", "raw_value", "start_ts", "end_ts"]
    cl_cols = ["type", "raw_type", "identifier", "source_name", "fhir_version",
               "received_ts"]

    rec_buf: list[tuple] = []
    wk_buf: list[tuple] = []
    sl_buf: list[tuple] = []
    cl_buf: list[tuple] = []
    counts = {"record": 0, "workout": 0, "sleep": 0, "activity_summary": 0,
              "clinical": 0}

    before = _table_counts(con)

    for kind, p in events:
        counts[kind] = counts.get(kind, 0) + 1
        if kind == "record":
            rec_buf.append((
                p["type"], p["raw_type"], p["source_name"], p["source_version"],
                p["device"], p["unit"], p["value"], p["value_str"], p["start"],
                p["end"], p["created"], config.source_priority(p["source_name"]),
            ))
            if len(rec_buf) >= BATCH:
                _flush(con, "records", rec_cols, rec_buf, _REC_HASH); rec_buf = []
        elif kind == "workout":
            wk_buf.append((
                p["type"], p["raw_type"], p["source_name"], p["device"],
                p["duration"], p["duration_unit"], p["distance"],
                p["distance_unit"], p["energy"], p["energy_unit"], p["avg_hr"],
                p["max_hr"], p["start"], p["end"],
            ))
            if len(wk_buf) >= BATCH:
                _flush(con, "workouts", wk_cols, wk_buf, _WORKOUT_HASH); wk_buf = []
        elif kind == "sleep":
            sl_buf.append((
                p["source_name"], p["device"], p["stage"], p["raw_value"],
                p["start"], p["end"],
            ))
            if len(sl_buf) >= BATCH:
                _flush(con, "sleep", sl_cols, sl_buf, _SLEEP_HASH); sl_buf = []
        elif kind == "clinical":
            cl_buf.append((
                p["type"], p["raw_type"], p["identifier"], p["source_name"],
                p["fhir_version"], p["received"],
            ))
            if len(cl_buf) >= BATCH:
                _flush(con, "clinical", cl_cols, cl_buf, _CLINICAL_HASH); cl_buf = []
        elif kind == "activity_summary":
            con.execute(
                "INSERT INTO activity_summary VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT (date) DO NOTHING",
                (p["date"], p["active_energy"], p["active_energy_goal"],
                 p["active_energy_unit"], p["exercise_minutes"], p["exercise_goal"],
                 p["stand_hours"], p["stand_goal"]),
            )

    _flush(con, "records", rec_cols, rec_buf, _REC_HASH)
    _flush(con, "workouts", wk_cols, wk_buf, _WORKOUT_HASH)
    _flush(con, "sleep", sl_cols, sl_buf, _SLEEP_HASH)
    _flush(con, "clinical", cl_cols, cl_buf, _CLINICAL_HASH)

    after = _table_counts(con)
    con.execute(
        "INSERT INTO import_runs (archive_name, records_total, workouts_total, "
        "sleep_total) VALUES (?,?,?,?)",
        (archive_name, after["records"], after["workouts"], after["sleep"]),
    )
    return {
        "seen": counts,
        "added": {t: after[t] - before[t] for t in after},
        "totals": after,
    }


def _table_counts(con) -> dict:
    out = {}
    for t in ("records", "workouts", "sleep", "clinical", "activity_summary"):
        out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    return out


def table_counts(con) -> dict:
    """Public: row counts per table, e.g. for reporting import results."""
    return _table_counts(con)
