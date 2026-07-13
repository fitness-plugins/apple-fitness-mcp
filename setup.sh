#!/usr/bin/env bash
#
# One-shot setup for the Apple Health MCP server on macOS.
# Idempotent: safe to re-run. Targets standard macOS paths only.
#
# No background automation is installed. Data is imported only when you ask:
# either by re-running this script, running `uv run apple-health-import`, or by
# asking Claude to call the `reload_data` tool.
#
set -euo pipefail

# --- resolve paths -----------------------------------------------------------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

VENV_PYTHON="$PROJECT_ROOT/.venv/bin/python"
EXPORT_DIR="$HOME/Library/Mobile Documents/com~apple~CloudDocs/AppleHealthExport"
CLAUDE_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"
LOG_DIR="$PROJECT_ROOT/logs"

bold() { printf "\033[1m%s\033[0m\n" "$*"; }
ok()   { printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$*"; }
die()  { printf "  \033[31m✗ %s\033[0m\n" "$*" >&2; exit 1; }

export PATH="$HOME/.local/bin:$PATH"

bold "Apple Health MCP — setup"
echo  "Project: $PROJECT_ROOT"
echo

# --- 0. prerequisites --------------------------------------------------------
bold "[0/6] Checking prerequisites"
command -v uv >/dev/null 2>&1 || die "uv is not installed. Install it: curl -LsSf https://astral.sh/uv/install.sh | sh"
ok "uv found: $(uv --version)"

# --- 1. venv + deps ----------------------------------------------------------
bold "[1/6] Creating venv and installing dependencies (uv sync)"
uv sync >/dev/null 2>&1 || die "uv sync failed. Run 'uv sync' manually to see the error."
[ -x "$VENV_PYTHON" ] || die "venv python not found at $VENV_PYTHON"
ok "environment ready ($("$VENV_PYTHON" --version))"

# --- 2. schema + import any existing export ----------------------------------
bold "[2/6] Initializing database schema and importing any present export"
mkdir -p "$LOG_DIR" "$PROJECT_ROOT/data"
uv run python -m apple_health_mcp.import_pipeline || die "schema init / import failed"
ok "DuckDB schema ready at data/health.duckdb"

# --- 3. iCloud export folder -------------------------------------------------
bold "[3/6] Ensuring iCloud export drop-folder exists"
mkdir -p "$EXPORT_DIR" || die "could not create export folder: $EXPORT_DIR"
ok "drop exports here: $EXPORT_DIR"

# --- 4. Claude Desktop config merge ------------------------------------------
bold "[4/6] Registering MCP server in Claude Desktop config"
VENV_PYTHON="$VENV_PYTHON" PROJECT_ROOT="$PROJECT_ROOT" CLAUDE_CONFIG="$CLAUDE_CONFIG" \
    uv run python scripts/merge_config.py || die "config merge failed"
"$VENV_PYTHON" -c "import json,sys; d=json.load(open('$CLAUDE_CONFIG')); sys.exit(0 if 'apple-health' in d.get('mcpServers',{}) else 1)" \
    || die "apple-health entry missing after merge"
ok "Claude Desktop config updated (backup written alongside it)"

# --- 5. restart Claude Desktop -----------------------------------------------
bold "[5/6] Restarting Claude Desktop to pick up the server"
if pgrep -x "Claude" >/dev/null 2>&1; then
    osascript -e 'quit app "Claude"' >/dev/null 2>&1 || warn "could not send quit to Claude"
    for _ in $(seq 1 20); do pgrep -x "Claude" >/dev/null 2>&1 || break; sleep 0.5; done
    if pgrep -x "Claude" >/dev/null 2>&1; then
        warn "Claude did not quit within 10s; please quit and reopen it manually"
    else
        ok "Claude quit cleanly"
    fi
else
    warn "Claude was not running"
fi
open -a "Claude" >/dev/null 2>&1 && ok "Claude relaunched" || warn "could not relaunch Claude — open it manually"

# --- 6. self-check -----------------------------------------------------------
bold "[6/6] Self-check: spawning the MCP server over stdio"
if uv run python scripts/selfcheck.py; then
    ok "self-check PASSED"
else
    die "self-check FAILED — the server did not respond correctly over stdio"
fi

# --- summary -----------------------------------------------------------------
bold "Done"
HAS_EXPORT="no"
if ls "$EXPORT_DIR"/*.zip >/dev/null 2>&1; then HAS_EXPORT="yes"; fi
echo
bold "Set up on this Mac:"
ok "Dependencies installed in .venv (uv)"
ok "DuckDB schema ready"
ok "MCP server registered in Claude Desktop and verified responding"
ok "Claude Desktop restarted"
echo
bold "How data flows (nothing runs in the background):"
echo "  1. iPhone Health app → Export All Health Data → Save to Files →"
echo "     iCloud Drive / AppleHealthExport"
echo "  2. Then import it, whichever you prefer:"
echo "       • ask Claude:  \"reload my health data\"  (runs the reload_data tool)"
echo "       • or run:      uv run apple-health-import"
echo "       • or re-run:   ./setup.sh"
echo
if [ "$HAS_EXPORT" = "yes" ]; then
    ok "An export is already present and has been imported."
else
    warn "No export in the folder yet — do step 1 above, then step 2."
fi
echo
bold "Try asking Claude:"
echo "  • \"What Apple Health data do you have access to?\""
echo "  • \"Summarize my steps by week for the last month.\""
echo "  • \"How's my resting heart rate trending this year?\""
