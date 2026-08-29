#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# Stream Failover Studio - Application Runner
# Launches FastAPI Web Server Dashboard & Stream Failover Manager
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8999}"
VENV_DIR="venv"

echo "================================================================="
echo "       Stream Failover Studio — Application Launcher"
echo "================================================================="

# 1. Virtual Environment Check & Activation
if [[ -f "${VENV_DIR}/bin/activate" ]]; then
    # Activate existing venv
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
elif [[ -f ".venv/bin/activate" ]]; then
    # shellcheck disable=SC1091
    source ".venv/bin/activate"
else
    echo "[+] Creating Python virtual environment in ${VENV_DIR}..."
    python3 -m venv "${VENV_DIR}"
    # shellcheck disable=SC1091
    source "${VENV_DIR}/bin/activate"
fi

# 2. Dependency Verification & Installation
if ! python3 -c "import uvicorn, fastapi" &>/dev/null; then
    echo "[+] Missing required dependencies. Installing from requirements.txt..."
    python3 -m pip install --upgrade pip
    if ! python3 -m pip install -r requirements.txt; then
        echo "[!] ERROR: Failed to install Python dependencies from requirements.txt!" >&2
        echo "[!] Please check your network connection and pip installation." >&2
        exit 1
    fi
fi

# 3. Run System Dependency Check
python3 app/check_deps.py

# 4. Ensure runtime directories exist
mkdir -p recordings logs app/templates

# 5. Start Uvicorn Web Dashboard Server
echo ""
echo "[+] Starting Web Dashboard Server on http://${HOST}:${PORT}..."
echo "[+] Press Ctrl+C to stop all active streams and exit gracefully."
echo ""

exec python3 -m uvicorn app.server:app --host "$HOST" --port "$PORT" --reload-dir app
