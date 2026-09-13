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

-- Structure *inside* a workout: one row per <WorkoutActivity> (a repetition
-- of a structured Apple Watch workout-builder session) and per <WorkoutEvent>
-- (segment / pause / resume / marker boundary). `workout_hash` repeats the
-- parent workout's row_hash, recomputed with the identical expression.
-- NOTE: this table is faithful to the export, which means it holds one copy of
-- every step PER duplicated workout row. Query `workout_events_dedup` instead.
CREATE TABLE IF NOT EXISTS workout_events (
    row_hash            VARCHAR PRIMARY KEY,
    workout_hash        VARCHAR,
    workout_type        VARCHAR,
    workout_source_name VARCHAR,
    workout_start_ts    TIMESTAMPTZ,
    workout_end_ts      TIMESTAMPTZ,
    event_kind          VARCHAR,
    event_type          VARCHAR,
    raw_event_type      VARCHAR,
    step_index          INTEGER,
    activity_uuid       VARCHAR,
    step_key_path       VARCHAR,
    step_block          INTEGER,
    step_repeat         INTEGER,
    step_slot           INTEGER,
    step_successful     BOOLEAN,
    start_ts            TIMESTAMPTZ,
    end_ts              TIMESTAMPTZ,
    duration            DOUBLE,
    duration_unit       VARCHAR,
    distance            DOUBLE,
    distance_unit       VARCHAR,
    energy              DOUBLE,
    energy_unit         VARCHAR,
    avg_hr              DOUBLE,
    min_hr              DOUBLE,
    max_hr              DOUBLE,
    avg_speed           DOUBLE,
    speed_unit          VARCHAR,
    avg_power           DOUBLE,
    power_unit          VARCHAR,
    step_count          DOUBLE,
    elevation_ascended  DOUBLE,
    elevation_unit      VARCHAR,
    stats_json          VARCHAR
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
    "CREATE INDEX IF NOT EXISTS idx_workout_events_workout "
    "ON workout_events(workout_hash)",
    "CREATE INDEX IF NOT EXISTS idx_workout_events_start "
    "ON workout_events(start_ts)",
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

# One physical step, once. The same session is exported repeatedly and lands in
# `workouts` 2-3 times under different `source_name`s (a watch rename alone does
# it: this export carries both "Apple Watch — Maksim" and "Maksim's Apple
# Watch"). Each copy brings its own full set of structural children, so a
# 5x1000 m session's ten repetitions become twenty or thirty rows in
# `workout_events`. Callers must read THIS VIEW, not the base table — the same
# split the project already makes between `records` and `records_dedup`.
#
# The partition key is the row identity minus `workout_source_name`:
#   * `event` / `activity` rows are uniquely placed by step_index + timing
#     within their workout, so `activity_uuid` is deliberately left out of the
#     key — dedup then works even if Apple were ever to re-issue a UUID.
#   * `activity_event` rows genuinely need it: nested <WorkoutEvent>s are copies
#     of the workout-level segments that overlap their activity, so the same
#     segment appears at the same step_index under several activities (verified
#     on the real export: one segment shared by three activities). Without the
#     uuid those distinct rows would collapse into one.
# Ties are broken on `workout_source_name` for determinism; copies of one step
# carry identical statistics, so the choice only decides which
# `workout_hash` / `workout_source_name` is surfaced.
WORKOUT_EVENT_DEDUP_VIEW = """
CREATE OR REPLACE VIEW workout_events_dedup AS
SELECT * EXCLUDE (rn) FROM (
    SELECT *,
        row_number() OVER (
            PARTITION BY workout_type, workout_start_ts, workout_end_ts,
                         event_kind, raw_event_type, step_index,
                         start_ts, end_ts, duration,
                         CASE WHEN event_kind = 'activity_event'
                              THEN activity_uuid END
            ORDER BY workout_source_name
        ) AS rn
    FROM workout_events
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
    con.execute(WORKOUT_EVENT_DEDUP_VIEW)


# --- row identity ------------------------------------------------------------
#
# Row identity must be reproducible from *values*, never from the way one
# ingestion path happens to render them as text. Two paths feed these tables:
# the iPhone's XML full export, and the delta the Readiness app pushes from
# HealthKit. A single digit of textual disagreement between them turns every
# delta-synced sample into a duplicate of its XML-synced twin, silently. Two
# rules follow, and every hash below obeys both.
#
# 1. TIMESTAMPTZ is rendered as UTC, to the second — never `cast(ts AS VARCHAR)`.
#
#    That cast goes through DuckDB's ICU cast (ICUStrftime::CastToVarchar),
#    which formats in the *session* timezone. The stored instant is correct, but
#    its string form — and therefore the hash — depends on the TimeZone setting
#    of whichever process performed the insert. The same sample imported under
#    `SET TimeZone='UTC'` and under `SET TimeZone='Europe/Moscow'` gets two
#    different row_hashes. Latent while everything runs in one zone; a live bug
#    the first time the user travels or a scheduled job runs elsewhere.
#    `AT TIME ZONE 'UTC'` converts to a plain TIMESTAMP first, which no session
#    setting can move, and `strftime` with an explicit format then drops any
#    sub-second component. The truncation is deliberate: the XML export only
#    ever carries whole seconds (`normalize._TS_FORMATS`) while an `HKSample`
#    date is an NSDate with sub-second precision, so truncating is what lets one
#    sample hash identically from both paths.
#
# 2. A numeric value is rendered to a fixed number of *significant* digits.
#    Apple's own `value_str` is used only where there is no number at all.
#
#    `value_str` is the raw XML attribute — Apple's formatting, verbatim. The
#    app is handed an `HKQuantity` and cannot reliably reproduce it ("72" vs
#    "72.0", "0.00123" vs "1.23e-03"), so it cannot be the identity of a
#    quantity sample. But ~75.8k rows carry `value IS NULL` with a non-null
#    `value_str`: the category types (sleep_analysis, stand_hour,
#    audio_exposure_event, mindful_session, high_heart_rate_event), where
#    `value_str` holds the category name and *is* the correct identity. Hence
#    the CASE: numeric when a number exists, Apple's string otherwise.
#
#    Precision: VALUE_SIG_DIGITS significant digits, via printf('%.5e', ...)
#    -> "1.23456e-03". Significant digits, not decimal places, because one rule
#    has to serve `step_count` (500) and `walking_asymmetry_percentage`
#    (0.000241262) at once — a fixed 6 decimal places would round the sub-unit
#    percentages into a handful of buckets, and a fixed 12 would preserve float
#    noise in the counts. Six is chosen against the two error sources that
#    actually exist in this data:
#      * Apple writes ~6 significant digits for derived quantities
#        (sum="1.50851", average="164.01"), so rounding at 6 is a no-op for
#        them: the operation is idempotent on values already stored.
#      * Values that originate as float32 arrive widened, with noise past the
#        7th digit ("45.612300872802734"). Rounding below float32's ~7.2-digit
#        precision normalizes that away instead of hashing it, which is exactly
#        what makes the XML and HealthKit renderings of one sample agree.
#    The cost: two samples of the same type, from the same source, in the same
#    second, whose values agree to 6 significant digits, become one row. That is
#    a duplicate by any reasonable definition — and `scripts/migrate_row_hash.py`
#    counts and reports every such collapse, per table, before it is allowed to
#    touch the database.
#
# Changing anything below changes every row_hash in the database. It must be
# accompanied by a run of scripts/migrate_row_hash.py.

VALUE_SIG_DIGITS = 6
# printf's %e prints one digit before the point, so N significant digits is
# N-1 after it.
VALUE_FORMAT = "%.{}e".format(VALUE_SIG_DIGITS - 1)
UTC_TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def utc_ts_expr(col: str) -> str:
    """SQL rendering one TIMESTAMPTZ column as canonical UTC, to the second.

    Session-timezone independent by construction — see rule 1 above. Kept as a
    function so every hash expression is built from the same string and cannot
    drift apart.

    The explicit CAST pins the operand's type and is load-bearing, not
    decoration. `_flush` stages rows through Arrow, and pyarrow infers the type
    of a column that is NULL for the *whole batch* as `null` rather than as a
    timestamp. DuckDB then sees an untyped NULL, and `NULL AT TIME ZONE 'UTC'`
    resolves to TIME WITH TIME ZONE — for which no `strftime` overload exists,
    so the insert dies with a BinderException instead of hashing a NULL to ''.
    Reachable with real data: any small delta batch whose rows all lack a
    timestamp, and the `clinical` table. Casting first makes the branch
    TIMESTAMPTZ -> TIMESTAMP in every case. On a column that is already
    TIMESTAMPTZ the cast is an identity, so hashes of existing rows are
    unchanged (pinned by tests/test_row_hash.py).
    """
    return (f"strftime((CAST({col} AS TIMESTAMP WITH TIME ZONE) "
            f"AT TIME ZONE 'UTC'), '{UTC_TS_FORMAT}')")


def value_id_expr(value_col: str, str_col: str | None = None) -> str:
    """SQL rendering a numeric identity component (see rule 2 above).

    `str_col` is the column holding Apple's own text for rows that have no
    number (the category types); pass None where there is no such column, and
    a missing number contributes an empty string instead.
    Never returns NULL, so `concat_ws` keeps its positions.
    """
    fallback = f"coalesce({str_col},'')" if str_col else "''"
    return (f"CASE WHEN {value_col} IS NULL THEN {fallback} "
            f"ELSE printf('{VALUE_FORMAT}', {value_col}) END")


# --- hash expressions used for idempotent upserts (computed in DuckDB) ---
_REC_HASH = (
    "md5(concat_ws('|', coalesce(type,''), coalesce(source_name,''), "
    f"coalesce({utc_ts_expr('start_ts')},''), "
    f"coalesce({utc_ts_expr('end_ts')},''), "
    "coalesce(unit,''), "
    f"{value_id_expr('value', 'value_str')}))"
)


def _workout_hash_expr(type_col: str = "type", source_col: str = "source_name",
                       start_col: str = "start_ts", end_col: str = "end_ts") -> str:
    """The workout identity hash, over arbitrarily-named columns.

    Single source of truth: `workouts.row_hash` and `workout_events.workout_hash`
    MUST come from this same expression or the child rows cannot be joined back
    to their parent. The child payload carries the parent's identity fields
    (type / source_name / start / end) precisely so this can be recomputed.
    """
    return (
        "md5(concat_ws('|', "
        f"coalesce({type_col},''), coalesce({source_col},''), "
        f"coalesce({utc_ts_expr(start_col)},''), "
        f"coalesce({utc_ts_expr(end_col)},'')))"
    )


_WORKOUT_HASH = _workout_hash_expr()

# Same expression, over the staging column names used for workout_events.
_WORKOUT_EVENT_PARENT_HASH = _workout_hash_expr(
    "workout_type", "workout_source_name", "workout_start_ts", "workout_end_ts"
)

# A workout can carry several byte-identical sibling events (this export has a
# workout with six Marker events sharing one instant), so document order
# (`step_index`) is part of the identity — otherwise they would collapse into a
# single row. Export order is deterministic, so re-importing stays idempotent.
# `duration` is normalized like any other number: the XML writes it with 16
# digits ("5.541540004809698") while HealthKit hands the app a TimeInterval, and
# `cast(duration AS VARCHAR)` would hash that disagreement.
_WORKOUT_EVENT_HASH = (
    "md5(concat_ws('|', " + _WORKOUT_EVENT_PARENT_HASH + ", "
    "coalesce(event_kind,''), coalesce(raw_event_type,''), "
    "coalesce(activity_uuid,''), coalesce(cast(step_index as varchar),''), "
    f"coalesce({utc_ts_expr('start_ts')},''), "
    f"coalesce({utc_ts_expr('end_ts')},''), "
    f"{value_id_expr('duration')}))"
)
_SLEEP_HASH = (
    "md5(concat_ws('|', coalesce(source_name,''), coalesce(raw_value,''), "
    f"coalesce({utc_ts_expr('start_ts')},''), "
    f"coalesce({utc_ts_expr('end_ts')},'')))"
)
# Unchanged by the normalization above, and deliberately so: it carries no
# timestamp and no numeric value, only three strings. Listed here (and covered
# by the migration's verification pass) so the omission is visibly a decision.
_CLINICAL_HASH = (
    "md5(concat_ws('|', coalesce(type,''), coalesce(identifier,''), "
    "coalesce(source_name,'')))"
)

# Every hash expression, by the table it identifies. Consumed by
# scripts/migrate_row_hash.py and by the tests that prove each one binds
# against the DuckDB build the server actually ships.
HASH_EXPRESSIONS = {
    "records": _REC_HASH,
    "workouts": _WORKOUT_HASH,
    "workout_events": _WORKOUT_EVENT_HASH,
    "sleep": _SLEEP_HASH,
    "clinical": _CLINICAL_HASH,
}

# Columns other than row_hash whose value is a hash computed in SQL rather than
# staged data. Same shape as `_flush(select_exprs=...)`.
DERIVED_HASH_COLUMNS = {
    "workout_events": {"workout_hash": _WORKOUT_EVENT_PARENT_HASH},
}


# Staging (and insert) column order for workout_events. `workout_hash` is not
# staged — it is computed in-database from the four parent identity columns.
WORKOUT_EVENT_COLS = [
    "workout_type", "workout_source_name", "workout_start_ts", "workout_end_ts",
    "event_kind", "event_type", "raw_event_type", "step_index", "activity_uuid",
    "step_key_path", "step_block", "step_repeat", "step_slot", "step_successful",
    "start_ts", "end_ts", "duration", "duration_unit", "distance",
    "distance_unit", "energy", "energy_unit", "avg_hr", "min_hr", "max_hr",
    "avg_speed", "speed_unit", "avg_power", "power_unit", "step_count",
    "elevation_ascended", "elevation_unit", "stats_json",
]

_TS_TYPE = pa.timestamp("us", tz="UTC")
_STR = pa.string()
_F64 = pa.float64()
_I32 = pa.int32()

# An explicit Arrow schema, unlike the inferred one used for the other tables:
# whole columns here are legitimately all-NULL (a plain <WorkoutEvent> has no
# statistics), and pyarrow would infer the `null` type for those, which is not
# reliably castable on insert. Naming the type keeps every batch well-typed.
WORKOUT_EVENT_ARROW_SCHEMA = pa.schema([
    ("workout_type", _STR), ("workout_source_name", _STR),
    ("workout_start_ts", _TS_TYPE), ("workout_end_ts", _TS_TYPE),
    ("event_kind", _STR), ("event_type", _STR), ("raw_event_type", _STR),
    ("step_index", _I32), ("activity_uuid", _STR), ("step_key_path", _STR),
    ("step_block", _I32), ("step_repeat", _I32), ("step_slot", _I32),
    ("step_successful", pa.bool_()),
    ("start_ts", _TS_TYPE), ("end_ts", _TS_TYPE),
    ("duration", _F64), ("duration_unit", _STR),
    ("distance", _F64), ("distance_unit", _STR),
    ("energy", _F64), ("energy_unit", _STR),
    ("avg_hr", _F64), ("min_hr", _F64), ("max_hr", _F64),
    ("avg_speed", _F64), ("speed_unit", _STR),
    ("avg_power", _F64), ("power_unit", _STR), ("step_count", _F64),
    ("elevation_ascended", _F64), ("elevation_unit", _STR),
    ("stats_json", _STR),
])

# Insert list for workout_events: the computed parent hash, then the staged
# columns in order. Paired with WORKOUT_EVENT_SELECT below.
WORKOUT_EVENT_INSERT_COLS = ["workout_hash"] + WORKOUT_EVENT_COLS
WORKOUT_EVENT_SELECT = {"workout_hash": _WORKOUT_EVENT_PARENT_HASH}


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
           conflict_col: str = "row_hash", insert_cols: list[str] | None = None,
           select_exprs: dict | None = None, arrow_schema=None) -> None:
    """Insert a batch of rows into `table`, ignoring duplicates.

    Uses DuckDB's Arrow zero-copy scan (columnar) rather than executemany — the
    latter is pathologically slow on real exports (millions of rows). The row
    hash is computed in-database and ON CONFLICT DO NOTHING gives idempotency.

    `cols` names the staged (Arrow) columns, in the order `rows` tuples carry
    them. The optional arguments exist for workout_events, whose `workout_hash`
    is *derived* in SQL rather than staged:
      * `insert_cols`   — target columns, when they differ from `cols`
      * `select_exprs`  — per-column SQL overriding the bare staged column
      * `arrow_schema`  — explicit types, for batches with all-NULL columns
    With none of them supplied the emitted SQL is exactly as it always was.
    """
    if not rows:
        return
    columns = list(zip(*rows))  # transpose to columnar
    data = {col: [_to_utc(v) for v in columns[i]] for i, col in enumerate(cols)}
    arrow_tbl = pa.table(data, schema=arrow_schema) if arrow_schema is not None \
        else pa.table(data)
    con.register("_stg_arrow", arrow_tbl)
    try:
        if insert_cols is None and select_exprs is None:
            col_defs = ", ".join(cols)
            sql = (
                f"INSERT INTO {table} "
                f"SELECT {hash_expr} AS {conflict_col}, {col_defs} FROM _stg_arrow "
                f"ON CONFLICT ({conflict_col}) DO NOTHING"
            )
        else:
            out_cols = insert_cols if insert_cols is not None else cols
            exprs = select_exprs or {}
            # Quote every identifier. A column name that happens to be reserved
            # in the DuckDB build the server ships (the `hours` trap) would
            # otherwise parse here and fail there.
            select_list = ", ".join(
                f'{exprs.get(c) or chr(34) + c + chr(34)} AS "{c}"'
                for c in out_cols
            )
            target = ", ".join(f'"{c}"' for c in [conflict_col] + out_cols)
            sql = (
                f"INSERT INTO {table} ({target}) "
                f"SELECT {hash_expr} AS {conflict_col}, {select_list} "
                f"FROM _stg_arrow "
                f"ON CONFLICT ({conflict_col}) DO NOTHING"
            )
        con.execute(sql)
    finally:
        con.unregister("_stg_arrow")


def workout_event_row(p: dict) -> tuple:
    """Parser `workout_event` payload -> a tuple in WORKOUT_EVENT_COLS order."""
    return (
        p["workout_type"], p["workout_source_name"], p["workout_start"],
        p["workout_end"], p["event_kind"], p["event_type"], p["raw_event_type"],
        p["step_index"], p["activity_uuid"], p["step_key_path"], p["step_block"],
        p["step_repeat"], p["step_slot"], p["step_successful"], p["start"],
        p["end"], p["duration"], p["duration_unit"], p["distance"],
        p["distance_unit"], p["energy"], p["energy_unit"], p["avg_hr"],
        p["min_hr"], p["max_hr"], p["avg_speed"], p["speed_unit"],
        p["avg_power"], p["power_unit"], p["step_count"],
        p["elevation_ascended"], p["elevation_unit"], p["stats_json"],
    )


def flush_workout_events(con, rows: list[tuple]) -> None:
    """Batch-insert staged workout_event rows (idempotent)."""
    _flush(con, "workout_events", WORKOUT_EVENT_COLS, rows,
           _WORKOUT_EVENT_HASH,
           insert_cols=WORKOUT_EVENT_INSERT_COLS,
           select_exprs=WORKOUT_EVENT_SELECT,
           arrow_schema=WORKOUT_EVENT_ARROW_SCHEMA)


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
    we_buf: list[tuple] = []
    sl_buf: list[tuple] = []
    cl_buf: list[tuple] = []
    counts = {"record": 0, "workout": 0, "workout_event": 0, "sleep": 0,
              "activity_summary": 0, "clinical": 0}

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
        elif kind == "workout_event":
            we_buf.append(workout_event_row(p))
            if len(we_buf) >= BATCH:
                flush_workout_events(con, we_buf); we_buf = []
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
    flush_workout_events(con, we_buf)
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
    for t in ("records", "workouts", "workout_events", "sleep", "clinical",
              "activity_summary"):
        out[t] = con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
    return out


def table_counts(con) -> dict:
    """Public: row counts per table, e.g. for reporting import results."""
    return _table_counts(con)
