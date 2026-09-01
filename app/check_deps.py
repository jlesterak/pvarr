#!/usr/bin/env python3
"""
Dependency Checker Module for PVArr
Verifies availability of FFmpeg, FFprobe, hls-proxy, and detect-headers CLI tools.
hls-proxy and detect-headers are OPTIONAL — their absence is a warning, not a fatal error.
"""

import os
import shutil
import sys
from pathlib import Path


def find_executable(name: str, alt_names: list = None) -> str:
    """Check PATH and common local workspace locations for an executable script or binary."""
    names = [name] + (alt_names or [])

    # 1. Search PATH first (covers symlinks like /usr/local/bin/hls-proxy)
    for n in names:
        path = shutil.which(n)
        if path:
            return path

    # 2. Search parent directories & workspace fallback (for local dev)
    base_dir = Path(__file__).resolve().parent.parent
    search_dirs = [
        base_dir,
        base_dir.parent,
        Path(os.getcwd()),
        Path("/usr/local/bin"),
        Path("/opt/hls-restream-proxy"),
    ]

    for d in search_dirs:
        for n in names:
            candidate = d / n
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
            # Python scripts that aren't +x are still usable via the interpreter
            if candidate.exists() and n.endswith(".py"):
                return str(candidate)

    return ""


# Why each optional tool matters, so a WARN line says what is actually lost.
OPTIONAL_NOTES = {
    "hls-proxy": "no bridge for streams needing continuous token refresh",
    # This used to claim the probe "covers everything but JS-built URLs", which
    # read as though detect-headers covered those. It does not: the shipped
    # version is the shell one, which is curl following iframes.
    "detect-headers": "one fewer route for pages that hide their m3u8 behind redirects",
    "yt-dlp": "no way to resolve a page whose player fetches its manifest over XHR",
}


def check_dependencies(verbose: bool = True) -> dict:
    """
    Check all required dependencies and return a status dictionary.
    FFmpeg and FFprobe are REQUIRED (exit 1 if missing).
    hls-proxy and detect-headers are OPTIONAL (warning only) -- header
    detection is built in (app/probe.py) and needs neither.
    """
    required = {
        "ffmpeg":  find_executable("ffmpeg"),
        "ffprobe": find_executable("ffprobe"),
    }
    optional = {
        "hls-proxy":      find_executable("hls-proxy.py",         ["hls-proxy"]),
        "detect-headers": find_executable("detect-headers-py.py", ["detect-headers.sh", "detect-headers"]),
        "yt-dlp":         find_executable("yt-dlp"),
    }

    all_required_ok = all(v for v in required.values())

    if verbose:
        print("=== PVArr - Dependency Check ===")
        for tool, path in required.items():
            if path:
                print(f"  [OK]      {tool:20s} -> {path}")
            else:
                print(f"  [MISSING] {tool:20s}  *** REQUIRED — install ffmpeg ***")

        for tool, path in optional.items():
            if path:
                print(f"  [OK]      {tool:20s} -> {path}")
            else:
                print(f"  [WARN]    {tool:20s}  (optional — {OPTIONAL_NOTES[tool]})")

        if not all_required_ok:
            print("\n[!] Required dependencies are missing. Please install ffmpeg.")
        print()

    return {
        "status": all_required_ok,
        "dependencies": {**required, **optional}
    }


if __name__ == "__main__":
    res = check_dependencies(verbose=True)
    if not res["status"]:
        sys.exit(1)
    # Optional tools missing → exit 0 (non-fatal)
    sys.exit(0)
