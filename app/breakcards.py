#!/usr/bin/env python3
"""
PVArr break-card detection

Some sources replace the broadcast's ad breaks with their own filler: an
animated "commercial break" card, a loop of promos, sometimes nothing but a
still picture. comskip finds almost none of it (5% of break time on the one
recording measured), because it looks for black frames, a station logo and
silence at the edges of a break, and a card has none of those.

Two things do give a card away, and this module needs both at once:

* **The audio drops.** The game runs at one level; the card's music sits well
  under it (about 17 dB on the measured source, one break fully silent).
* **The score overlay disappears.** The overlay is the one part of the picture
  that stays put through the game. Its position differs per network, so it is
  learned from each recording, never configured.

Needing both is deliberate. Audio alone also fires on a quiet stretch of the
game; overlay alone fires on replays and studio segments. Together they marked
about 89% of break time with nothing wrongly marked, measured against two
independent signals (exactly repeated footage, which live play never produces,
and the overlay itself). A recording with no stable overlay gets no card
breaks at all rather than an audio-only guess.

Cost: two FFmpeg decodes of the finished file, about 75 s of one core for a
two-and-a-half-hour 720p game, and under 100 MB of memory. Stdlib only.
"""

import logging
import math
import re
import statistics
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from app.check_deps import find_executable

logger = logging.getLogger("PVArrBreakCards")

# Thumbnail size. Small enough that a four-hour recording is a few tens of
# MB in memory, big enough that a score overlay covers a dozen or more pixels.
THUMB_W, THUMB_H = 64, 36
FRAME_BYTES = THUMB_W * THUMB_H

QUIET_DROP_DB = 10.0      # how far under the recording's median a break sits
SMOOTH_SEC = 15           # rolling median half-width: 31 s window
STABLE_MAD = 3            # a pixel this steady through the game is overlay
MIN_OVERLAY_PIXELS = 8    # fewer than this: no overlay found, give up
DARK_PIXEL = 24           # black bars and borders are steady too; not overlay
PIXEL_CHANGED = 25        # a pixel this far from its overlay value has changed
GONE_IN_QUIET = 0.3       # overlay pixels must vanish this often when it goes quiet
ABSENT_SHARE = 0.6        # overlay gone = this share of its pixels changed
MERGE_GAP_SEC = 15
OVERLAY_GAP_SEC = 6       # the overlay must stay gone, not flicker (replays, graphics)
MIN_BREAK_SEC = 30
MIN_FRAME_GAP_SEC = 1.0   # bounds memory on sources where every frame is a keyframe
MAX_TEMPLATE_FRAMES = 4000   # plenty to learn a steady overlay; bounds CPU and memory
PASS_TIMEOUT_SEC = 30 * 60
SILENT_DB = -120.0

_PTS_RE = re.compile(r"pts_time:\s*(-?[0-9.]+)")
Span = Tuple[float, float]


# --- the decision, kept free of I/O so it can be tested on synthetic data ---

def rolling_median(values: Sequence[float], half_width: int) -> List[float]:
    n = len(values)
    return [statistics.median(values[max(0, i - half_width):i + half_width + 1])
            for i in range(n)]


def quiet_seconds(rms_db: Sequence[float]) -> List[bool]:
    """Seconds whose smoothed loudness sits QUIET_DROP_DB under the recording's median."""
    if not rms_db:
        return []
    threshold = statistics.median(rms_db) - QUIET_DROP_DB
    return [v < threshold for v in rolling_median(rms_db, SMOOTH_SEC)]


