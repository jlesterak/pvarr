#!/usr/bin/env python3
"""
PVArr break-card detection

Some sources replace the broadcast's ad breaks with their own filler: an
animated "commercial break" card, a loop of promos, sometimes nothing but a
still picture. comskip finds almost none of it (5% of break time on the one
recording measured), because it looks for black frames, a station logo and
silence at the edges of a break, and a card has none of those.

What gives a card away is that it repeats. A source shows the same card (or
the same loop) in every break, so the same pictures come back many minutes
apart. Live play never does that: a camera never frames exactly the same
picture twice. So a second is break when its picture closely matches one
from more than REPEAT_GAP_SEC away, and such seconds hold for a break's
length. The gap keeps a still picture or a slow shot from matching itself.

This replaced an earlier "quiet audio and score overlay gone" rule, which
found nothing on a hockey recording whose audio never dropped and whose
overlay moved between stitched sources. On two hockey recordings the repeat
rule found every card the sponsor checked (all of one game's ~22 min of
breaks, four of four spot-checked spans on the other).

What it misses: a card shown only once in a recording, and a break that is
real live footage (a studio segment). What it can wrongly mark: anything
else shown twice, such as a long black or "stream offline" slate.

Cost: one FFmpeg decode of the keyframes at 64x36 grayscale (niced, one
thread), then a comparison of every second against every other in numpy:
a few seconds. Under 200 MB at one keyframe a second for three hours.
"""

import logging
import os
import re
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.check_deps import find_executable

logger = logging.getLogger("PVArrBreakCards")

# Thumbnail as decoded, then halved again for the comparison (32x18): small
# enough that encoder noise averages out, big enough to tell shots apart.
THUMB_W, THUMB_H = 64, 36
FRAME_BYTES = THUMB_W * THUMB_H

MATCH_RMS = 3.0           # pictures this close (0-255 gray, RMS) are the same picture
REPEAT_GAP_SEC = 300      # ... and only count when they are this far apart
HOLD_SEC = 10             # a keyframe stands for the seconds after it, up to this
MERGE_GAP_SEC = 10
MIN_BREAK_SEC = 45
MIN_FRAME_GAP_SEC = 1.0   # at most one picture a second, bounding memory and CPU
BLOCK_ROWS = 256          # comparison rows at a time; bounds memory
PASS_TIMEOUT_SEC = 30 * 60

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")
Span = Tuple[float, float]


# --- the decision, kept free of I/O so it can be tested on synthetic data ---

def spans_from_seconds(flags: Sequence[bool], gap: int = MERGE_GAP_SEC,
                       min_len: int = MIN_BREAK_SEC) -> List[Span]:
    """Runs of True seconds, gaps under `gap` merged, runs under `min_len` dropped."""
    spans: List[List[float]] = []
    start: Optional[int] = None
    for i, flag in enumerate(list(flags) + [False]):
        if flag and start is None:
            start = i
        elif not flag and start is not None:
            if spans and start - spans[-1][1] < gap:
                spans[-1][1] = i
            else:
                spans.append([start, i])
            start = None
    return [(float(a), float(b)) for a, b in spans if b - a >= min_len]


