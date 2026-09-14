# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **fully local** MCP (stdio) server that exposes Apple Health export data to
Claude Desktop via DuckDB. No cloud, no network calls, no telemetry. Apple has no
Health API — data only comes from a manual **Export All Health Data** on the
iPhone, dropped into `~/Documents/AppleHealthExport`; the Mac imports the
`export.xml` inside.

## Environment & commands

Uses **uv** (not pip). Python is pinned to **3.12** in `.python-version` — the
machine default may be 3.14, which lacks some wheels (duckdb). Always run through
`uv`:

```bash
uv sync                                   # create .venv + install (editable) deps
uv run pytest -q                          # run all tests
uv run pytest tests/test_parser.py::test_get_sleep_wake_day_and_filter_consistency -q  # single test
uv run apple-health-import [path.zip]     # import newest (or specific) export
uv run python scripts/selfcheck.py        # spawn server over stdio, call list_metrics
uv run python -c "from apple_health_mcp import dashboard; print(dashboard.build())"
                                          # rebuild the dashboard without Claude
./setup.sh                                # full install: venv, schema, config merge, restart Claude, self-check
```

There is **no lint step** configured. Pyright diagnostics that say
`duckdb`/`pyarrow`/`config` "could not be resolved" are false positives from an
editor using the system interpreter instead of `.venv` — ignore them; verify by
running under `uv`.

## Architecture (the big picture)

Flow: **iPhone Health export → ~/Documents/AppleHealthExport → on-demand import → DuckDB → MCP
server → Claude Desktop**. Nothing runs in the background; data is imported only
when asked (the `reload_data` tool, `apple-health-import`, or `./setup.sh`).

`src/apple_health_mcp/` (imported as the package via editable install):

- **config.py** — single source of truth for absolute paths (`DB_PATH`,
  `EXPORT_DIR`) and the source-priority dedup map (Watch > iPhone > iPad).
  Everything resolves absolutely so it behaves identically from shell, Claude
  Desktop, or a subprocess with an unknown cwd. Env overrides: `HEALTH_DB`,
  `HEALTH_EXPORT_DIR` (used by tests).
- **parser.py** — streaming `ElementTree.iterparse` (NOT `parse`); exports are
  200–800 MB / millions of records. Yields `(kind, payload)` in one pass and
  detaches processed nodes from the root to keep memory flat. Tolerates a
  truncated file (stops cleanly, keeps what parsed).
- **normalize.py** — HealthKit identifier → snake_case (`StepCount` →
  `step_count`), unit/timestamp handling, sleep-stage mapping.
- **storage.py** — DuckDB schema, indexes, idempotent upsert, and the
  `records_dedup` view. Import goes through `_flush` using DuckDB's **Arrow
  columnar insert**, not `executemany`.
- **import_pipeline.py** — unzip → locate `export.xml` (ignore `export_cda.xml`)
  → stream import. `reload()` (used by the MCP tool) wraps this in `import_lock`
  (an `fcntl` advisory lock) and returns a status dict. Idempotent via an archive
  fingerprint in `data/import_state.json` plus per-row hashes.
- **server.py** — `FastMCP` server. Query tools are read-only
  (`ToolAnnotations(readOnlyHint=True)`); there are two write tools
  (`readOnlyHint=False`, both non-destructive): `reload_data` (imports new data)
  and `save_weekly_plan` (writes a WeeklyPlan JSON to a plain local folder,
  `~/Documents/AppleFitnessPlans/plan.json` by default, that the user
  AirDrops/shares to the iOS app — no iCloud — see `config.PLAN_OUTPUT_PATH`;
  `get_weekly_plan` reads it back).
  `run_sql` is SELECT-only (validated +
  read-only connection). Tables/views: `records`, `records_dedup`, `workouts`,
  `sleep`, `activity_summary`, `clinical`.
  The third write tool is `build_dashboard` (`readOnlyHint=False`, idempotent).
- **dashboard.py** — regenerates the progress dashboard: queries the DB
  read-only, applies the three corrections below, derives the training metrics,
  and writes one self-contained HTML file (atomic temp-file + `os.replace`) to
  `config.DASHBOARD_OUTPUT_PATH`
  (`~/Documents/AppleFitnessPlans/dashboard.html`, override `HEALTH_DASHBOARD_PATH`).
  Safe to call while the server is serving queries.
