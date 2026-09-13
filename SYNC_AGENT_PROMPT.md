# Build spec — direct LAN sync between the Readiness iOS app and the apple-fitness MCP server

You are implementing a feature that spans two repositories on the user's Mac. Read this whole
document before writing anything. Every design decision below was made deliberately by the user;
do not relitigate them, but **do** report if implementation reveals one of them is unworkable.

---

## 1. The problem being solved

The MCP server ingests Apple Health data into DuckDB and serves training analytics. Today the only
ingestion path is the iPhone Health app's **full export**: the user manually assembles a 62 MB zip
containing a 1.15 GB `export.xml`, drops it in a folder, and the server re-parses ~5 million records.

That work is almost entirely wasted. A representative import parses **5,007,365 records to insert
9,823 new rows** — 99.8% of the parse exists only to be discarded by `ON CONFLICT DO NOTHING`. The
database itself is already incremental and persistent; it is the *parse* that is full every time.
The phone-side export is also slow and manual.

The fix: the Readiness iOS app already holds HealthKit read authorization. `HKAnchoredObjectQuery`
with a persisted `HKQueryAnchor` returns **only samples added since the last sync** — a daily delta
of a few thousand samples, tens of kilobytes. Push that straight to the Mac over the local network.

The full-export path stays, as a repair and backfill mechanism. It is not being replaced, only
demoted to once-in-a-while.

---

## 2. Repositories and environment

| | |
|---|---|
| Python MCP server | `~/Documents/projects/apple-fitness-mcp` |
| SwiftUI iOS app "Readiness" | `~/Documents/projects/apple-fitness-ios` |
| Health export drop folder | `~/Documents/AppleHealthExport/` |
| Outputs folder (plan.json, dashboard.html) | `~/Documents/AppleFitnessPlans/` |
| Live database | `~/Documents/projects/apple-fitness-mcp/data/health.duckdb` (1.5 GB) |

**Read `CLAUDE.md` and `AGENTS.md` in the MCP repo root first.** They document conventions and two
past bugs that cost real time. One is directly relevant to you: *SQL that parses in a container can
fail in the DuckDB version the server actually ships* — `hours` turned out to be a reserved word.
Validate SQL against the shipped build, prefer boring explicit identifiers, and never assume.

If you are working through the `mcp__remote-devices__device_bash` bridge rather than natively on the
Mac, note: 45-second hard timeout per call, no network, `rm` is forbidden (use `mv` to a
`_to_delete/` folder), background processes are killed when the call returns, and the bridge VM has
Python 3.10 with stdlib only — no duckdb, no pyarrow, no pytest, and the repo `.venv` is
macOS-linked and unusable. In that case you cannot run the test suite; write tests anyway, and state
plainly in your report what you could not verify. The user runs `uv run pytest -q` himself.

---

## 3. Decisions already made

| Decision | Choice | Why |
|---|---|---|
| Direction | **Phone pushes, Mac receives** | An iOS app can only hold a listening socket in the foreground, so a Mac-initiated pull would only work while the user is staring at the app. |
| Receiver location | **Inside the MCP server process** | Fewer moving parts, and receive + import share one process so there is no DuckDB single-writer conflict. Accepted consequence below. |
| Row identity | **Numeric hash + one-time migration** | See §4. This is the load-bearing decision. |
| Auth | **Pre-shared token, QR pairing** | Without it, anyone on the same Wi-Fi can write into a health database the user makes training decisions from. |
| v1 trigger | **Foreground push only** | `HKObserverQuery` + background delivery is deferred to a later version. |

**Accepted consequence of the in-process receiver:** the phone can only deliver while Claude Desktop
is running. This is safe *only because* the anchor discipline in §6 guarantees no data loss when the
Mac is unreachable — the samples simply go on the next successful sync. Your implementation must make
that guarantee real, and the app must show the user when it last succeeded rather than failing
silently.

---

## 4. Part 0 — row-identity migration (do this first, it gates everything else)

### The problem

`storage.py` currently computes record identity as:

```
md5(concat_ws('|', type, source_name, cast(start_ts as varchar), cast(end_ts as varchar), unit, value_str))
```

`value_str` is **Apple's own string formatting, taken verbatim from the XML attribute**. HealthKit
gives the app an `HKQuantity`; the app would have to format the number back into a string that
matches Apple's XML rendering exactly, for every type and unit. A single digit of disagreement —
`72` vs `72.0`, `0.00123` vs `1.23e-03` — makes every delta-synced sample a duplicate of its
XML-synced twin. The database doubles silently and nobody notices for weeks.

### The fix

Replace `value_str` in the hash with a **normalized numeric value**, so both ingestion paths agree
regardless of formatting.

