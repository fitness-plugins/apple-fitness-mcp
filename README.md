# Apple Health MCP

A **fully local** MCP server that gives Claude Desktop read-only access to your
Apple Health data. Nothing about your health data ever leaves your Mac except
the specific answers you ask Claude for in chat.

Apple has **no cloud API** for Health — HealthKit data lives only on your
iPhone. The one unavoidable manual step is exporting your data from the **Health
app** (Health → profile → *Export All Health Data*) and getting the resulting
`export.zip` into `~/Documents/AppleHealthExport` on your Mac. Everything after
that — parsing 200–800 MB XML, loading DuckDB, serving it to Claude — is handled
locally.

> **Note:** This export is a **manual, repeatable** step, not a one-time
> setup-and-forget. iOS does not provide a reliable way to schedule a full
> Health export (the Shortcuts "Export Health Data" action is unavailable/
> unreliable across iOS versions). So each time you want Claude to see fresh
> data, you repeat the ~1-minute export below. The Mac side then imports it with
> zero further action.

## Architecture

```
┌──────────────────────┐
│  iPhone — Health app │  Export All Health Data (manual, ~1 min),
│  Export All Health   │  then get the export.zip onto the Mac —
│  Data → the Mac      │  AirDrop, or iCloud if ~/Documents syncs
│                      │
└──────────┬───────────┘
           │ export.zip
           ▼
┌──────────────────────┐
│  ~/Documents/        │  the drop-folder the Mac imports from
│  AppleHealthExport/  │
└──────────┬───────────┘
           │ you import it — reload_data / apple-health-import
           ▼
┌──────────────────────┐
│  Import pipeline     │  runs on demand (no background jobs):
│  (on request)        │   • ask Claude → reload_data tool
│                      │   • uv run apple-health-import
│                      │   • ./setup.sh
└──────────┬───────────┘
           │ unzip → streaming iterparse → normalize → upsert
           ▼
┌──────────────────────┐
│  DuckDB              │  data/health.duckdb
│  records / workouts /│  date-indexed, deduplicated view
│  sleep / clinical    │
└──────────┬───────────┘
           │ read-only SQL, per-call connection
           ▼
┌──────────────────────┐
│  MCP server (stdio)  │  FastMCP · read-only tools + reload_data
│  apple_health_mcp    │
└──────────┬───────────┘
           │ stdio
           ▼
┌──────────────────────┐
│  Claude Desktop      │  ask questions in plain English
└──────────────────────┘
```

## One setup command

```bash
./setup.sh
```

This does everything on the Mac side, with no placeholders to edit:

1. Creates the uv virtual environment and installs dependencies.
2. Initializes the DuckDB schema (`data/health.duckdb`) and imports any export
   already sitting in the folder.
3. Creates the export drop-folder (`~/Documents/AppleHealthExport`) if missing.
4. Backs up your existing `claude_desktop_config.json`, then merges in the MCP
   server entry with correct absolute paths.
5. Restarts Claude Desktop so the server is picked up.
6. Runs a self-check: actually spawns the server over stdio and calls
   `list_metrics`, reporting pass/fail.

No background jobs are installed — data is imported only when you ask (see
below). Re-running `./setup.sh` is safe and idempotent.

## The one manual step — export from the Health app

Health data can only be exported on the phone, and the only reliable way is the
built-in **Export All Health Data** in the Health app. It takes about a minute.
Repeat it whenever you want Claude to see fresher data.

### Export it (on your iPhone)

1. Open the **Health** app.
2. Tap your **profile picture / initials** in the top-right corner.
3. Scroll to the bottom and tap **Export All Health Data**.
4. Tap **Export** to confirm. iOS prepares a single `export.zip`
   (this can take a minute or more on a large history — that's normal).
5. When the **share sheet** appears, tap **Save to Files**.

### Where it needs to end up — the drop-folder

The Mac imports from this local folder (setup creates it):

```
~/Documents/AppleHealthExport/
```

Because it's a local folder, get the `export.zip` there in whichever way suits
you:

- **AirDrop** (simplest): in the iPhone share sheet after export, tap **AirDrop**
  → your Mac. Then move the received `export.zip` into
  `~/Documents/AppleHealthExport/`.
- **Save to Files → On My iPhone / iCloud**, then move it into that folder on the
  Mac.
- **If your `~/Documents` is synced to iCloud** (System Settings → Apple ID →
  iCloud → *Desktop & Documents Folders*), you can *Save to Files* directly to
  **iCloud Drive → Documents → AppleHealthExport** and it will sync down into the
  same folder — no manual move.

### Then import it (on the Mac) — nothing runs in the background

Once the `export.zip` is in `~/Documents/AppleHealthExport/`, import it whenever
you want, whichever is easiest:

- **Ask Claude:** *"reload my health data"* — Claude calls the `reload_data`
  tool, which imports the newest export from the folder and reports what changed.
- **Terminal:** `uv run apple-health-import`
- **Re-run setup:** `./setup.sh`

### Notes

- **Filename doesn't matter.** iOS names it `export.zip` (or `export-1.zip`,
  etc. if one already exists). The importer always picks the **newest** `.zip`
  in the folder and skips archives it has already imported.
