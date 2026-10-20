#!/usr/bin/env python3
"""
Measure a working stream's real quality, so the best ones fill the slots.

A probe proves a stream plays; it says nothing about how well. For each
working candidate this module:

  1. reads the playlist and, for a master playlist, takes its best variant
     (the one FFmpeg records: highest bandwidth);
  2. downloads three recent segments -- a few seconds of video each, size-
     and time-capped, skipping the newest, which a relay may still be filling
     -- and times the last two, once the connection is warm: seconds of video
     per second of download is the stream's headroom (2.0x = it arrives twice
     as fast as it plays);
  3. runs ffprobe on one segment for the real resolution, frame rate and
     codec. ffprobe reads headers only: no decoding, a few milliseconds of CPU.

Cost per stream: about 2-10 MB of download, typically 1-2 s; the caller
bounds the total (aggregator.RANK_BUDGET_SEC) and abandons a stream that
stalls. Candidates are
measured one at a time: measured together they compete for the same link
and the numbers say more about each other than about the stream (measured
2026-10-19: one stream read 6-11x alone, 1-7x alongside five others). Headroom matters most: a 1080p stream that barely keeps
up stalls and fails over, so a stream under MIN_HEADROOM ranks below every
one that keeps up, whatever its resolution.

Off with PVARR_RANK_STREAMS=0 (metered connections): candidates then keep
page order, as before.
"""

import ipaddress
import json
import os
import re
import subprocess
import tempfile
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

import requests

from app import probe
from app.check_deps import find_executable

MIN_HEADROOM = 1.5
SEGMENTS = 3          # recent segments fetched; the first only warms the connection
SEGMENT_MAX_BYTES = 12 * 1024 * 1024    # ~10 s of 8 Mbps video, with margin
SEGMENT_TIMEOUT_SEC = 12                # wall clock per segment
PLAYLIST_TIMEOUT_SEC = 6
FFPROBE_TIMEOUT_SEC = 10
MIN_VIDEO_KBPS = 100    # below this it is not a video feed, whatever it sniffs as

_EXTINF_RE = re.compile(r"^#EXTINF:\s*([0-9.]+)")
_GENERIC_PATH_RE = re.compile(
    r"(?:/(?:hls|live|stream|streams|tv|video|media|playlist))*"
    r"/(?:index|playlist|master|chunklist|mono|stream|live|video)[\w-]*\.m3u8?")


def enabled() -> bool:
    return os.environ.get("PVARR_RANK_STREAMS", "1").strip().lower() not in ("0", "false", "no", "off")


def _best_variant(variants: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """The variant FFmpeg records by default: the highest bandwidth."""
    usable = [v for v in variants if v.get("url")]
    if not usable:
        return None
    return max(usable, key=lambda v: (v.get("bandwidth") or 0, _height(v.get("resolution"))))


def _height(resolution: Optional[str]) -> int:
    m = re.match(r"^\d+x(\d+)$", resolution or "")
    return int(m.group(1)) if m else 0


def _segments(body: bytes, base: str) -> List[Tuple[str, float]]:
    """(url, seconds) for every segment in a media playlist, in order."""
    out, duration = [], None
    for line in body.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        m = _EXTINF_RE.match(line)
        if m:
            try:
                duration = float(m.group(1))
            except ValueError:
                duration = None
        elif line and not line.startswith("#"):
            if duration and 0 < duration <= 60:
                out.append((urljoin(base, line), duration))
            duration = None
    return out


def _looks_like_media(data: bytes) -> bool:
    """MPEG-TS (0x47 sync every 188 bytes, three packets seen), fragmented
    MP4, or ID3-tagged TS/AAC; not an HTML error page served with a 200, nor
    a short body that merely starts with "G"."""
    if len(data) >= 377 and data[0:1] == data[188:189] == data[376:377] == b"\x47":
        return True
    if data[4:8] in (b"ftyp", b"styp", b"moof", b"sidx", b"moov"):
        return True
    return data[:3] == b"ID3"


def _ffprobe(data: bytes) -> Dict[str, Any]:
    """Resolution, frame rate and codec of a segment's first video stream."""
    exe = find_executable("ffprobe") or "ffprobe"
    with tempfile.NamedTemporaryFile(suffix=".ts") as fh:
        fh.write(data)
        fh.flush()
        cmd = [exe, "-v", "error", "-select_streams", "v:0",
               "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,codec_name",
               "-of", "json", fh.name]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=FFPROBE_TIMEOUT_SEC)
            stream = (json.loads(res.stdout or "{}").get("streams") or [{}])[0]
        except (OSError, subprocess.SubprocessError, ValueError, IndexError):
            return {}
    fps = None
    for key in ("avg_frame_rate", "r_frame_rate"):
        num, _, den = str(stream.get(key) or "").partition("/")
        try:
            value = float(num) / float(den or 1)
        except (ValueError, ZeroDivisionError):
            continue
        if 0 < value <= 240:
            fps = round(value, 2)
            break
    return {"width": stream.get("width"), "height": stream.get("height"),
            "fps": fps, "codec": stream.get("codec_name")}


