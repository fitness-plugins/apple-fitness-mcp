# MCP build — orchestrator prompt (`save_weekly_plan`)

Paste the block below into **one** Claude Code chat at the **apple-fitness-mcp**
repo root. The detailed spec lives in `MCP_SAVE_PLAN_PROMPT.md`. This stage is
independent of the iOS repo — it is coupled to the app only by the WeeklyPlan JSON
schema and the iCloud container id `iCloud.com.readiness.plan`.

---

```
You are the ORCHESTRATOR for adding the `save_weekly_plan` write tool to the
apple-fitness-mcp server. The detailed spec is in `MCP_SAVE_PLAN_PROMPT.md`. Drive
it to completion with a subagent — do NOT code in this main context.

Rules:
- Do NOT read the server code or the spec into THIS context. Read
  `MCP_SAVE_PLAN_PROMPT.md` ONCE to know the stage — nothing else.
- Launch ONE FRESH subagent (Task tool, general-purpose) with:
    "Open MCP_SAVE_PLAN_PROMPT.md and execute the PROMPT exactly as written. Obey
     its 'Read ONLY' scope — do not scan the rest of the repo, do not touch the
     parser/scoring/storage code or the iOS repo. Do all the work, including the
     pytest tests. When done, reply with a concise report: files created/changed,
     `uv run pytest -q` result, and any blocker. Do not summarize code line by
     line."
- After the subagent returns, verify success. If it reports it can't run
  `uv run pytest -q` in its sandbox, record that but accept the stage as long as
  the code + tests were generated. Only STOP and ask the user if the tool code
  can't be produced at all.
- Keep a one-line checklist in THIS chat (done / blocked + note).

When done, print a final summary:
- the checklist and any blocker,
- a reminder that the tool writes to `iCloud.com.readiness.plan`'s Documents
  container (env override `HEALTH_PLAN_PATH`), and that this id must match the iOS
  app's iCloud capability — the iOS half is built in the OTHER repo
  (apple-fitness-ios), NOT here,
- how to sanity-check it: run `uv run python scripts/selfcheck.py` (or the
  project's self-check) and confirm the new tool is listed, then note that Claude
  Desktop must be restarted to pick up the new tool.

Start now.
```

---

**Notes**
- Keep the server fully local: the tool only writes a local file (iCloud syncs it
  natively), no network calls.
- If you change any WeeklyPlan field name here, change it in
  `apple-fitness-ios/IOS_TRAINING_PLAN_PROMPTS.md` too — the JSON schema is the
  only contract between the two repos.
