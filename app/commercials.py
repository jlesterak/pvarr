#!/usr/bin/env python3
"""
PVArr Commercial Detection

Optional, off by default, and never destructive unless asked. Runs after the
remux has finished and the recording is already safely in the library, so
nothing here can cost you footage that was captured.

Two deliberate choices:

* **Chapters by default, cutting only on request.** comskip is a heuristic. A
  false positive in chapter mode is a skip point you ignore; a false positive
  in cut mode deletes a play that cannot be re-recorded, because there is no
  re-downloading a live game. Everything else in this project is built to avoid
  silently losing footage, and a detector that edits recordings by default
  would sit badly beside that.
* **It runs after the "recording finished" notification, not before.** Comskip
  is roughly 20-40 minutes of CPU on a three-hour capture. Putting it ahead of
  the notification would leave an operator waiting half an hour to be told a
  recording they can already watch had finished.

What it is good at, and what it is not: comskip was built for broadcast TV and
leans on station logos, black frames, aspect changes and audio silence. On a
rebroadcast OTA channel that is exactly right. On a stream that fills its
breaks with an animated "commercial break in progress" card it has much less to
work with -- such a card is not black, not silent, and not frozen, so the
obvious signals all miss it.
"""

import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.check_deps import find_executable

logger = logging.getLogger("PVArrCommercials")

# comskip is roughly real-time or faster, but a long recording on a busy NAS is
# not. Generous, because being killed halfway produces nothing useful.
DEFAULT_TIMEOUT_SEC = 3 * 60 * 60

# comskip's own defaults write nothing we can use. These two outputs are the
# whole point: ffmeta gives chapters ffmpeg can apply directly, and the EDL
# gives the ranges needed to cut.
_DEFAULT_INI = "output_edl=1\noutput_ffmeta=1\n"


def comskip_path() -> Optional[str]:
    """Where comskip is, or None. Absent is normal: it is an optional extra."""
    return find_executable("comskip") or None


def enabled() -> bool:
    """Off unless asked for. It costs real CPU on every finished recording."""
    return os.getenv("PVARR_COMSKIP", "0").strip().lower() in ("1", "true", "yes", "on")


def mode() -> str:
    """`chapters` (safe) or `cut` (destructive). Anything else means chapters."""
    requested = os.getenv("PVARR_COMSKIP_MODE", "chapters").strip().lower()
    return "cut" if requested == "cut" else "chapters"


def _ini_path(workdir: Path) -> Path:
    """The operator's comskip.ini if they supplied one, else a minimal default.

    Tuning is per-source and genuinely matters -- the logo and blank-frame
    thresholds that work for one broadcaster are wrong for another -- so an
    operator who has done that work must be able to bring their own file.
    """
    supplied = os.getenv("PVARR_COMSKIP_INI", "").strip()
    if supplied and Path(supplied).is_file():
        return Path(supplied)
    generated = workdir / "comskip.ini"
    generated.write_text(_DEFAULT_INI, encoding="utf-8")
    return generated


def parse_edl(edl_text: str) -> List[Tuple[float, float]]:
    """Commercial ranges from comskip's EDL.

    Lines are `start<TAB>end<TAB>action`, seconds as floats. Action 0 is "cut";
    anything else is a mute or a commercial-break marker we do not act on.
    Malformed lines are skipped rather than fatal -- a detector must never be
    the reason a finished recording is lost.
    """
    ranges: List[Tuple[float, float]] = []
    for line in (edl_text or "").splitlines():
        parts = line.replace(",", ".").split()
        if len(parts) < 2:
            continue
        try:
            start, end = float(parts[0]), float(parts[1])
        except ValueError:
            continue
        action = parts[2] if len(parts) > 2 else "0"
        if action != "0" or end <= start:
            continue
        ranges.append((start, end))
    return ranges


