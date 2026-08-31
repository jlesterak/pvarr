#!/usr/bin/env python3
"""
Sports File-Naming & Storage Module - PVArr
Handles standardized sports recording filenames, ffprobe resolution probe,
and output directory management.
"""

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

from app.check_deps import find_executable

# Every container a recording can exist in. Capture writes .ts; the
# post-processor remuxes to .mp4 (or .mkv) and deletes the .ts, so a library
# that only knew about .ts could not see a single finished recording.
RECORDING_EXTENSIONS = (".ts", ".mp4", ".mkv")

# Served as the Content-Type when a recording is downloaded.
MEDIA_TYPES = {
    ".ts": "video/mp2t",
    ".mp4": "video/mp4",
    ".mkv": "video/x-matroska",
}


def media_type_for(filename: str) -> str:
    """Content-Type for a recording, by extension."""
    return MEDIA_TYPES.get(Path(filename).suffix.lower(), "application/octet-stream")


def sanitize_token(text: str, fallback: str = "Unknown") -> str:
    """Clean string token for safe filename usage."""
    if not text:
        return fallback
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", text.strip()).strip("_")
    return cleaned if cleaned else fallback


def probe_video_resolution(filepath: str) -> str:
    """Use ffprobe to inspect video stream height and return formatted resolution (e.g., 1080p, 720p)."""
    ffprobe_cmd = find_executable("ffprobe")
    if not ffprobe_cmd or not os.path.exists(filepath) or os.path.getsize(filepath) == 0:
        return "1080p"  # Default assumption

    cmd = [
        ffprobe_cmd,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0",
        filepath
    ]

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if res.returncode == 0 and res.stdout.strip():
            # Output like: 1920x1080
            dim = res.stdout.strip().split("x")
            if len(dim) >= 2 and dim[1].isdigit():
                height = int(dim[1])
                if height >= 2160:
                    return "4K"
                elif height >= 1440:
                    return "1440p"
                elif height >= 1080:
                    return "1080p"
                elif height >= 720:
                    return "720p"
                elif height >= 480:
                    return "480p"
                else:
                    return f"{height}p"
    except Exception:
        pass

    return "1080p"


def generate_sports_filename(
    sport: str,
    team_a: str,
    team_b: str,
    resolution: str = "1080p",
    date_str: Optional[str] = None,
    ext: str = "ts"
) -> str:
    """
    Generate standardized filename format: YYYY-MM-DD_[Sport]_[TeamA_vs_TeamB]_[Resolution].ts
    """
    date = date_str or datetime.now().strftime("%Y-%m-%d")
    s_sport = sanitize_token(sport, "Sports")
    s_team_a = sanitize_token(team_a, "TeamA")
    s_team_b = sanitize_token(team_b, "TeamB")
    s_res = sanitize_token(resolution, "1080p")
    ext = ext.lstrip(".")

    teams_str = f"{s_team_a}_vs_{s_team_b}"
    filename = f"{date}_{s_sport}_{teams_str}_{s_res}.{ext}"
    return filename


class StorageManager:
    def __init__(self, record_dir: str = "recordings"):
        self.record_dir = Path(record_dir).resolve()
        self.record_dir.mkdir(parents=True, exist_ok=True)

    def get_output_path(
        self,
        sport: str,
        team_a: str,
        team_b: str,
        resolution: str = "1080p",
        custom_dir: Optional[str] = None
    ) -> Path:
        target_dir = Path(custom_dir).resolve() if custom_dir else self.record_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        filename = generate_sports_filename(sport, team_a, team_b, resolution)
        path = target_dir / filename

        # Avoid collision
        counter = 1
        stem = path.stem
        ext = path.suffix
        while path.exists():
            path = target_dir / f"{stem}_{counter}{ext}"
            counter += 1

        return path

    def list_recordings(self, target_dir: Optional[str] = None) -> List[Dict[str, Any]]:
        dir_path = Path(target_dir).resolve() if target_dir else self.record_dir
        if not dir_path.exists():
            return []

        # Was glob("*.ts"), which hid every finished recording: post-processing
        # remuxes to .mp4 and deletes the .ts, so the library went empty the
        # moment a capture succeeded, and a delete clicked against a stale .ts
        # entry 404'd on a file that no longer existed.
        files = [
            f for f in dir_path.iterdir()
            if f.is_file() and f.suffix.lower() in RECORDING_EXTENSIONS
        ]
        results = []
        for file in sorted(files, key=os.path.getmtime, reverse=True):
            stat = file.stat()
            results.append({
                "filename": file.name,
                "filepath": str(file),
                "size_mb": round(stat.st_size / (1024 * 1024), 2),
                "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S"),
                "modified_timestamp": stat.st_mtime
            })
        return results

    def rename_recording(self, old_filename: str, new_filename: str, target_dir: Optional[str] = None) -> bool:
        dir_path = Path(target_dir).resolve() if target_dir else self.record_dir
        old_path = dir_path / old_filename
        # Keep the container the file is actually in. This used to force ".ts"
        # onto every rename, so renaming a finished recording produced
        # "highlights.mp4.ts" -- a name that lies about the contents.
        if Path(new_filename).suffix.lower() not in RECORDING_EXTENSIONS:
            new_filename += old_path.suffix
        new_path = dir_path / new_filename

        if old_path.exists() and not new_path.exists():
            old_path.rename(new_path)
            return True
        return False

    def delete_recording(self, filename: str, target_dir: Optional[str] = None) -> bool:
        dir_path = Path(target_dir).resolve() if target_dir else self.record_dir
        file_path = dir_path / filename
        if file_path.exists():
            file_path.unlink()
            return True
        return False