Constraint you must handle: **75,820 records have `value IS NULL` and a non-null `value_str`.** These
are the category types — `sleep_analysis` (44,286), `stand_hour` (31,482), `audio_exposure_event`,
`mindful_session`, `high_heart_rate_event`. For those, `value_str` carries the category name
(`HKCategoryValueSleepAnalysisAsleepCore` etc.) and remains the correct identity component. So the
expression must be conditional: numeric when a numeric value exists, `value_str` otherwise. Decide
the rounding precision deliberately and justify it — the smallest magnitudes in the data are
`walking_asymmetry_percentage` and similar sub-unit fractions.

### Second defect to fix in the same migration

`cast(start_ts as varchar)` on a `TIMESTAMPTZ` **renders using the DuckDB session timezone**. The
stored instant is correct, but its string rendering — and therefore the hash — depends on the
session TZ at insert time. Two imports run under different session timezones produce different
hashes for the same sample. This is latent today because everything has run in one timezone, but it
is a live bug the moment the user travels or a scheduled job runs under a different environment.

Render timestamps into a canonical UTC form inside the hash expression instead. This affects
`_REC_HASH`, `_WORKOUT_HASH`, `_SLEEP_HASH`, and the workout-event parent hash in the same file —
migrate all of them consistently.

### Migration mechanics

The live database is 1.5 GB / ~5M rows and the user cannot afford to lose it.

- Write `scripts/migrate_row_hash.py`. It must be **safe and resumable**: operate on a copy, verify
  row counts and a sample of recomputed hashes before swapping, and leave the original in place
  (remember you may not be able to delete files — plan for that).
- Recomputing the hash can collapse rows that were previously distinct (two records differing only
  in `value_str` formatting). That is the *point*, but it must be **reported**, not silent: log how
  many rows collapsed per table, and refuse to proceed without an explicit flag if the collapse
  exceeds a sane threshold, since a large collapse would mean the new expression is too lossy.
- `records_dedup` and `workout_events_dedup` are views over these tables — confirm they survive.
- The `import_state.json` fingerprint should be invalidated so the next full export re-imports under
  the new hash.

### Acceptance test for Part 0

Import a full export. Then import a delta covering the same time window. **Assert zero rows added.**
This single test is the reason the migration exists; if it does not exist, the feature is not done.

---

## 5. Part 1 — MCP server: receiver and tools

### Receiver

An HTTP listener running in a background thread inside the MCP server process (`server.py` owns its
lifecycle; start it on server startup, shut it down cleanly).

- Bind on the LAN, ephemeral port.
- **Advertise over Bonjour** as `_healthsync._tcp` so the phone needs no IP configuration and
  survives DHCP changes. Add the dependency to `pyproject.toml`. Include the port and a stable
  device identifier in the TXT record.
- `POST /v1/batch` — body is gzipped NDJSON. Headers carry the auth token, a `batch_id`, and a
  device identifier. Behaviour: authenticate, write the body to a spool file under
  `~/Documents/AppleHealthExport/deltas/`, **fsync**, and only then return 200 with the received
  count. A `batch_id` already on disk returns 200 idempotently without rewriting.
- `GET /v1/health` — liveness, for the app to confirm pairing works.
- Reject unauthenticated requests without leaking whether the token was close.

Receive and import are deliberately separate steps. The receiver only durably stores; a crash
mid-import must never lose a batch that was already acknowledged to the phone.

### NDJSON format

One JSON object per line, each with a `kind` discriminator. **The payload shapes must match what
`parser.iter_export` yields**, so that `storage.import_stream` consumes both paths with only a thin
adapter. Do not write a second loader — a second loader is a second set of dedup bugs.

Kinds needed: `record`, `sleep`, `workout`, plus the workout structure. Note that the workout
structure work has already landed: `<WorkoutActivity>` elements (Apple's structured-interval steps)
are ingested into a `workout_events` table, queried through a `workout_events_dedup` view. On iOS
17+, `HKWorkout.workoutActivities` is the same structure. **It must be included in the sync**, or
interval sessions synced via delta will lose the per-repetition data that the analytics now depend on.

### New MCP tools

- `pair_device()` — generate (or rotate) the shared token, persist it with restrictive file
  permissions, and return a pairing payload the phone can consume. Return it as a scannable QR
  rendered in text, plus the raw payload as a fallback for manual entry.
- `import_from_app(wait_seconds: int = 0)` — import all pending spool batches through the existing
  `import_lock`, move them to an `imported/` subfolder, return per-table added counts. With
  `wait_seconds > 0`, poll the spool first, so a conversation can say "open the app now" and block
  briefly for the result. This is the flow the user explicitly asked for.
- `sync_status()` — is the listener bound and on what port, is Bonjour advertising, is a device
  paired, when was the last batch received, how many are pending, and the latest sample timestamp
  per type. This is the tool that answers "did my data actually arrive", so make it answer that
  question without further queries.

Follow the existing tool-annotation conventions (`RO` vs `WRITE`) and write descriptions in the same
terse, specific style as the existing ones — those strings are what a model reads to decide whether
to call the tool.

---

## 6. Part 2 — iOS app (Readiness)

### Sync service

Anchored queries per HealthKit type, with each type's `HKQueryAnchor` persisted (archive it to a
file in application support; `UserDefaults` is the wrong home for this).

