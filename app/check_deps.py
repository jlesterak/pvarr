#!/usr/bin/env python3
"""
Dependency Checker Module for Stream Failover Studio
Verifies availability of FFmpeg, FFprobe, hls-proxy, and detect-headers CLI tools.
"""

import os
import shutil
import sys
from pathlib import Path


def find_executable(name: str, alt_names: list = None) -> str:
    """Check PATH and common local workspace locations for an executable script or binary."""
    names = [name] + (alt_names or [])
    
    # 1. Search PATH
    for n in names:
        path = shutil.which(n)
        if path:
            return path
            
    # 2. Search parent directories & workspace fallback
    base_dir = Path(__file__).resolve().parent.parent
    search_dirs = [
        base_dir,
        base_dir.parent,
        Path(os.getcwd())
    ]
    
    for d in search_dirs:
        for n in names:
            candidate = d / n
            if candidate.exists() and os.access(candidate, os.X_OK):
                return str(candidate)
            # Also check if it's executable via python interpreter if it ends with .py
            if candidate.exists() and n.endswith(".py"):
                return str(candidate)
                
    return ""


def check_dependencies(verbose: bool = True) -> dict:
    """
    Check all required dependencies and return a dictionary of status.
    """
    deps = {
        "ffmpeg": find_executable("ffmpeg"),
        "ffprobe": find_executable("ffprobe"),
        "hls-proxy": find_executable("hls-proxy.py", ["hls-proxy"]),
        "detect-headers": find_executable("detect-headers-py.py", ["detect-headers.sh", "detect-headers"])
    }
    
    all_ok = True
    if verbose:
        print("=== Stream Failover Studio - Dependency Check ===")
        for tool, path in deps.items():
            if path:
                print(f"  [OK] {tool:15s} -> {path}")
            else:
                print(f"  [MISSING] {tool:15s}")
                all_ok = False
                
    return {"status": all_ok, "dependencies": deps}


if __name__ == "__main__":
    res = check_dependencies(verbose=True)
    if not res["status"]:
        sys.exit(1)