def measure(res: Dict[str, Any], allow_private: bool = True,
            deadline: Optional[float] = None) -> Dict[str, Any]:
    """Quality of one working candidate, from its probe result `res`.

    Never raises: anything that goes wrong yields what was measured so far
    and a `note`. `deadline` (monotonic) stops further downloads.
    """
    out: Dict[str, Any] = {"measured": False, "height": None, "width": None, "fps": None,
                           "codec": None, "kbps": None, "headroom": None,
                           "declared_height": None, "declared_kbps": None, "note": ""}
    playlist = res.get("m3u8_url") or ""
    if not playlist:
        out["note"] = "no playlist to measure"
        return out
    headers = {"User-Agent": res.get("user_agent") or probe.DEFAULT_USER_AGENT}
    if res.get("referer"):
        headers["Referer"] = res["referer"]
    if res.get("cookie"):
        headers["Cookie"] = res["cookie"]
    plain = probe.NoPrivateRedirects(requests.Session())
    fetcher = (probe._browser_tls_session() if res.get("impersonate") else None) or plain

    def safe(url: str) -> bool:
        return allow_private or not probe.is_private_url(url)

    try:
        variant = _best_variant(res.get("variants") or [])
        if variant:
            out["declared_height"] = _height(variant.get("resolution")) or None
            if variant.get("bandwidth"):
                out["declared_kbps"] = round(variant["bandwidth"] / 1000)
            playlist = variant["url"]
        if not safe(playlist):
            out["note"] = "refused: private address"
            return out
        resp, body = probe._fetch(fetcher, playlist, headers, PLAYLIST_TIMEOUT_SEC)
        if not (resp.ok and probe._is_playlist_body(body)):
            out["note"] = f"playlist answered {resp.status_code}"
            return out
        segs = _segments(body, resp.url)[:-1][-SEGMENTS:]   # skip the newest
        if not segs:
            out["note"] = "no segments listed"
            return out

        got_bytes, got_secs, spent, first, too_slow = 0, 0.0, 0.0, b"", False
        wall = (SEGMENT_TIMEOUT_SEC // 2) * 2       # probe._fetch's wall-clock cap
        for n, (url, secs) in enumerate(segs):
            if deadline is not None and time.monotonic() > deadline:
                break
            if not safe(url):
                out["note"] = "refused: private address"
                return out
            t0 = time.monotonic()
            try:
                r, data = probe._fetch(plain, url, headers, SEGMENT_TIMEOUT_SEC // 2,
                                       max_bytes=SEGMENT_MAX_BYTES)
            except probe._FETCH_ERRORS as exc:
                # One bad segment keeps what the others measured. A stall
                # (before the headers: Timeout; mid-body: a ConnectionError
                # after a full read timeout) is a stream that does not keep up.
                out["note"] = f"segment failed: {type(exc).__name__}"
                too_slow = too_slow or isinstance(exc, requests.Timeout) \
                    or time.monotonic() - t0 >= SEGMENT_TIMEOUT_SEC // 2
                continue
            took = time.monotonic() - t0
            if not (r.ok and data):
                out["note"] = f"segment answered {r.status_code}"
                break
            if not _looks_like_media(data):
                out["note"] = "segment is not video"
                continue        # never credited, whichever segment it is
            first = first or data
            if n == 0 and len(segs) > 1:
                continue        # connection set-up (DNS, TLS) would skew it
            try:
                length = int(r.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length and len(data) < length:
                # Cut off by a cap: credit only the share that arrived.
                share = len(data) / length
            elif len(data) >= SEGMENT_MAX_BYTES:
                continue        # over the size cap, length unknown: no honest number
            elif took >= wall - 0.5:
                too_slow = True     # still arriving when the clock ran out
                continue
            else:
                share = 1.0
            if len(data) * 8 / (secs * share) / 1000 < MIN_VIDEO_KBPS:
                # A few hundred bytes "for" seconds of video is no video feed,
                # whatever it sniffs as; timing it would inflate headroom.
                out["note"] = "too little data to be video"
                continue
            got_bytes += len(data)
            got_secs += secs * share
            spent += took
        if not first and not too_slow:
            out["note"] = out["note"] or "segment is not video"
            return out
        if got_secs and spent > 0:
            out["kbps"] = round(got_bytes * 8 / got_secs / 1000)
            out["headroom"] = round(got_secs / spent, 2)
            out["measured"] = True
        if too_slow:
            out["headroom"] = min(out["headroom"] or 1.0, 1.0)
            out["measured"] = True
        if first:
            out.update({k: v for k, v in _ffprobe(first).items() if v})
    except probe._FETCH_ERRORS as exc:
        out["note"] = f"could not measure: {type(exc).__name__}"
    except Exception as exc:  # measuring is a bonus; it must never sink a pick
        out["note"] = f"could not measure: {type(exc).__name__}"
    finally:
        for session in {id(plain): plain, id(fetcher): fetcher}.values():
            try:
                session.close()
            except Exception:
                pass
    return out


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def source_key(m3u8_url: str) -> str:
    """Which real feed a playlist is, ignoring the edge server and its token.

    Several sites often relay one origin through numbered edge servers
    (`edge1.cdn.example/hls/abc.m3u8`, `edge4.cdn.example/hls/abc.m3u8?st=...`):
    as failover these are one source, since they die together when the origin
    does. Key = host with its digits removed, plus any port and the path (case
    kept: paths are case-sensitive); the query (tokens, expiry) is dropped.
    An IP address is kept exact. "" when unknown.
    """
    parts = urlsplit(m3u8_url or "")
    try:
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:      # a port that is not a number
        return ""
    if not host:
        return ""
    path = parts.path
    # Only a path naming a specific stream identifies the feed. A generic one
    # ("/live/index.m3u8") on tv1.example and tv2.example says nothing about
    # being the same origin, so then the exact host is kept.
    if not _is_ip(host) and not _GENERIC_PATH_RE.fullmatch(path.lower()):
        host = re.sub(r"\d+", "", host)
    return f"{host}{f':{port}' if port else ''}{path}"


def diversify(order: List[int], keys: Dict[int, str], slow: Dict[int, bool], pick: int) -> List[int]:
    """Choose `pick` of `order` (best first) so the backups are, where possible,
    different sources from the primary and from each other.

    The primary is always the best. Then: the best stream of each source not
    yet held, among those that keep up; then the same allowing slow ones (a
    slow independent backup beats a fast copy of a feed that is already
    down); then the best of the rest. An unknown key counts as its own source.
    """
    if not order:
        return []
    chosen = [order[0]]
    held = {keys.get(order[0]) or f"?{order[0]}"}
    for allow_slow in (False, True):
        for i in order[1:]:
            if len(chosen) >= pick:
                return chosen
            key = keys.get(i) or f"?{i}"
            if i in chosen or key in held or (slow.get(i) and not allow_slow):
                continue
            chosen.append(i)
            held.add(key)
    for i in order[1:]:
        if len(chosen) >= pick:
            break
        if i not in chosen:
            chosen.append(i)
    return chosen


def score(q: Dict[str, Any]) -> Tuple:
    """Sort key, best first: keeps up, then real resolution, frame rate,
    bitrate in 500 kbps steps, then headroom.

    Unmeasured streams sit between those that keep up and those that do not:
    nothing is known against them, but nothing proves them good either.
    Bitrate is bucketed because several sites often relay one source: their
    1.7 vs 1.8 Mbps is noise, and the faster delivery should win (measured
    2026-10-19: six relays of one 720p feed, 2.3x to 6.7x real-time).
    """
    headroom = q.get("headroom")
    if headroom is None:
        tier = 1
    else:
        tier = 0 if headroom >= MIN_HEADROOM else 2
    # A resolution ffprobe saw beats one the playlist merely claims
    # (RESOLUTION=1x99999 must not outrank a measured 1080p); the claim only
    # orders streams whose delivery was timed but ffprobe could not read, and
    # only within reason. Nothing measured: claims count for nothing, so a
    # failed measurement keeps page order.
    trusted = bool(q.get("measured"))
    height = q.get("height") or 0
    claimed = 0 if height or not trusted else min(q.get("declared_height") or 0, 2160)
    smooth = 1 if (q.get("fps") or 0) >= 48 else 0
    kbps = q.get("kbps") or (min(q.get("declared_kbps") or 0, 50000) if trusted else 0)
    return (tier, -height, -claimed, -smooth, -(kbps // 500), -(headroom or 0))


def summary(q: Dict[str, Any]) -> str:
    """'1080p60 · 6.1 Mbps · 4.2x real-time', or why it could not be measured."""
    parts = []
    height = q.get("height") or min(q.get("declared_height") or 0, 2160)
    if height:
        fps = q.get("fps")
        parts.append(f"{height}p{round(fps) if fps and fps >= 48 else ''}")
    kbps = q.get("kbps") or min(q.get("declared_kbps") or 0, 50000)
    if kbps:
        parts.append(f"{kbps / 1000:.1f} Mbps")
    if q.get("headroom") is not None:
        h = q["headroom"]
        parts.append(f"{h:.1f}x real-time" + (" (too slow)" if h < MIN_HEADROOM else ""))
    text = " · ".join(parts)
    if q.get("note"):
        text = f"{text} ({q['note']})" if text else q["note"]
    return text
