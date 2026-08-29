#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Stream Failover Studio - Application Runner
# Launches FastAPI Web Server Dashboard & Stream Failover Manager
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8000}"

echo "================================================================="
echo "       Stream Failover Studio — Application Launcher"
echo "================================================================="

# 1. Run dependency check
python3 app/check_deps.py

# 2. Ensure directories exist
mkdir -p recordings logs app/templates

# 3. Start Uvicorn Web Dashboard Server
echo ""
echo "[+] Starting Web Dashboard Server on http://${HOST}:${PORT}..."
echo "[+] Press Ctrl+C to stop all active streams and exit gracefully."
echo ""

exec uvicorn app.server:app --host "$HOST" --port "$PORT" --reload-dir app