def detect(video_path: str, timeout: int = DEFAULT_TIMEOUT_SEC) -> Optional[Dict[str, Any]]:
    """Run comskip. Returns its outputs, or None if it could not be run.

    Every path here is an explicit argv element; nothing is interpolated into a
    shell.
    """
    binary = comskip_path()
    source = Path(video_path)
    if not binary or not source.is_file():
        return None

    workdir = Path(tempfile.mkdtemp(prefix="pvarr-comskip-"))
    try:
        ini = _ini_path(workdir)
        cmd = [binary, f"--ini={ini}", f"--output={workdir}", str(source)]
        try:
            subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            logger.warning("comskip timed out on %s after %ss", source.name, timeout)
            return None
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("comskip could not run on %s: %s", source.name, exc)
            return None

        stem = source.stem
        ffmeta = workdir / f"{stem}.ffmeta"
        edl = workdir / f"{stem}.edl"
        # comskip exits non-zero in normal operation (its status encodes
        # "commercials found"), so the outputs are the only reliable signal.
        if not ffmeta.is_file() and not edl.is_file():
            logger.info("comskip produced no output for %s", source.name)
            return None

        return {
            "workdir": workdir,
            "ffmeta": str(ffmeta) if ffmeta.is_file() else "",
            "edl": str(edl) if edl.is_file() else "",
            "breaks": parse_edl(edl.read_text(encoding="utf-8", errors="replace")
                                if edl.is_file() else ""),
        }
    except Exception as exc:            # never let detection kill a recording
        logger.warning("comskip failed on %s: %s", source.name, exc)
        shutil.rmtree(workdir, ignore_errors=True)
        return None


