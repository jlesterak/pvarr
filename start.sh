#!/usr/bin/env bash
set -euo pipefail

# =============================================================================
# PVArr - Application Runner
# Launches FastAPI Web Server Dashboard & Stream Failover Manager
# =============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8999}"
VENV_DIR="venv"

echo "================================================================="
echo "              PVArr — Application Launcher"
echo "================================================================="

# 1. Quick PATH check for required system binaries (fast fail before Python starts)
if ! command -v ffmpeg &>/dev/null; then
    echo "[!] ERROR: 'ffmpeg' not found in PATH. Please install ffmpeg." >&2
    exit 1
fi
if ! command -v ffprobe &>/dev/null; then
    echo "[!] ERROR: 'ffprobe' not found in PATH. Please install ffmpeg." >&2
    exit 1
fi

# Informational check for optional proxy tools
if command -v hls-proxy &>/dev/null || command -v hls-proxy.py &>/dev/null; then
    echo "[+] hls-proxy      -> FOUND (proxy fallback enabled)"
else
    echo "[i] hls-proxy      -> not found (direct FFmpeg mode only — proxy fallback disabled)"
fi
if command -v detect-headers &>/dev/null || command -v detect-headers-py.py &>/dev/null; then
    echo "[+] detect-headers -> FOUND (header auto-detection enabled)"
else
    echo "[i] detect-headers -> not found (manual header injection only)"
fi

# 2. Virtual Environment Check & Activation
# Skip venv creation inside Docker (PVARR_NO_VENV=1 is set or venv doesn't make sense)
if [[ "${PVARR_NO_VENV:-0}" == "1" ]]; then
    echo "[+] Running in container mode — skipping venv."
elif [[ -f "${VENV_DIR}/bin/activate" ]]; then
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

# 3. Dependency Verification & Installation
if ! python3 -c "import uvicorn, fastapi" &>/dev/null; then
    echo "[+] Missing required Python dependencies. Installing from requirements.txt..."
    python3 -m pip install --upgrade pip
    if ! python3 -m pip install -r requirements.txt; then
        echo "[!] ERROR: Failed to install Python dependencies from requirements.txt!" >&2
        echo "[!] Please check your network connection and pip installation." >&2
        exit 1
    fi
fi

# 4. Run full Python dependency check (non-fatal for optional tools)
python3 app/check_deps.py || true

# 5. Ensure runtime directories exist
mkdir -p recordings logs

# 6. Start Uvicorn Web Dashboard Server
echo ""
echo "[+] Starting Web Dashboard Server on http://${HOST}:${PORT}..."
echo "[+] Press Ctrl+C to stop all active streams and exit gracefully."
echo ""

# --reload-dir without --reload is a no-op; omitted rather than shipping a
# reloader in production.
exec python3 -m uvicorn app.server:app --host "$HOST" --port "$PORT"
