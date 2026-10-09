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

# 5. Ensure runtime directories exist.
#
# Non-fatal: under `set -e` a failure here killed the container at boot for any
# uid that does not own /app. In the container these are bind mounts that the
# entrypoint has already prepared, and `recordings` here is a decoy anyway --
# PVARR_RECORDINGS_DIR points at /recordings.
mkdir -p recordings logs 2>/dev/null || true

# 6. Start Uvicorn Web Dashboard Server
echo ""
echo "[+] Starting Web Dashboard Server on http://${HOST}:${PORT}..."
echo "[+] Press Ctrl+C to stop all active streams and exit gracefully."
echo ""

# How long uvicorn may spend waiting for open connections before it closes them
# and moves on to the application shutdown hook -- the hook that stops the
# recorders and marks their sessions for resume.
#
# Without a bound this is unlimited, and the dashboard holds a log-tailing
# EventSource open for as long as a browser tab is on it. So a `docker stop`
# with the UI open anywhere waited on that tab, Docker's stop_grace_period (30s)
# expired first, and the container was SIGKILLed before a single recorder had
# been told to stop: no resume marker, and FFmpeg killed mid-write. Measured at
# ~80s with one tab open.
#
# The whole shutdown must fit inside stop_grace_period:
#   PVARR_GRACEFUL_TIMEOUT (drain) + PVARR_SHUTDOWN_TIMEOUT (reap + remux) < 30s
# Defaults are 5 + 20 = 25s. Raise one and lower the other, or raise
# stop_grace_period in docker-compose.yml to match.
GRACEFUL_TIMEOUT="${PVARR_GRACEFUL_TIMEOUT:-5}"
if ! [[ "$GRACEFUL_TIMEOUT" =~ ^[0-9]+$ ]]; then
    echo "[!] Ignoring invalid PVARR_GRACEFUL_TIMEOUT=${GRACEFUL_TIMEOUT}; using 5." >&2
    GRACEFUL_TIMEOUT=5
fi

# --reload-dir without --reload is a no-op; omitted rather than shipping a
# reloader in production.
exec python3 -m uvicorn app.server:app --host "$HOST" --port "$PORT" \
    --timeout-graceful-shutdown "$GRACEFUL_TIMEOUT"
