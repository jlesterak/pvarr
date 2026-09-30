#!/usr/bin/env python3
"""
PVArr - comskip scoring harness

Turns "these comskip settings seem better" into a number. Two steps:

  truth  Build an answer key for one recording from the channel's corner logo.
         Broadcasters put a static logo on every frame of the programme and on
         none of the ads, so "logo present" is a ground truth that owes nothing
         to comskip. Only keyframes are decoded, so a 3-4 hour game takes about
         half a minute.

  score  Compare a comskip .edl with that answer key: how much ad time it
         caught, how much it missed, and how much of the game it marked as an
         ad (the number that matters before anyone turns on cut mode).

Usage:
  scripts/score-comskip.py truth GAME.mp4 --logo-box 160:40:1100:12 > truth.csv
  scripts/score-comskip.py score truth.csv GAME.edl [--exclude 6400-6984 ...]

--logo-box is ffmpeg's crop order, W:H:X:Y in pixels of the recording. Find it
by grabbing one frame (`ffmpeg -ss 600 -i GAME.mp4 -frames:v 1 f.png`) and
boxing the logo tightly; some background around it is fine.

Always spot-check the key by eye before trusting a score: halftime shows,
studio segments and replays can drop the logo too. Leave those out of the
scoring with --exclude rather than editing the key.

Needs ffmpeg and ffprobe; standard library only.
"""

import argparse
import csv
import re
import statistics
import subprocess
import sys
from typing import List, Tuple

Range = Tuple[float, float]


def _logo_crops(video: str, box: str) -> Tuple[List[float], List[bytes]]:
    """Grayscale logo-box crops of every keyframe, with their timestamps."""
    w, h, _, _ = (int(v) for v in box.split(":"))
    cmd = ["ffmpeg", "-nostdin", "-v", "info", "-skip_frame", "nokey", "-i", video,
           "-vf", f"crop={box},format=gray,showinfo", "-fps_mode", "passthrough",
           "-f", "rawvideo", "-"]
    proc = subprocess.run(cmd, capture_output=True, check=True)
    times = [float(t) for t in re.findall(rb"pts_time:\s*([0-9.]+)", proc.stderr)]
    size = w * h
    frames = [proc.stdout[i:i + size] for i in range(0, len(proc.stdout), size)]
    if len(frames) != len(times) or not frames:
        sys.exit(f"crop/timestamp mismatch: {len(frames)} frames, {len(times)} times")
    return times, frames


def logo_track(video: str, box: str) -> List[Tuple[float, float]]:
    """(time, fraction of the logo visible) per keyframe.

    The template is the per-pixel median over a sample of frames: the logo
    holds still while the picture behind it moves, so the median keeps the
    logo and washes the background out. Only pixels that are stable across
    most samples count as logo, which works for white, coloured or
    semi-transparent logos alike.
    """
    times, frames = _logo_crops(video, box)
    sample = frames[::max(1, len(frames) // 400)]
    n = len(frames[0])
    template = [statistics.median(f[p] for f in sample) for p in range(n)]
    mask = [p for p in range(n)
            if sum(abs(f[p] - template[p]) <= 20 for f in sample) >= 0.6 * len(sample)]
    if len(mask) < 0.02 * n:
        sys.exit("no stable logo found in that box -- check --logo-box")
    return [(t, sum(abs(f[p] - template[p]) <= 30 for p in mask) / len(mask))
            for t, f in zip(times, frames)]


def breaks_from_track(track: List[Tuple[float, float]], min_break: float,
                      threshold: float = 0.5, blip: int = 2) -> List[Range]:
    """Logo-absent runs of at least min_break seconds.

    Runs of up to `blip` keyframes are flipped to match their neighbours first,
    so one ad that happens to show a similar corner, or one dropped detection,
    does not split or invent a break.
    """
    present = [score >= threshold for _, score in track]
    i = 0
    while i < len(present):
        j = i
        while j < len(present) and present[j] == present[i]:
            j += 1
        if 0 < i and j < len(present) and j - i <= blip:
            present[i:j] = [not present[i]] * (j - i)
        i = j
    step = statistics.median(b[0] - a[0] for a, b in zip(track, track[1:]))
    out: List[Range] = []
    i = 0
    while i < len(present):
        j = i
        while j < len(present) and present[j] == present[i]:
            j += 1
        if not present[i]:
            start = track[i][0]
            end = track[j][0] if j < len(track) else track[-1][0] + step
            if end - start >= min_break:
                out.append((start, end))
        i = j
    return out


def parse_edl(path: str) -> List[Range]:
    """Commercial ranges (action 0) from a comskip .edl."""
    ranges = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            parts = line.split()
            if len(parts) >= 3 and parts[2] == "0":
                ranges.append((float(parts[0]), float(parts[1])))
    return ranges


def score(truth: List[Range], detected: List[Range], exclude: List[Range]) -> dict:
    """Second-by-second overlap of the detected breaks with the answer key."""
    length = int(max([e for _, e in truth + detected] + [0])) + 1

    def cover(ranges: List[Range]) -> bytearray:
        sec = bytearray(length)
        for a, b in ranges:
            sec[int(a):int(b)] = b"\x01" * (int(b) - int(a))
        return sec

    t, d, x = cover(truth), cover(detected), cover(exclude)
    caught = missed = false_pos = 0
    for i in range(length):
        if x[i]:
            continue
        caught += t[i] and d[i]
        missed += t[i] and not d[i]
        false_pos += d[i] and not t[i]
    return {"caught": caught, "missed": missed, "false_pos": false_pos,
            "recall": caught / max(1, caught + missed),
            "breaks_detected": len(detected),
            "whole_breaks_missed": sum(
                1 for a, b in truth
                if not any(x[int(a):int(b)]) and not any(d[int(a):int(b)]))}


def _range(text: str) -> Range:
    a, b = text.split("-")
    return float(a), float(b)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    tr = sub.add_parser("truth", help="build an answer key from the corner logo")
    tr.add_argument("video")
    tr.add_argument("--logo-box", required=True, help="W:H:X:Y, ffmpeg crop order")
    tr.add_argument("--min-break", type=float, default=20.0,
                    help="shortest logo-absent run counted as a break (s)")
    sc = sub.add_parser("score", help="score a comskip .edl against an answer key")
    sc.add_argument("truth")
    sc.add_argument("edl", nargs="+")
    sc.add_argument("--exclude", type=_range, action="append", default=[],
                    help="START-END seconds left out of scoring (e.g. halftime)")
    args = ap.parse_args()

    if args.cmd == "truth":
        writer = csv.writer(sys.stdout)
        for start, end in breaks_from_track(logo_track(args.video, args.logo_box),
                                            args.min_break):
            writer.writerow([f"{start:.3f}", f"{end:.3f}"])
        return

    with open(args.truth, encoding="utf-8") as fh:
        truth = [(float(a), float(b)) for a, b in csv.reader(fh)]
    print(f"{'edl':40} {'recall':>6} {'caught':>7} {'missed':>7} "
          f"{'game-as-ad':>10} {'breaks':>6} {'whole-missed':>12}")
    for edl in args.edl:
        r = score(truth, parse_edl(edl), args.exclude)
        print(f"{edl[-40:]:40} {r['recall']:6.0%} {r['caught'] / 60:6.1f}m "
              f"{r['missed'] / 60:6.1f}m {r['false_pos'] / 60:9.1f}m "
              f"{r['breaks_detected']:6} {r['whole_breaks_missed']:12}")


if __name__ == "__main__":
    main()