- **dashboard_template.py** — the HTML/CSS/JS shell as one constant, split out
  purely for size. Everything is inlined: **never add a CDN import or a `<link>`
  here**, the output must open offline from a `file://` URL. A test asserts it.

`scripts/merge_config.py` merges the server entry into
`~/Library/Application Support/Claude/claude_desktop_config.json` (backs it up
first, preserves existing keys). `scripts/selfcheck.py` proves the server works
by doing a real stdio round-trip.

## Non-obvious gotchas (learned the hard way)

- **Never use `con.executemany` for bulk insert.** It hits a DuckDB perf cliff
  (`TransformPreparedParameters`) — a real 57 MB export stalled for 8+ min. The
  Arrow path in `storage._flush` does 2.3M rows in ~1 min. `_to_utc` normalizes
  tz-aware datetimes so a batch has one Arrow timezone.
- **`pytz` is a required dependency** — DuckDB needs it to materialize
  `TIMESTAMPTZ` columns back into Python (any query selecting a raw timestamp
  fails without it).
- **DuckDB forbids mixing read-write and read-only handles to one file within a
  process.** The server opens read-only per query and closes; schema init runs
  once (`server._schema_ready` flag), never per-call. `storage.connect()` retries
  on lock contention.
- **Sleep is dated by WAKE day**, not bedtime. `get_sleep` groups and filters on
  the *same* expression `(start_ts + INTERVAL 6 HOUR)::DATE` (sleep-day runs
  18:00→18:00). Filtering and grouping MUST use the same day-definition or a date
  query returns a differently-labelled night — the bug that motivated the fix.
- **MCP tool results:** plain-`dict` returns land in the result's text `content`
  as JSON; `structuredContent` is `None`. Read `content[0].text` when inspecting
  results programmatically (this is what Claude Desktop reads too).
- **`data/` (the 777 MB health DB), `logs/`, real exports, and
  `.claude/settings.local.json` are git-ignored.** Never commit them; always
  verify the add-set before pushing.

## The export lies in three ways — `dashboard.py` corrects them

Anything computing training metrics must do the same, or its numbers will be
confidently wrong. A naive `SUM` over these tables is not the truth.

- **Workouts are stored 2–3 times.** Repeated exports, plus any watch rename,
  create copies of one session under different `row_hash`es and sometimes
  different `source_name`s. Identity is `(type, start minute)` — dedupe with
  `DISTINCT ON`. On the real export this is **280 rows → 142 workouts**; taken at
  face value the file claims 78 runs when there are 41, and doubles weekly
  mileage.
- **Sleep segments overlap across devices.** iPhone and Watch both log the night
  and their segments intersect, so `SUM(end - start)` gives **~15 h a night**.
  Take the interval *union* (gaps-and-islands: a new island starts when a segment
  begins after the running `MAX(end_ts)` before it) → the real ~7.5 h.
- **Steps and energy are counted by every device at once.** Summing all sources
  gives ~30,000 steps/day. Take the single highest-recording source per day
  (~14,000).

Two smaller traps in the same file:

- **`hours` is a reserved word in some DuckDB builds** and cannot be a column
  alias — it parses in 1.5.x but fails in the version the server ships. Use
  `sleep_hours`. Test in the container ≠ test in the server.
- **`sorted(xs)[int(len(xs) * 0.95)]` is not a 95th percentile** for small `n` —
  it returns the maximum, so the outlier the percentile exists to exclude sets
  the value anyway. Use `statistics.quantiles(..., n=20, method="inclusive")[18]`.
  This matters because functional HRmax (p95 of per-run maxima) sets every zone
  boundary downstream.

## Testing

Tests run against a synthetic `export.xml` fixture in `tests/test_parser.py`
(never real data). The `sandbox` fixture monkeypatches `config` paths to a
tmp DB/export dir; when a test calls `server.*`, reset `server._schema_ready =
False` so schema init targets the sandbox DB.