def overlay_pixels(frames: Sequence[bytes],
                   quiet_frames: Sequence[bytes] = ()) -> Tuple[List[int], List[int]]:
    """Pixels that barely change across `frames`, with their typical value.

    `frames` should be ones the audio calls game, where the overlay is on
    screen. With `quiet_frames`, a steady pixel only counts if it changes in
    at least GONE_IN_QUIET of them: background that never moves, in play or
    out, is not an overlay. Returns (pixel indexes, template values).
    """
    if not frames:
        return [], []
    joined = b"".join(frames)
    pixels: List[int] = []
    template: List[int] = []
    for p in range(FRAME_BYTES):
        column = sorted(joined[p::FRAME_BYTES])
        med = column[len(column) // 2]
        if med < DARK_PIXEL:
            continue
        deviations = sorted(abs(v - med) for v in column)
        if deviations[len(deviations) // 2] < STABLE_MAD:
            pixels.append(p)
            template.append(med)
    if quiet_frames:
        kept = [(p, t) for p, t in zip(pixels, template)
                if sum(abs(f[p] - t) > PIXEL_CHANGED for f in quiet_frames)
                >= GONE_IN_QUIET * len(quiet_frames)]
        pixels, template = [p for p, _ in kept], [t for _, t in kept]
    return pixels, template


def overlay_gone(frame: bytes, pixels: Sequence[int], template: Sequence[int]) -> bool:
    """Most of the overlay changed. A share, not a mean: when the score
    changes a few digit pixels move, and that must not read as the overlay
    leaving."""
    changed = sum(abs(frame[p] - t) > PIXEL_CHANGED for p, t in zip(pixels, template))
    return changed >= ABSENT_SHARE * len(pixels)


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


def _fill(spans: List[Span], n: int) -> List[bool]:
    flags = [False] * n
    for a, b in spans:
        for s in range(int(a), min(int(b), n)):
            flags[s] = True
    return flags


def _thin(frames: List[bytes]) -> List[bytes]:
    if len(frames) <= MAX_TEMPLATE_FRAMES:
        return frames
    step = len(frames) / MAX_TEMPLATE_FRAMES
    return [frames[int(i * step)] for i in range(MAX_TEMPLATE_FRAMES)]


def find_breaks(rms_db: Sequence[float], frame_times: Sequence[float],
                frames: Sequence[bytes]) -> List[Span]:
    """Break ranges in seconds from per-second loudness and keyframe thumbnails."""
    if not rms_db or not frames or len(frames) != len(frame_times):
        return []
    quiet = quiet_seconds(rms_db)

    def second(t: float) -> int:
        return min(max(int(t), 0), len(quiet) - 1)

    game = [f for f, t in zip(frames, frame_times) if not quiet[second(t)]]
    hushed = [f for f, t in zip(frames, frame_times) if quiet[second(t)]]
    pixels, template = overlay_pixels(_thin(game), _thin(hushed))
    if len(pixels) < MIN_OVERLAY_PIXELS:
        logger.info("No steady score overlay found (%d pixels); no card breaks.", len(pixels))
        return []

    absent_at = [overlay_gone(f, pixels, template) for f in frames]
    # Each second takes the state of the latest keyframe at or before it.
    absent = [False] * len(quiet)
    k = 0
    for s in range(len(quiet)):
        while k + 1 < len(frame_times) and frame_times[k + 1] <= s:
            k += 1
        absent[s] = absent_at[k] if frame_times[k] <= s + 1 else False
    # Each signal has to hold for a break's length on its own before they are
    # combined. Combining second by second first let an overlay that flickered
    # through a quiet stretch of play (replays) be bridged into a "break".
    n = len(quiet)
    quiet_runs = _fill(spans_from_seconds(quiet), n)
    absent_runs = _fill(spans_from_seconds(absent, gap=OVERLAY_GAP_SEC), n)
    # No cap on the share marked is needed: "quiet" is measured against the
    # recording's own median, so it can never cover more than half the file.
    return spans_from_seconds([q and a for q, a in zip(quiet_runs, absent_runs)])


# --- FFmpeg passes ---

def parse_rms(text: str) -> List[float]:
    """Per-second RMS levels from FFmpeg's ametadata print output."""
    levels: dict = {}
    current: Optional[int] = None
    for line in text.splitlines():
        m = _PTS_RE.search(line)
        if line.startswith("frame:") and m:
            current = int(float(m.group(1)))
        elif "RMS_level=" in line and current is not None:
            raw = line.split("=", 1)[1].strip()
            try:
                value = float(raw)
            except ValueError:
                value = SILENT_DB
            levels[current] = value if math.isfinite(value) else SILENT_DB
    if not levels:
        return []
    return [levels.get(s, SILENT_DB) for s in range(max(levels) + 1)]


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
        audio = _run([
            ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-i", _input(video_path), "-vn",
            "-af", "aresample=8000,asetnsamples=8000,astats=metadata=1:reset=1,"
                   "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
            "-f", "null", "-",
        ], "audio", name)
        if audio is None:
            return []
        rms = parse_rms(audio.stdout.decode("utf-8", "replace"))

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
        return find_breaks(rms, times, frames)
    except Exception as exc:        # a detector must never cost a recording
        logger.warning("Break-card detection failed on %s: %s", name, exc)
        return []