- **Keeping the folder tidy is optional.** You can delete old `.zip` files after
  they've imported; the database already holds their data. Re-importing the same
  archive is harmless (it's idempotent — no duplicates).
- **Custom location.** Set the `HEALTH_EXPORT_DIR` environment variable to point
  the importer and `reload_data` at a different folder.

## MCP tools

All query tools are **read-only** (marked with `readOnlyHint`). The exceptions
are `reload_data`, which imports new data on demand (it only ever *adds*
records — non-destructive and idempotent), `save_weekly_plan`, and
`build_dashboard`, which writes an HTML file.

| Tool | What it returns | Example question to Claude |
|------|-----------------|----------------------------|
| `list_metrics` | Every metric, its record count, units, date range | "What health data do you have access to?" |
| `get_summary(metric, start_date, end_date, granularity)` | Day/week/month aggregates | "Summarize my active energy by week for the last 3 months." |
| `get_steps` | Daily step totals | "How many steps did I average last month?" |
| `get_heart_rate` | Daily heart-rate stats | "Has my resting heart rate trended up this year?" |
| `get_hrv` | Daily HRV (SDNN) | "Show my HRV trend over the last 90 days." |
| `get_sleep` | Nightly sleep by stage | "How much deep sleep am I getting on average?" |
| `get_weight` | Body weight over time | "Plot my weight for the past 6 months." |
| `get_vo2max` | VO2 max measurements | "Is my VO2 max improving?" |
| `get_workouts(start_date, end_date, type?)` | Workouts, optionally by type | "List my runs in May and their average heart rate." |
| `run_sql(query)` | Read-only SELECT over the DB | "Which weekday do I walk the most?" |
| `reload_data(force?)` | Imports the newest export from the drop-folder now | "I just exported fresh data — reload it." |
| `build_dashboard(path?)` | Regenerates the progress dashboard as one self-contained HTML file | "Rebuild my dashboard." |

`run_sql` accepts only a single `SELECT`/`WITH` statement; all DDL/DML is
rejected and the connection is opened read-only as a second safeguard.

Tables available to `run_sql`: `records`, `records_dedup` (deduplicated view),
`workouts`, `sleep`, `activity_summary`, `clinical`.

## The progress dashboard

`build_dashboard` writes a single self-contained HTML file — every style, script
and data point inlined — to `~/Documents/AppleFitnessPlans/dashboard.html`
(override with `HEALTH_DASHBOARD_PATH`, or pass `path`). It has no external
assets and makes no network requests, so it opens straight from disk and keeps
working offline. Four tabs: **Last 30 days**, **Running**, **The engine**
(resting HR, HRV, HR recovery, VO2 max), and **Load & recovery**.

Typical refresh, after dropping a new export in the folder:

```
"Reload my health data and rebuild the dashboard"
   -> reload_data() then build_dashboard()
```

Or without Claude at all:

```bash
uv run python -c "from apple_health_mcp import dashboard; print(dashboard.build())"
```

**Its numbers will not match a naive `SUM` over the tables, deliberately.** The
raw export is wrong in three ways that materially change training metrics, and
the dashboard corrects all three (see `src/apple_health_mcp/dashboard.py`):

1. **Workouts are stored 2–3 times** — repeated exports and watch renames create
   copies. Deduped on `(type, start minute)`. On a real export this is 280 rows
   → 142 workouts; uncorrected it claims 78 runs where there are 41, and doubles
   weekly mileage.
2. **Sleep segments overlap across iPhone and Watch** — summing them reports
   ~15 h a night. The night total is the interval *union*: ~7.5 h.
3. **Steps and energy are counted by every device at once** — summing all
   sources gives ~30,000 steps/day; taking the highest single source per day
   gives ~14,000.

## Deduplication

The iPhone and Apple Watch often report the same metric for overlapping times.
Raw rows are all preserved in `records`; a `records_dedup` view sits on top and
keeps the highest-priority source per `(type, start, end)` window. Default
priority is **Apple Watch > iPhone > iPad**, configurable in
`src/apple_health_mcp/config.py`.

## Importing data

Data is imported only when you ask — there are no background jobs. Any of these
work (all idempotent):

```bash
uv run apple-health-import                 # import newest archive in the folder
uv run apple-health-import /path/to.zip    # import a specific archive
uv run python -m apple_health_mcp.import_pipeline --force  # re-import even if unchanged
```

Or just ask Claude to run the `reload_data` tool ("reload my health data").

## Privacy

- **Everything is local.** The database is a plain file at `data/health.duckdb`
  on your Mac. The MCP server talks to Claude Desktop over stdio (a local pipe).
- **No network calls** are made by this server. It has no telemetry.
- **Nothing is uploaded** except the specific answers to questions you ask
  Claude in chat — the same as anything else you type into Claude.
- `data/`, logs, and any real export files are git-ignored so they can never be
  committed.
- **Nothing runs in the background.** Imports happen only when you trigger them,
  and they only ever **read** the export and **write** the local DB.

## Development

```bash
uv sync            # install deps + dev tools
uv run pytest -q   # run the parser/storage tests against synthetic fixtures
```

## Uninstall

No launchd agents or background jobs are installed, so there's nothing to unload.
To remove the server from Claude, delete the `apple-health` entry from
`~/Library/Application Support/Claude/claude_desktop_config.json` (a timestamped
backup is written next to it every time setup runs), then restart Claude Desktop.
Deleting the project folder removes everything else.
