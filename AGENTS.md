# AGENTS.md

Guidance for AI coding agents working in this repository. The full guide lives in
[CLAUDE.md](./CLAUDE.md) — read it. This file is a short pointer plus the rules
that most often trip agents up.

## Essentials

- **Tooling is `uv`, Python pinned to 3.12** (`.python-version`). Run everything
  via `uv` (`uv sync`, `uv run pytest -q`, `uv run apple-health-import`). The
  system Python may be 3.14 and lacks some wheels.
- **Local-only project.** No network calls, no cloud API for Apple Health. Data
  enters via a manual iPhone export; the Mac imports it on demand.
- **No lint step.** Editor "import could not be resolved" warnings for
  `duckdb`/`pyarrow`/`config` are false positives — verify by running under `uv`.

## Don't-break rules

- **Bulk insert must use the DuckDB Arrow path in `storage._flush`, never
  `con.executemany`** (executemany stalls for minutes on real exports).
- **Don't mix read-write and read-only DuckDB connections to one file in a
  process.** Schema init runs once (`server._schema_ready`); queries are
  read-only per call.
- **`get_sleep` filters and groups on the same wake-day expression**
  `(start_ts + INTERVAL 6 HOUR)::DATE`. Keep them identical.
- **`pytz` is a required runtime dep** (DuckDB needs it for `TIMESTAMPTZ`).
- **Never commit `data/`, `logs/`, real exports, or
  `.claude/settings.local.json`** — all git-ignored; verify the add-set before
  pushing.

## Verify your change

```bash
uv run pytest -q                    # all tests (synthetic fixtures only)
uv run python scripts/selfcheck.py  # stdio round-trip against the server
```
