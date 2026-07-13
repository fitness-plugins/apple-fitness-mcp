"""Self-check: spawn the MCP server over stdio and prove it responds.

Launches the server exactly the way Claude Desktop will (venv python +
`-m apple_health_mcp.server`), performs the MCP handshake, lists tools, and
calls `list_metrics`. Exits 0 on success, non-zero on any failure — so setup.sh
can *prove* the server works rather than assuming it.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXPECTED_TOOLS = {
    "list_metrics", "get_summary", "get_steps", "get_heart_rate", "get_hrv",
    "get_sleep", "get_weight", "get_vo2max", "get_workouts", "run_sql",
    "reload_data",
}


async def _run() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "apple_health_mcp.server"],
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await asyncio.wait_for(session.initialize(), timeout=30)

            tools = await session.list_tools()
            names = {t.name for t in tools.tools}
            missing = EXPECTED_TOOLS - names
            if missing:
                print(f"FAIL: server is missing tools: {sorted(missing)}")
                return 1

            # Confirm read-only annotations are advertised.
            ro = {t.name for t in tools.tools
                  if t.annotations and t.annotations.readOnlyHint}
            print(f"  tools exposed : {len(names)} ({', '.join(sorted(names))})")
            print(f"  read-only     : {len(ro)}/{len(names)} marked readOnlyHint")

            result = await asyncio.wait_for(
                session.call_tool("list_metrics", {}), timeout=30
            )
            if result.isError:
                print(f"FAIL: list_metrics returned an error: {result.content}")
                return 1

            # Prefer structured content; fall back to the JSON text block (which
            # is what Claude Desktop itself reads for plain-dict tool returns).
            payload = result.structuredContent
            if not payload and result.content:
                text = getattr(result.content[0], "text", None)
                if text:
                    try:
                        payload = json.loads(text)
                    except json.JSONDecodeError:
                        payload = {}
            payload = payload or {}
            metrics = payload.get("metrics", []) if isinstance(payload, dict) else []
            print(f"  list_metrics  : OK — {len(metrics)} metric type(s) currently "
                  "in the database")
            if not metrics:
                print("  (database is empty — expected until your first export "
                      "is imported.)")
            print("PASS: MCP server initialized, listed tools, and responded to "
                  "list_metrics over stdio.")
            return 0


def main() -> int:
    try:
        return asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001 — self-check must report, not crash
        print(f"FAIL: self-check raised {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