**The anchor discipline is the single most important rule in this document.** Advance the anchor
**only after the Mac returns 200**. If you advance on send and the connection drops, HealthKit will
never hand you those samples again — they are gone permanently, with no error and no way to detect
it afterwards. Every failure path must leave the anchor untouched.

Give each batch a `batch_id` so a retry after an ambiguous failure is a no-op rather than a duplicate.

### Trigger

`scenePhase` transition to `.active` runs the anchored queries; if anything is new, discover the Mac
and push. Debounce so rapid foreground/background cycling does not re-run it. No background
scheduling in v1.

### Discovery and transport

`NWBrowser` for `_healthsync._tcp`, with a manual host:port field as a fallback. Requires
`NSLocalNetworkUsageDescription` in Info.plist and the iOS 14+ local-network permission prompt — the
first sync will fail confusingly if that is missed. If you use `URLSession` over plain HTTP you also
need an App Transport Security exception for local networking; choose your transport deliberately and
say which and why.

### Pairing

Scan the QR from `pair_device()`, store the token in the **Keychain**, send it on every request.

### Value and unit encoding

Send the numeric value as a number plus its unit string. Do **not** attempt to reproduce Apple's XML
number formatting — that is precisely what the Part 0 migration removed the need for.

**But the unit string is still inside the hash.** HealthKit makes *you* choose the unit when calling
`doubleValue(for:)`, so the app must request the same unit the export writes, per type, or the hashes
diverge and you get silent duplicates. The mapping is not guessable — derive it from the real data.
The live database yields exactly one unit per type; here it is, verbatim, as of 2026-08-20:

```
physical_effort                     kcal/hr·kg      heart_rate                     count/min
active_energy                       kcal            basal_energy                   kcal
distance_walking_running            km              step_count                     count
headphone_audio_exposure            dBASPL          walking_speed                  km/hr
walking_step_length                 cm              walking_double_support_pct     %
exercise_time                       min             walking_asymmetry_percentage   %
stand_time                          min             respiratory_rate               count/min
flights_climbed                     count           time_in_daylight               min
running_power                       W               running_speed                  km/hr
environmental_audio_exposure        dBASPL          oxygen_saturation              %
running_vertical_oscillation        cm              running_ground_contact_time    ms
stair_descent_speed                 m/s             running_stride_length          m
stair_ascent_speed                  m/s             hrv                            ms
environmental_sound_reduction       dBASPL          resting_heart_rate             count/min
walking_heart_rate_average          count/min       distance_cycling               km
vo2max                              mL/min·kg       walking_steadiness             %
six_minute_walk_test_distance       m               heart_rate_recovery            count/min
sleeping_wrist_temperature          degC            height                         cm
weight                              kg              hk_data_type_sleep_duration_goal  hr

no unit (category types): sleep_analysis, stand_hour, audio_exposure_event,
                          mindful_session, high_heart_rate_event
```

Re-derive this yourself with `SELECT DISTINCT type, unit FROM records` before trusting it, and pin it
in a test so a future HealthKit change surfaces as a test failure rather than as duplicate rows.

Note the units are **display forms with non-ASCII characters** (`kcal/hr·kg`, `mL/min·kg` use U+00B7
MIDDLE DOT). Match them byte for byte.

### One more encoding trap, already observed in this data

`sourceName` in the export contains **non-breaking spaces** (U+00A0), e.g. `Apple Watch —
Maksim`, and the name changed mid-history to `Maksim's Apple Watch`. `source_name` is in the
hash. The app must send `HKSource.name` verbatim — no whitespace normalization, no trimming, no
Unicode folding anywhere along the path.

---

## 7. Definition of done

1. Full export imported, then a delta covering the same window imported → **zero rows added**.
2. A structured interval workout synced by delta produces the same `workout_events_dedup` rows as the
   same workout imported from XML.
3. Mac unreachable → the app reports the failure, the anchor does not advance, and the next
   successful sync delivers everything that was missed.
4. An unpaired device is rejected by the receiver.
5. `sync_status()` alone is enough to diagnose "my data did not arrive".
6. The full-export path still works unchanged.
7. `uv run pytest -q` passes, and any new dependency is in `pyproject.toml` and `uv.lock`.

## 8. Report back

What you built, per repo. The exact NDJSON schema. The new hash expression and the migration's
before/after row counts including any collapse. Which transport you chose and why. Every decision
where you deviated from this spec, with reasoning. And an explicit list of what you could not verify,
separated from what you tested — do not report success on anything you did not actually run.
