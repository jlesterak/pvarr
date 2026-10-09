#!/usr/bin/env python3
"""
PVArr Post-Processing Engine
Remuxes completed MPEG-TS (.ts) streams into fast-start MP4/MKV containers
and verifies remux integrity before optional source cleanup.
"""

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional, Dict, Any

from app.check_deps import find_executable
from app.naming import probe_video_resolution, reserve_output_path, retag_resolution

logger = logging.getLogger("PVArrPostProcessor")


def remux_recording(
    ts_filepath: str,
    target_format: str = "mp4",
    delete_source: bool = True
) -> Dict[str, Any]:
    """
    Remux a .ts file into fast-start mp4 or mkv container.
    """
    source_path = Path(ts_filepath).resolve()
    if not source_path.exists() or source_path.stat().st_size == 0:
        logger.error(f"Post-processing failed: Source file {ts_filepath} does not exist or is empty.")
        return {"status": "failed", "error": "Source file not found"}

    target_format = target_format.lstrip(".").lower()
    if target_format not in ["mp4", "mkv"]:
        target_format = "mp4"

    dest_path = source_path.with_suffix(f".{target_format}")
    # Name the finished file after what was recorded, not after the form's
    # Resolution Tag. Only when the probe actually measured something: a
    # failed probe leaves the name alone rather than guessing again.
    reserved = False
    measured = probe_video_resolution(str(source_path))
    if measured:
        retagged = retag_resolution(dest_path, measured)
        if retagged != dest_path:
            try:
                # The new name was never reserved -- the .ts holds the old
                # one -- so claim it the same way a new recording does, or a
                # finished recording already under that name would be
                # overwritten by `ffmpeg -y`.
                dest_path = reserve_output_path(retagged)
                reserved = True
                logger.info(
                    f"Resolution tag corrected: recorded {measured}, "
                    f"finishing as {dest_path.name}"
                )
            except OSError as e:
                logger.warning(f"Could not reserve {retagged.name}, keeping the original tag: {e}")
    ffmpeg_cmd = find_executable("ffmpeg") or "ffmpeg"

    cmd = [
        ffmpeg_cmd,
        "-y",
        "-i", str(source_path),
        "-c", "copy"
    ]

    if target_format == "mp4":
        cmd.extend(["-movflags", "+faststart", "-bsf:a", "aac_adtstoasc"])

    cmd.append(str(dest_path))

    logger.info(f"Starting post-processing remux: {source_path.name} -> {dest_path.name}...")
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if res.returncode == 0 and dest_path.exists() and dest_path.stat().st_size > 0:
            logger.info(f"Remux successful! Final size: {dest_path.stat().st_size / (1024*1024):.2f} MB")
            
            if delete_source:
                try:
                    source_path.unlink()
                    logger.info(f"Source file {source_path.name} removed post-remux.")
                except Exception as e:
                    logger.warning(f"Could not delete source file {source_path.name}: {e}")

            return {
                "status": "success",
                "output_filepath": str(dest_path),
                "output_filename": dest_path.name,
                "size_mb": round(dest_path.stat().st_size / (1024 * 1024), 2)
            }
        else:
            logger.error(f"Remux failed: {res.stderr}")
            _discard_reservation(dest_path, reserved)
            return {"status": "failed", "error": res.stderr}
    except Exception as e:
        logger.error(f"Remux error for {source_path.name}: {e}")
        _discard_reservation(dest_path, reserved)
        return {"status": "failed", "error": str(e)}


def _discard_reservation(dest_path: Path, reserved: bool) -> None:
    """Remove a name this remux claimed and failed to fill.

    The .ts survives a failed remux under its original name, so the retagged
    placeholder -- or FFmpeg's partial output in it -- belongs to nothing and
    would hold the name against the next attempt.
    """
    if reserved:
        try:
            dest_path.unlink(missing_ok=True)
        except OSError:
            pass
