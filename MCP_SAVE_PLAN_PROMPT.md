# MCP prompt — `save_weekly_plan` write tool

Adds the missing half of the training-plan bridge: a tool on **this** server that
writes a `WeeklyPlan` JSON where the iOS app can pick it up. The iOS side
(`PlanProvider` / matcher / UI) is specified in the app repo at
`apple-fitness-ios/IOS_TRAINING_PLAN_PROMPTS.md`; **that file's WeeklyPlan JSON
schema is the contract — this tool must emit exactly that shape.**

Same conventions as the app's build prompts: run in a **fresh Claude Code
session**, obey the "Read ONLY" scope, generate code, run tests if the env allows
(`uv run pytest -q`) else stop and flag it. Never touch the iOS repo from here.

---

## Flow this closes

```
user (Claude Desktop) --asks for a plan--> Claude reads get_recovery /
get_training_load / get_workouts / get_hr_zones --composes WeeklyPlan JSON-->
save_weekly_plan(plan) --writes plan.json--> ~/Documents/AppleFitnessPlans/ -->
user AirDrops / shares the file --> iOS app's LocalImportPlanProvider reads it
```

The tool returns the written path AND the JSON in its result, so the user can
AirDrop / share the file (or copy the JSON) into the app.

Why not automatic iCloud sync: the iCloud capability needs a PAID Apple Developer
Program membership — a free/personal team can't enable it (Xcode errors:
"Personal development teams … do not support the iCloud capability"). So the
default transport is a plain file + manual share. iCloud stays an optional upgrade
for a paid account (just repoint `HEALTH_PLAN_PATH`); no app or tool code changes.

---

## PROMPT — `save_weekly_plan`

```
Fresh session. Read ONLY: src/apple_health_mcp/server.py (the FastMCP setup, the
RO/WRITE ToolAnnotations near the top, and the reload_data tool at the bottom as
the write-tool precedent), src/apple_health_mcp/config.py, and the "WeeklyPlan
JSON schema" section of ../apple-fitness-ios/IOS_TRAINING_PLAN_PROMPTS.md (the
contract). Do not scan the rest of the repo or the iOS Swift code.

Add ONE new write tool, `save_weekly_plan`, that persists a WeeklyPlan JSON to a
location the iOS app reads. Reads nothing from the DB; it only validates and
writes. Deliver:

1) config.py — a single source of truth for the output path:
   - `PLAN_OUTPUT_PATH`, overridable via env `HEALTH_PLAN_PATH` (used by tests),
     defaulting to a plain local folder: `~/Documents/AppleFitnessPlans/plan.json`.
     Create it in ensure_dirs(). This is the transport the app actually uses on a
     free/personal Apple account: the tool writes the file here, the user AirDrops
     / shares it into the app (the tool also returns the JSON in its result for
     exactly this). Keep it a Path.
     NOTE: iCloud sync is intentionally NOT the default — the iCloud capability
     requires a PAID Apple Developer Program membership; personal teams can't use
     it, so the app can't declare an iCloud container. If the user later enrolls,
     they can point `HEALTH_PLAN_PATH` at the app's iCloud container to get
     automatic sync — but do not hardcode that path.

2) A small, dependency-light validator (plain Python checks; use pydantic only if
   it's already a dependency in pyproject.toml — do not add new deps) enforcing the
   contract from the schema doc:
   - required: `schema_version` (int), `week_of` (ISO date, the Monday of the
     week), `planned` (non-empty list). Each planned item: `id` (non-empty str,
     unique within the plan) and `constraints` (an object — an OPEN map; do NOT
     validate individual constraint names or params, the app owns that).
   - optional: `title`, `notes`, `day` (one of monday..sunday or null),
     `generated_by`.
   - `plan_id` (UUID) and `generated_at` (ISO-8601 UTC): if absent, the tool
     FILLS them — `plan_id = uuid4()`, `generated_at = now UTC`. If `plan_id` is
     present, keep it (lets the caller re-save a specific version).
   - Raise a clear ValueError listing every problem on invalid input (the tool
     turns that into a {"status": "invalid", "errors": [...]} result, not a crash).

3) The tool itself:
   @mcp.tool(annotations=WRITE, description="…")  # WRITE: writes a file, idempotent, non-destructive
   def save_weekly_plan(plan: dict, path: Optional[str] = None) -> dict
   - Accept `plan` as a dict (MCP passes JSON objects as dicts). Also accept a
     JSON string defensively (json.loads if given a str).
   - Validate + fill plan_id/generated_at (step 2).
   - Write pretty-printed UTF-8 JSON ATOMICALLY (write to a temp file in the same
     dir, then os.replace) to `path` if given, else `config.PLAN_OUTPUT_PATH`.
     Create parent dirs. If the destination dir can't be created/written (e.g. a
     custom `HEALTH_PLAN_PATH` on an offline iCloud volume), return
     {"status": "no_destination", "hint": "…", "plan_json": <the filled plan>} so
     the user can still AirDrop the JSON.
   - Idempotent: re-saving a plan whose `plan_id` already equals the one on disk
     overwrites identically (no error). Since the app dedupes by plan_id, a caller
     wanting a fresh re-match must omit plan_id (new uuid) — document this in the
     tool description.
   - Return {"status": "saved", "plan_id": …, "week_of": …, "planned_count": …,
     "path": <written path>, "plan_json": <the filled plan>}. Include plan_json in
     the result so Claude can show it / the user can AirDrop it without opening the
     file.
   - Write a concise tool description telling Claude WHEN to call it: after
     composing a WeeklyPlan from the user's recovery/load, to push it to the phone;
     mention that omitting plan_id creates a new plan version that the app
     re-matches from scratch.

4) (Optional, if quick) a matching RO tool `get_weekly_plan()` that reads back and
   returns the currently-saved plan.json (or {"status": "none"}), so the user can
   ask "what's my current plan?" in Desktop.

5) Update the CLAUDE.md line that says "reload_data is the one write tool" to note
   save_weekly_plan as the second (also non-destructive).

Add pytest tests (tests/test_save_plan.py) driving HEALTH_PLAN_PATH at a tmp file:
plan_id/generated_at auto-fill; a provided plan_id is preserved; atomic write
produces valid re-readable JSON; invalid input (missing week_of / empty planned /
planned item without id) returns status "invalid" with errors and writes nothing;
re-saving the same plan_id is idempotent. Run `uv run pytest -q`; if the env can't
run it, generate and stop, flagging it. Do not modify the parser/scoring/storage
code or the iOS repo.
```

---

**Notes**
- The app and this tool are coupled ONLY by the JSON schema in
  `apple-fitness-ios/IOS_TRAINING_PLAN_PROMPTS.md`. If you change a field name
  here, change it there too.
- Keep the server fully local: this tool writes a local file (iCloud syncs it
  natively). No network calls, matching the project's no-cloud stance.
