"""Merge the apple-health MCP server into claude_desktop_config.json.

Preserves every existing key (preferences, other mcpServers, ...). Writes a
timestamped backup first, then does an atomic replace. Idempotent: re-running
just refreshes the apple-health entry.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

CONFIG_PATH = Path(
    os.environ.get(
        "CLAUDE_CONFIG",
        Path.home() / "Library" / "Application Support" / "Claude"
        / "claude_desktop_config.json",
    )
)
SERVER_NAME = "apple-health"


def main() -> int:
    venv_python = os.environ["VENV_PYTHON"]  # set by setup.sh
    project_root = os.environ["PROJECT_ROOT"]

    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)

    if CONFIG_PATH.exists():
        raw = CONFIG_PATH.read_text() or "{}"
        try:
            config = json.loads(raw)
        except json.JSONDecodeError as exc:
            print(f"ERROR: existing config is not valid JSON ({exc}). "
                  "Refusing to overwrite; please fix it by hand.", file=sys.stderr)
            return 1
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = CONFIG_PATH.with_suffix(f".json.backup-{stamp}")
        shutil.copy2(CONFIG_PATH, backup)
        print(f"  backed up existing config -> {backup.name}")
    else:
        config = {}
        print("  no existing config; creating a new one")

    if not isinstance(config, dict):
        print("ERROR: config root is not a JSON object.", file=sys.stderr)
        return 1

    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print("ERROR: existing 'mcpServers' is not an object.", file=sys.stderr)
        return 1

    servers[SERVER_NAME] = {
        "command": venv_python,
        "args": ["-m", "apple_health_mcp.server"],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "PYTHONPATH": str(Path(project_root) / "src"),
        },
    }

    # Atomic write.
    fd, tmp = tempfile.mkstemp(dir=str(CONFIG_PATH.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

    print(f"  merged '{SERVER_NAME}' MCP server into {CONFIG_PATH.name}")
    print(f"    command: {venv_python} -m apple_health_mcp.server")
    return 0


if __name__ == "__main__":
    sys.exit(main())