def repeated_frames(frame_times: Sequence[float], frames: Sequence[bytes]) -> List[bool]:
    """For each frame: does a near-identical one appear REPEAT_GAP_SEC or more away?"""
    # One BLAS thread: this runs beside recordings. Only takes effect if
    # numpy is not imported yet, which in PVArr it is not.
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    import numpy as np

    n = len(frames)
    raw = np.frombuffer(b"".join(frames), dtype=np.uint8).reshape(n, THUMB_H // 2, 2, THUMB_W // 2, 2)
    a = np.empty((n, (THUMB_H // 2) * (THUMB_W // 2)), dtype=np.float32)
    for i in range(0, n, BLOCK_ROWS):   # in blocks: a whole-file float copy is 4x the bytes
        a[i:i + BLOCK_ROWS] = raw[i:i + BLOCK_ROWS].astype(np.float32).mean(axis=(2, 4)).reshape(-1, a.shape[1])
    dims = a.shape[1]
    sq = (a * a).sum(axis=1)
    t = np.asarray(frame_times, dtype=np.float64)
    limit = MATCH_RMS * MATCH_RMS * dims
    out = np.zeros(n, dtype=bool)
    for i in range(0, n, BLOCK_ROWS):
        d2 = sq[i:i + BLOCK_ROWS, None] + sq[None, :] - 2.0 * (a[i:i + BLOCK_ROWS] @ a.T)
        far = np.abs(t[i:i + BLOCK_ROWS, None] - t[None, :]) >= REPEAT_GAP_SEC
        out[i:i + BLOCK_ROWS] = ((d2 < limit) & far).any(axis=1)
    return out.tolist()


def find_breaks(frame_times: Sequence[float], frames: Sequence[bytes]) -> List[Span]:
    """Break ranges in seconds from keyframe thumbnails and their times."""
    if len(frames) < 2 or len(frames) != len(frame_times):
        return []
    try:
        repeated = repeated_frames(frame_times, frames)
    except ImportError:
        logger.info("numpy is not installed; no card breaks.")
        return []
    seconds = int(max(frame_times)) + 1
    flags = [False] * seconds
    k = 0
    for s in range(seconds):
        while k + 1 < len(frame_times) and frame_times[k + 1] <= s:
            k += 1
        flags[s] = repeated[k] and 0 <= s - frame_times[k] < HOLD_SEC
    return spans_from_seconds(flags)


# --- FFmpeg passes ---

def parse_frames(raw: bytes, log: str) -> Tuple[List[float], List[bytes]]:
    """Pair raw thumbnails with showinfo's timestamps, at most one per MIN_FRAME_GAP_SEC."""
    times = [float(m.group(1)) for line in log.splitlines()
             if "showinfo" in line and (m := _PTS_RE.search(line))]
    count = min(len(times), len(raw) // FRAME_BYTES)
    if abs(len(times) - len(raw) // FRAME_BYTES) > 2:
        # Timestamps and pictures paired by position; if the counts disagree
        # the pairing is a guess, and a guess here marks the wrong minutes.
        logger.info("Break-card frames and timestamps disagree; skipping.")
        return [], []
    kept_t: List[float] = []
    kept_f: List[bytes] = []
    for i in range(count):
        if kept_t and times[i] - kept_t[-1] < MIN_FRAME_GAP_SEC:
            continue
        kept_t.append(times[i])
        kept_f.append(raw[i * FRAME_BYTES:(i + 1) * FRAME_BYTES])
    return kept_t, kept_f


def _input(video_path: str) -> str:
    # An absolute file: URL, so no file name can read as an option or a protocol.
    return "file:" + str(Path(video_path).resolve())


def _run(cmd: List[str], what: str, name: str) -> Optional[subprocess.CompletedProcess]:
    # Niced and single-threaded: a recording running next to this matters more.
    nice = find_executable("nice")
    if nice:
        cmd = [nice, "-n", "10"] + cmd
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=PASS_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        logger.warning("Break-card %s pass timed out on %s", what, name)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Break-card %s pass could not run on %s: %s", what, name, exc)
        return None
    if result.returncode != 0:
        logger.warning("Break-card %s pass failed on %s: %s", what, name,
                       result.stderr.decode("utf-8", "replace")[-300:])
        return None
    return result


def detect(video_path: str) -> List[Span]:
    """Card breaks in a finished recording. Never raises; [] when unsure."""
    source = Path(video_path)
    ffmpeg = find_executable("ffmpeg")
    if not ffmpeg or not source.is_file():
        return []
    name = source.name
    try:
        video = _run([
            ffmpeg, "-nostdin", "-nostats", "-v", "info", "-skip_frame", "nokey",
            "-threads", "1", "-i", _input(video_path), "-an", "-filter_threads", "1",
            "-vf", f"showinfo,scale={THUMB_W}:{THUMB_H}:flags=area,format=gray",
            "-fps_mode", "passthrough", "-f", "rawvideo", "-",
        ], "video", name)
        if video is None:
            return []
        times, frames = parse_frames(video.stdout, video.stderr.decode("utf-8", "replace"))
        del video
        return find_breaks(times, frames)
    except Exception as exc:        # a detector must never cost a recording
        logger.warning("Break-card detection failed on %s: %s", name, exc)
        return []
