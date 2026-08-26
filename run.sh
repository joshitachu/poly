#!/usr/bin/env bash
# run.sh — Wrapper to run weather or fastloop trader with logging
#
# Usage:
#   ./run.sh weather    # Run weather trader (paper mode, quiet)
#   ./run.sh fastloop   # Run fastloop BTC trader (paper mode, quiet)
#   ./run.sh status     # Check account balance & positions
#
# Cron examples:
#   */30 * * * * /path/to/poly/run.sh weather  >> /path/to/poly/logs/weather.log 2>&1
#   */5  * * * * /path/to/poly/run.sh fastloop >> /path/to/poly/logs/fastloop.log 2>&1

set -euo pipefail

# ── Configuration ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# Interpreter — override with PYTHON=/path/to/python (e.g. a venv)
PYTHON="${PYTHON:-python3}"

# Load API key from .env file
if [ -f "$SCRIPT_DIR/.env" ]; then
  set -a
  source "$SCRIPT_DIR/.env"
  set +a
fi

# Ensure logs directory exists
mkdir -p "$SCRIPT_DIR/logs"

# ── Timestamp helper ───────────────────────────────────────────────
ts() {
  echo "--- $(date -u '+%Y-%m-%d %H:%M:%S UTC') ---"
}

# ── Main ───────────────────────────────────────────────────────────
MODE="${1:-help}"

# Only the modes that hit the Simmer API need a key; `summary` reads local logs
case "$MODE" in
  weather|fastloop|status)
    if [ -z "${SIMMER_API_KEY:-}" ]; then
      echo "Error: SIMMER_API_KEY not set. Create a .env file — see .env.example"
      exit 1
    fi
    ;;
esac

case "$MODE" in
  weather)
    ts
    "$PYTHON" "$SCRIPT_DIR/weather-trader/weather_trader.py" --quiet
    echo ""
    ;;
  fastloop)
    ts
    "$PYTHON" "$SCRIPT_DIR/fastloop_trader.py" --quiet
    echo ""
    ;;
  status)
    ts
    "$PYTHON" "$SCRIPT_DIR/weather-trader/scripts/status.py" --positions
    ;;
  summary)
    shift
    "$PYTHON" "$SCRIPT_DIR/scripts/summarize_logs.py" "$@"
    ;;
  help|*)
    echo "Usage: $0 {weather|fastloop|status|summary}"
    echo ""
    echo "  weather   Run weather trader (paper mode, quiet)"
    echo "  fastloop  Run BTC fastloop trader (paper mode, quiet)"
    echo "  status    Show account balance & positions"
    echo "  summary   Summarize past runs from logs/ (accepts --since, --json)"
    exit 1
    ;;
esac