def apply_chapters(video_path: str, ffmeta_path: str) -> bool:
    """Write comskip's chapters into the file, without re-encoding.

    Stream copy, so this is an I/O-bound remux of container metadata and takes
    seconds rather than the length of the recording. Written to a temp file and
    moved into place only on success: the recording is already in the library
    and someone may be watching it.
    """
    source, meta = Path(video_path), Path(ffmeta_path)
    if not source.is_file() or not meta.is_file():
        return False
    ffmpeg = find_executable("ffmpeg") or "ffmpeg"
    tmp = source.with_name(f".{source.stem}.chapters{source.suffix}")
    cmd = [
        ffmpeg, "-y",
        "-i", str(source),
        "-i", str(meta),
        "-map_metadata", "1",
        "-map_chapters", "1",
        "-codec", "copy",
        str(tmp),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if result.returncode != 0 or not tmp.is_file() or tmp.stat().st_size == 0:
            logger.warning("Could not write chapters into %s", source.name)
            tmp.unlink(missing_ok=True)
            return False
        os.replace(tmp, source)
        return True
    except Exception as exc:
        logger.warning("Chapter write failed for %s: %s", source.name, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False


def keep_ranges(breaks: List[Tuple[float, float]], duration: float) -> List[Tuple[float, float]]:
    """Invert the commercial ranges into the parts worth keeping.

    Overlapping or out-of-order ranges are merged first: comskip can emit them,
    and naively inverting an unsorted list produces negative-length keeps that
    silently drop content.
    """
    if duration <= 0:
        return []
    merged: List[List[float]] = []
    for start, end in sorted(breaks):
        start, end = max(0.0, start), min(duration, end)
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])

    keeps: List[Tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start - cursor > 0.1:        # ignore slivers shorter than a frame or two
            keeps.append((cursor, start))
        cursor = end
    if duration - cursor > 0.1:
        keeps.append((cursor, duration))
    return keeps


def media_duration(video_path: str) -> Optional[float]:
    """Duration in seconds via ffprobe, or None."""
    ffprobe = find_executable("ffprobe") or "ffprobe"
    cmd = [ffprobe, "-v", "error", "-show_entries", "format=duration",
           "-of", "default=nw=1:nk=1", str(video_path)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        return float((result.stdout or "").strip())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def cut_breaks(
    video_path: str,
    breaks: List[Tuple[float, float]],
    tolerance: float = 30.0,
) -> bool:
    """Remove the commercial ranges, but only replace the original if verified.

    This is the destructive path, so it is built to refuse rather than guess.
    The new file is written beside the original and is only moved into place
    after ffprobe says it is playable *and* its duration dropped by roughly the
    length of what was cut. A stream-copy concat that silently produced a
    30-second file from a three-hour recording would otherwise replace the
    recording with the wreckage.

    Cuts land on keyframes because the parts are copied, not re-encoded --
    a second or two of slop at each boundary, in exchange for not spending
    hours of CPU re-encoding a recording.
    """
    source = Path(video_path)
    if not source.is_file() or not breaks:
        return False
    duration = media_duration(video_path)
    if not duration:
        logger.warning("Refusing to cut %s: could not read its duration.", source.name)
        return False

    keeps = keep_ranges(breaks, duration)
    if not keeps:
        logger.warning("Refusing to cut %s: nothing would be left.", source.name)
        return False

    expected = sum(end - start for start, end in keeps)
    if expected <= 0 or expected > duration + 1:
        logger.warning("Refusing to cut %s: implausible target duration.", source.name)
        return False

    ffmpeg = find_executable("ffmpeg") or "ffmpeg"
    workdir = Path(tempfile.mkdtemp(prefix="pvarr-comcut-"))
    try:
        parts: List[Path] = []
        for index, (start, end) in enumerate(keeps):
            part = workdir / f"part{index:04d}{source.suffix}"
            cmd = [ffmpeg, "-y", "-ss", f"{start:.3f}", "-to", f"{end:.3f}",
                   "-i", str(source), "-c", "copy",
                   "-avoid_negative_ts", "make_zero", str(part)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if result.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
                logger.warning("Refusing to cut %s: segment %s failed.", source.name, index)
                return False
            parts.append(part)

        listing = workdir / "parts.txt"
        listing.write_text(
            "".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        cut = workdir / f"cut{source.suffix}"
        concat = [ffmpeg, "-y", "-f", "concat", "-safe", "0",
                  "-i", str(listing), "-c", "copy", str(cut)]
        result = subprocess.run(concat, capture_output=True, text=True, timeout=3600)
        if result.returncode != 0 or not cut.is_file() or cut.stat().st_size == 0:
            logger.warning("Refusing to cut %s: concat failed.", source.name)
            return False

        # The verification that makes this safe to ship.
        actual = media_duration(str(cut))
        if actual is None:
            logger.warning("Refusing to replace %s: the cut file is unreadable.", source.name)
            return False
        if abs(actual - expected) > tolerance:
            logger.warning(
                "Refusing to replace %s: cut is %.1fs but %.1fs was expected "
                "(tolerance %.0fs). Keeping the original.",
                source.name, actual, expected, tolerance,
            )
            return False

        if keep_original():
            backup = source.with_name(f"{source.stem}.original{source.suffix}")
            shutil.copy2(source, backup)
            logger.info("Kept the uncut recording as %s", backup.name)

        shutil.move(str(cut), str(source))
        logger.info(
            "Cut %s: %.1fs removed across %s breaks.",
            source.name, duration - actual, len(breaks),
        )
        return True
    except Exception as exc:
        logger.warning("Cut failed for %s: %s", source.name, exc)
        return False
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def keep_original() -> bool:
    """Whether to keep an uncut copy beside the cut recording.

    On by default. Cutting is a heuristic acting on footage that cannot be
    re-recorded, so the safe default is the one that costs disk rather than the
    one that costs a play.
    """
    return os.getenv("PVARR_COMSKIP_KEEP_ORIGINAL", "1").strip().lower() not in (
        "0", "false", "no", "off")


def process(video_path: str) -> Dict[str, Any]:
    """Detect commercials and mark (or remove) them. Never raises.

    Returns a small summary for the log and the session record.
    """
    result: Dict[str, Any] = {"ran": False, "breaks": 0, "mode": mode(),
                              "applied": False, "cut": False}
    if not enabled():
        return result
    if not comskip_path():
        logger.info("PVARR_COMSKIP is on but comskip is not installed; skipping.")
        return result

    found = detect(video_path)
    if not found:
        return result

    result["ran"] = True
    result["breaks"] = len(found["breaks"])
    try:
        if found["ffmeta"]:
            result["applied"] = apply_chapters(video_path, found["ffmeta"])
        if result["mode"] == "cut" and found["breaks"]:
            # Chapters were written first deliberately: if the cut refuses its
            # own verification, the operator is left with a marked-up recording
            # rather than nothing.
            result["cut"] = cut_breaks(video_path, found["breaks"])
    finally:
        _discard_workdir(found.get("workdir"), video_path)
    return result


def _discard_workdir(workdir: Any, video_path: str) -> None:
    """Remove comskip's scratch directory, and nothing else.

    detect() always creates this with mkdtemp, so in practice it is always a
    fresh temp directory. The guard exists because the consequence of that
    assumption being wrong one day is `rmtree` on a directory full of
    recordings -- and this project spends a lot of effort making sure captured
    footage is never lost quietly. A recursive delete built on an assumption is
    exactly the shape of the bug worth refusing to write.
    """
    if not workdir:
        return
    target = Path(workdir).resolve()
    tmp_root = Path(tempfile.gettempdir()).resolve()
    video_parent = Path(video_path).resolve().parent
    if target == video_parent or not target.is_relative_to(tmp_root):
        logger.warning(
            "Refusing to remove %s: it is not a scratch directory.", target)
        return
    shutil.rmtree(target, ignore_errors=True)
