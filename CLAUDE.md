# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A **fully local** MCP (stdio) server that exposes Apple Health export data to
Claude Desktop via DuckDB. No cloud, no network calls, no telemetry. Apple has no
Health API — data only comes from a manual **Export All Health Data** on the
iPhone, saved into an iCloud folder; the Mac imports the `export.xml` inside.

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
./setup.sh                                # full install: venv, schema, config merge, restart Claude, self-check
```

There is **no lint step** configured. Pyright diagnostics that say
`duckdb`/`pyarrow`/`config` "could not be resolved" are false positives from an
editor using the system interpreter instead of `.venv` — ignore them; verify by
running under `uv`.

## Architecture (the big picture)

Flow: **iPhone Health export → iCloud folder → on-demand import → DuckDB → MCP
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
  (`ToolAnnotations(readOnlyHint=True)`); `reload_data` is the one write tool
  (`readOnlyHint=False`, non-destructive). `run_sql` is SELECT-only (validated +
  read-only connection). Tables/views: `records`, `records_dedup`, `workouts`,
  `sleep`, `activity_summary`, `clinical`.

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

## Testing

Tests run against a synthetic `export.xml` fixture in `tests/test_parser.py`
(never real data). The `sandbox` fixture monkeypatches `config` paths to a
tmp DB/export dir; when a test calls `server.*`, reset `server._schema_ready =
False` so schema init targets the sandbox DB.
