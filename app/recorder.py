#!/usr/bin/env python3
"""
PVArr Core Recorder & Multi-Stream Failover Engine
Implements Direct-First FFmpeg connection with automatic fallback to hls-proxy-stream,
dynamic HTTP header injection, freeze detection, and continuous segment appending.
"""

import collections
import io
import json
import logging
import math
import os
import re
import select
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from typing import List, Optional, Callable, Dict, Any, Set, Tuple

from app.check_deps import find_executable
from app.logging_config import redact_url_secrets
from app import browser, quality, ytdlp
from app.probe import DEFAULT_USER_AGENT, is_private_url, probe_stream
from app.relay import PlaylistRelay

logger = logging.getLogger("PVArrRecorder")


ALLOWED_URL_SCHEMES = ("http", "https")


def safe_stream_url(value: str) -> str:
    """Reject anything that is not a plain http(s) URL.

    FFmpeg speaks far more than HTTP. Handed `file:///etc/passwd` it will
    happily open it, `concat:` will splice two local files together, and
    `tcp://host:port` will connect anywhere the container can reach. Every
    candidate URL comes from an unauthenticated caller, and the captured bytes
    are readable back through the stream and download endpoints -- so an
    unconstrained scheme turns PVArr into a file-read and port-scan primitive
    for anything on the LAN. Checked here as well as at the API boundary
    because this is the last point before the URL reaches an argv list.
    """
    url = (value or "").strip()
    if not url:
        raise ValueError("Stream URL is empty.")
    scheme = url.split(":", 1)[0].lower() if ":" in url else ""
    if scheme not in ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Unsupported URL scheme {scheme or '(none)'!r}: "
            "only http:// and https:// stream URLs are accepted."
        )
    return url


def safe_header_value(value: str) -> Optional[str]:
    """Return the value, or None if it cannot be put in a header safely.

    FFmpeg pastes -headers straight into its outgoing HTTP request, so a CR or
    LF in a Referer/User-Agent/Cookie injects extra headers. These values are
    not all operator-typed: probe.py accepts a `referer=` taken from the query
    string of a third-party m3u8 URL and percent-decodes it, so a hostile page
    can supply one containing a real CRLF.

    Rejected rather than stripped -- a silently mangled cookie produces a
    confusing 403 much later, where a refusal names the problem at once.
    """
    if value is None:
        return None
    if any(ch in value for ch in ("\r", "\n", "\x00")):
        return None
    return value

# Free-space floor below which a recording aborts rather than filling the
# volume. Module level rather than a class attribute so callers can read it
# without going through StreamFailoverRecorder, which tests routinely patch.
# Each session reserves a contiguous block of proxy ports: start_proxy() binds
# base_port + candidate_index, so a session with three candidates occupies
# base_port .. base_port + 2. The allocator in server.py hands out base ports
# this far apart, so one session's third candidate cannot land on the next
# session's primary. Must stay greater than the maximum candidates per session.
PROXY_PORT_STRIDE = 4

DEFAULT_MIN_FREE_GB = 5.0

# Global backstop on how long any one recording may run, in hours. A capture
# pointed at a 24/7 channel never ends on its own: the stream does not stop, so
# nothing in the failover logic ever fires and the file grows until the disk
# guard trips. That is a safety net doing a scheduler's job.
#
# 6 rather than 4: 4 truncates NFL overtime and extra-innings baseball, which
# are the most likely things to be recording unattended. A per-recording
# duration overrides this; 0 disables it entirely.
DEFAULT_MAX_HOURS = 6.0


def origin_of(url: str) -> str:
    """scheme://host[:port] of an http(s) URL, or "" for anything else."""
    parts = urlsplit(url or "")
    if parts.scheme in ("http", "https") and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return ""


class StreamOutcome(str, Enum):
    """Why a single FFmpeg attempt ended.

    A bare bool cannot express this. Previously "did any bytes arrive" stood in
    for "did the stream finish", so a mid-recording stall or crash looked
    identical to a clean finish and the loop stopped instead of failing over --
    silently truncating the recording at the point of failure.
    """

    COMPLETED = "completed"      # FFmpeg exited 0: the stream genuinely ended
    FAILED = "failed"            # died without delivering a single byte
    INTERRUPTED = "interrupted"  # delivered data, then stalled or exited non-zero


# Cache keyed by ffmpeg path: the answer cannot change while we run, and this
# shells out.
_HLS_EXT_FLAGS_CACHE: Dict[str, List[str]] = {}


def hls_extension_flags(ffmpeg_path: Optional[str]) -> List[str]:
    """FFmpeg options that stop the HLS demuxer refusing a segment by extension.

    Some origins serve MPEG-TS segments under a non-video extension (one
    seen in testing used URLs ending ".image"), and FFmpeg's HLS demuxer
    refuses any extension outside its allowlist. hls-proxy mirrors the upstream extension onto its own
    /proxy.<ext> path, so the rewritten segments get refused for the same
    reason -- which is why the fallback could not rescue that stream either.

    Which option unlocks it depends on the build, and they are not
    interchangeable. Measured against the shipped image (Debian's ffmpeg
    5.1.9) with a real .image segment:

        no flags                        refused: not in allowed_segment_extensions
        -allowed_extensions ALL         refused: not in allowed_segment_extensions
        -allowed_segment_extensions ALL refused: "extension none mismatches"
        -extension_picky 0              PASS

    So `extension_picky` is the one that matters there -- and it does not exist
    on upstream 6.1, which has only `allowed_extensions`. Passing an option a
    build does not know is fatal, so ask the binary what it supports rather
    than guessing from a version number.
    """
    key = ffmpeg_path or "ffmpeg"
    if key in _HLS_EXT_FLAGS_CACHE:
        return list(_HLS_EXT_FLAGS_CACHE[key])

    flags: List[str] = []
    try:
        result = subprocess.run(
            [key, "-hide_banner", "-h", "demuxer=hls"],
            capture_output=True, text=True, timeout=10,
        )
        help_text = (result.stdout or "") + (result.stderr or "")
    except (OSError, subprocess.SubprocessError):
        help_text = ""

    for option, value in (
        ("allowed_extensions", "ALL"),
        ("allowed_segment_extensions", "ALL"),
        ("extension_picky", "0"),
    ):
        if f"-{option} " in help_text:
            flags.extend([f"-{option}", value])

    _HLS_EXT_FLAGS_CACHE[key] = list(flags)
    return flags


# --- MPEG-TS output timeline ------------------------------------------------
#
# Every failover starts a fresh FFmpeg, and FFmpeg normalises each input to its
# own zero -- a source whose own clock reads one hour still comes out of
# `-c copy -f mpegts` starting at ~1.42s. Those bytes are appended to the same
# .ts, so without intervention the file's timeline jumps *backwards* at every
# switch. A player reads that as time travel: mpv logs "Invalid audio PTS ...
# Reset playback due to audio timestamp reset", the demuxer resyncs, and
# ffprobe reports only the duration of the first segment.
#
# The cure is `-output_ts_offset`, which needs to know where the previous
# segment ended. Nothing tells us that except the bytes themselves, so we read
# the timestamps back out of our own output.

TS_PACKET_SIZE = 188
TS_SYNC_BYTE = 0x47
PTS_CLOCK_HZ = 90000.0
# A PTS is 33 bits at 90kHz, so the counter wraps at ~26.5 hours. Past that an
# offset stops being meaningful and the muxer would wrap mid-file.
PTS_WRAP_SECONDS = (1 << 33) / PTS_CLOCK_HZ
# Bytes of our own output kept in memory so a segment's final timestamp can be
# read when it ends. Comfortably more than one PES interval at any sane bitrate.
TIMELINE_TAIL_BYTES = 262144
# Hard ceiling on an offset before it is treated as garbage rather than clamped.
# Well past any plausible capture, and far enough above the 26.5h wrap that a
# genuinely long rebroadcast channel is not cut off by it.
MAX_TIMELINE_OFFSET_SECONDS = 30 * 24 * 3600.0


def _ts_alignment(buf: bytes) -> Optional[int]:
    """Find where the 188-byte packet grid starts inside `buf`.

    The capture loop reads in 64KB chunks, which is not a multiple of 188, so a
    buffer sliced out of that stream almost never begins on a packet boundary.
    Rather than have callers track a running byte count -- which a resumed
    recording could not reconstruct anyway -- the grid is found by looking for
    an offset where several sync bytes line up. Three in a row is enough to be
    sure; 0x47 appears in payload often, three times at exactly 188-byte spacing
    does not.
    """
    if len(buf) < TS_PACKET_SIZE:
        return None
    probes = min(3, len(buf) // TS_PACKET_SIZE)
    for off in range(TS_PACKET_SIZE):
        if off + (probes - 1) * TS_PACKET_SIZE >= len(buf):
            break
        if all(buf[off + k * TS_PACKET_SIZE] == TS_SYNC_BYTE for k in range(probes)):
            return off
    return None


def _packet_pts(packet: bytes) -> Optional[float]:
    """Presentation timestamp from one TS packet, or None if it carries none.

    Every field consulted here comes off the wire, including the adaptation
    field length used as a skip distance, so each step is bounds-checked before
    it is trusted. A malformed packet must yield None, never an exception: this
    runs on the capture thread, where an unhandled IndexError would end a
    recording that was otherwise healthy.
    """
    if len(packet) != TS_PACKET_SIZE or packet[0] != TS_SYNC_BYTE:
        return None
    # Only the first packet of a PES carries the header holding the timestamp.
    if not packet[1] & 0x40:
        return None
    adaptation = (packet[3] >> 4) & 0x3
    if not adaptation & 0x1:
        return None  # adaptation field only, no payload
    pos = 4
    if adaptation & 0x2:
        pos += 1 + packet[4]  # attacker-controlled length: may run off the end
    # start code (3) + stream id (1) + length (2) + flags (2) + hdr len (1)
    # + PTS (5)
    if pos + 14 > TS_PACKET_SIZE:
        return None
    if packet[pos:pos + 3] != b"\x00\x00\x01":
        return None
    stream_id = packet[pos + 3]
    # Audio (0xC0-0xDF) and video (0xE0-0xEF) only. Padding, private and
    # PSI streams either carry no PTS or carry one that is not the programme's.
    if not (0xC0 <= stream_id <= 0xEF):
        return None
    if not packet[pos + 7] & 0x80:
        return None  # PTS_DTS_flags says no PTS present
    b = packet[pos + 9:pos + 14]
    # Marker bits are fixed 1s in a well-formed PTS field. Checking them is the
    # cheapest way to reject a header that only looks like one.
    if not (b[0] & 0x01 and b[2] & 0x01 and b[4] & 0x01):
        return None
    ticks = (
        ((b[0] >> 1) & 0x07) << 30
        | b[1] << 22
        | ((b[2] >> 1) & 0x7F) << 15
        | b[3] << 7
        | ((b[4] >> 1) & 0x7F)
    )
    return ticks / PTS_CLOCK_HZ


def last_timeline_position(buf: bytes) -> Optional[float]:
    """Highest presentation timestamp in a run of MPEG-TS packets.

    The maximum rather than the last one found: audio and video are interleaved
    and neither is reliably ahead, so taking whichever PES header happens to
    come last would under-report the end of the segment -- and an offset that
    is too small puts the backward jump straight back.
    """
    start = _ts_alignment(buf)
    if start is None:
        return None
    best: Optional[float] = None
    for off in range(start, len(buf) - TS_PACKET_SIZE + 1, TS_PACKET_SIZE):
        pts = _packet_pts(buf[off:off + TS_PACKET_SIZE])
        if pts is not None and (best is None or pts > best):
            best = pts
    return best


def advance_timeline_position(previous: float, observed: float) -> float:
    """Move the running timeline to a freshly-read PTS, allowing for wraparound.

    A PTS field is 33 bits at 90kHz, so it restarts every ~26.5 hours. Our own
    running offset does not: it keeps counting. Past the wrap the value read
    back out of the file is therefore *smaller* than where we know we are, and
    assigning it directly would recreate the very backward jump this exists to
    remove -- a day late rather than at every failover.

    So the reading is treated as a distance travelled since `previous`, measured
    the short way round. A distance in the far half of the circle means the read
    went backwards rather than nearly all the way around, which happens if the
    tail scan under-reports; in that case the timeline holds still. Standing
    still can only cost a small forward gap at the splice. Moving backwards
    breaks playback, so that is the direction to refuse.
    """
    span = (observed - previous) % PTS_WRAP_SECONDS
    if span > PTS_WRAP_SECONDS / 2:
        return previous
    return previous + span


def tail_timeline_position(path, tail_bytes: int = TIMELINE_TAIL_BYTES):
    """Where the timeline of an existing .ts file has reached.

    Used when a recording resumes onto a file an earlier process wrote: that
    append is a splice like any other, and starting the offset back at zero
    would reintroduce exactly the discontinuity this exists to prevent.
    """
    try:
        size = os.path.getsize(path)
        if size <= 0:
            return None
        with open(path, "rb") as fh:
            fh.seek(max(0, size - tail_bytes))
            return last_timeline_position(fh.read())
    except OSError:
        return None


def output_ts_offset_flags(offset: Optional[float]) -> List[str]:
    """`-output_ts_offset` argv for a continuing segment, or nothing.

    The value is derived from stream content, which is remote and therefore
    hostile input, so it is validated here rather than trusted from the caller:
    NaN and infinity render as words that FFmpeg would reject or misread, a
    negative would push timestamps below zero, and anything past the 33-bit
    wrap is meaningless. Fixed-point formatting keeps exponent notation --
    which FFmpeg does not accept as a duration -- out of the argv.
    """
    if offset is None:
        return []
    try:
        value = float(offset)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(value) or value <= 0.0:
        return []
    if value > MAX_TIMELINE_OFFSET_SECONDS:
        return []
    return ["-output_ts_offset", f"{value:.6f}"]


# FFmpeg's HLS demuxer reports a lost segment only at warning level. Wording
# checked against real output of both the shipped 5.1.9 and 6.1, driven by a
# local live source that 404s one segment and stalls another past the window.
#
# The segment patterns are anchored to the whole line *and* to the hls demuxer's
# own context. Unanchored, any warning that quotes source-controlled text -- an
# HTTP reason phrase, a URI from the playlist -- could carry "skipping
# 999999999999 segments ahead" and inflate the count shown to the operator.
_FFMPEG_LEVEL_RE = re.compile(
    r"^(?:\[[^\]]+\] )*\[(panic|fatal|error|warning|info|verbose|debug|trace)\] ")
_HLS_WARNING = r"^\[(?:[^\]/]*/)?hls @ 0x[0-9a-fA-F]+\] \[warning\] "
_SEGMENT_FAILED_RE = re.compile(
    _HLS_WARNING + r"Failed to open segment (\d{1,12}) of playlist (\d{1,6})$")
_SEGMENTS_EXPIRED_RE = re.compile(
    _HLS_WARNING + r"skipping (\d{1,12}) segments ahead, expired from playlists?$")
# Why the next "Failed to open segment" happened, read from the lines FFmpeg
# writes just before it (checked against FFmpeg 6.1 with this recorder's flags):
# a 404 logs "HTTP error 404 Not Found" / "Server returned 404 Not Found"; an
# open that times out logs "keepalive request failed ... Connection timed out".
# A segment that times out on a fresh connection logs no reason at all, so a
# failure counts as the connection's unless one of these named the source.
# Refused connections and failed lookups are not a slow link either.
_SOURCE_REASON_RE = re.compile(
    r"HTTP error [45]\d\d|Server returned [45]\d\d|Connection refused|Failed to resolve")
_SLOW_REASON_RE = re.compile(
    r"Connection timed out|Operation timed out|Connection reset by peer|Input/output error",
    re.I)
# An expiry this soon after a source error follows FFmpeg's retries of the bad
# segment, not a slow connection.
SOURCE_SHADOW_SEC = 10.0
# No live window expires more than this in one go; FFmpeg's 15s read timeout
# fails the attempt long before. Anything larger is not a real count.
_MAX_EXPIRED_PER_LINE = 10000


def url_for_log(url: Optional[str], limit: int = 70) -> str:
    """A URL shortened for a log line, redacted *before* it is cut.

    _log() redacts every line at the sink, but by then the URL may already
    have been truncated -- and a path token cut short enough no longer looks
    like a token, so its first characters passed straight through.
    """
    return redact_url_secrets(url or "")[:limit]


class _TailBuffer:
    """The last ~256KB of one FFmpeg invocation's output, kept for its PTS.

    Deliberately a deque of the original chunks rather than a single bytes
    object trimmed on each write. The obvious version --
    `buf = (buf + chunk)[-CAP:]` -- reallocates and copies a quarter of a
    megabyte on every chunk, roughly ten times a second per recording, for the
    life of every recording, to serve a value that is read exactly once when
    the invocation ends. Appending references and dropping them from the front
    copies nothing; the single join happens at the one moment the bytes are
    actually needed.
    """

    def __init__(self, cap: int = TIMELINE_TAIL_BYTES):
        self.cap = cap
        self._chunks: "collections.deque" = collections.deque()
        self._size = 0

    def __bool__(self) -> bool:
        """True once anything has been captured -- i.e. the attempt got data."""
        return bool(self._chunks)

    def append(self, chunk: bytes) -> None:
        self._chunks.append(chunk)
        self._size += len(chunk)
        # Keep the first chunk that still straddles the cap: dropping it would
        # take the buffer below the window we promised to retain.
        while len(self._chunks) > 1 and self._size - len(self._chunks[0]) >= self.cap:
            self._size -= len(self._chunks.popleft())

    def bytes(self) -> bytes:
        return b"".join(self._chunks)


class _FileSink:
    """The recording sink: an append handle that knows whether it still exists.

    A plain `open(path, "ab")` handle keeps working perfectly after the file
    it points at is deleted -- writes succeed, the offset advances, and nothing
    raises. The bytes go to an inode with no name and are freed when the handle
    closes. NFS makes this visible as a `.nfsXXXX` silly-rename; on a local
    filesystem it is completely invisible.

    That is not hypothetical: a recording was lost to it. The library delete
    endpoint unlinked a file that was being recorded to, and the capture loop
    wrote four minutes of video into the hole without noticing, while the
    dashboard showed 0 MB because it stats the path rather than the handle.
    So the sink carries the check with it.
    """

    def __init__(self, path):
        self.path = Path(path)
        self._fh = open(self.path, "ab")

    def write(self, data: bytes) -> int:
        return self._fh.write(data)

    def flush(self) -> None:
        self._fh.flush()

    def is_intact(self) -> bool:
        """True while our handle still refers to whatever is at our path.

        Inode comparison, not `st_nlink == 0`. A silly-rename is a *rename*,
        so the link count stays 1 and a link-count test passes happily on
        exactly the case this exists to catch.
        """
        try:
            return os.fstat(self._fh.fileno()).st_ino == os.stat(self.path).st_ino
        except OSError:
            return False  # the path is gone entirely

    def reopen(self) -> None:
        """Point at the path again, creating it if it has been removed."""
        try:
            self._fh.close()
        except OSError:
            pass
        self._fh = open(self.path, "ab")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        try:
            self._fh.close()
        except OSError:
            pass
        return False


class _RingSink:
    """Adapts a RingBuffer to the file-like write/flush the capture loop uses.

    The capture loop is the most-debugged code in this project. Rebroadcast
    changes only where the bytes land, so it presents the same tiny interface
    the loop already writes to rather than forking the loop.
    """

    def __init__(self, ring):
        self.ring = ring

    def write(self, data: bytes) -> int:
        return self.ring.write(data)

    def flush(self) -> None:
        # The ring is positional writes to an already-sized file; there is no
        # userspace buffer to push.
        pass

    def is_intact(self) -> bool:
        # The ring owns a fixed file it created and never unlinks mid-capture;
        # there is no path for anyone else to delete out from under it.
        return True

    def reopen(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


MAX_VARIANT_KBPS = 200_000


def usable_variants(master_url: str, variants: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """A master playlist's quality levels that a step-down may record, best first.

    Each is `{"url", "kbps", "height"}`. A level is kept only when its declared
    bitrate is sane, it is plain http(s), and it is on the master's own host:
    the candidate's Cookie and Referer go with every request, and a master on
    one host must not be able to send them to another. A public master may not
    name a level on a private address (the relay re-checks the level's own
    contents on every reload).
    """
    host = (urlsplit(master_url).hostname or "").lower()
    private_ok = is_private_url(master_url)
    out = []
    for v in variants or []:
        url = str(v.get("url") or "")
        bw = v.get("bandwidth")
        if not isinstance(bw, int) or not 0 < bw <= MAX_VARIANT_KBPS * 1000:
            continue
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != host:
            continue
        if not private_ok and is_private_url(url):
            continue
        res = str(v.get("resolution") or "")
        height = int(res.split("x")[1]) if re.fullmatch(r"\d{1,5}x\d{1,5}", res) else 0
        out.append({"url": url, "kbps": bw // 1000, "height": height})
    out.sort(key=lambda v: (v["kbps"], v["height"]), reverse=True)
    return out


def describe_level(kbps: Optional[float], height: int = 0) -> str:
    """'720p, 3.8 Mbps' from our own numbers only (never upstream text)."""
    parts = [f"{height}p"] if height else []
    if kbps:
        parts.append(f"{kbps / 1000:.1f} Mbps")
    return ", ".join(parts) or "unknown quality"


class CandidateStream:
    def __init__(self, url: str, name: str = "Stream"):
        self.url = url.strip()
        self.name = name
        self.m3u8_url: str = ""
        self.referer: str = ""
        self.user_agent: str = DEFAULT_USER_AGENT
        self.cookie: str = ""
        self.slug: str = ""
        self.detected: bool = False
        # Which mechanism supplied the headers: probe, detect-headers, or none.
        self.detect_source: str = ""
        self.detect_note: str = ""
        self.used_proxy: bool = False
        # The playlist is served only to a browser TLS profile, so FFmpeg
        # reads it through app.relay. Set by the probe on every connect.
        self.needs_relay: bool = False
        # Segments are video packed inside PNG/WebP images (app.pngwrap): the relay
        # serves FFmpeg the MPEG-TS inside them. Set by the probe on every connect.
        self.png_wrapped: bool = False
        # The playlist is a JSON segment list FFmpeg cannot read; the relay
        # serves it as m3u8. Set by the probe on every connect.
        self.json_playlist: bool = False
        self.fail_count: int = 0
        self.last_error: str = ""
        # DNS on this network refuses to name the stream's host (0.0.0.0).
        # Set by the probe on every connect; such a source is dead from here.
        self.dns_blocked: bool = False
        # Standby check (a backup probed while another candidate records):
        # "alive", "dead", "wrong_game" or "" when never checked. Read through
        # StreamFailoverRecorder.standby_state(), which ages it out.
        self.standby: str = ""
        self.standby_at: float = 0.0
        # The playlist the last standby check resolved to: says which feed a
        # backup is before it is ever connected to (the "real sources" count).
        self.standby_m3u8: str = ""
        # Quality levels the last probe listed (a master playlist's variants),
        # and the step-down ceiling: None records the best one. A ceiling, not
        # a stored URL, because every connect re-probes and the variant URLs
        # carry tokens that go stale. See StreamFailoverRecorder._check_step.
        self.variants: List[Dict[str, Any]] = []
        self.max_kbps: Optional[int] = None
        # Declared bitrate of this feed's best level, from its last probe or
        # standby check; None when the playlist does not say.
        self.declared_kbps: Optional[int] = None

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Serialise this candidate.

        The cookie is a live session credential -- often the only thing
        standing between a stranger and the operator's paid account -- and
        PVArr's API is unauthenticated by design, on the assumption that it
        sits on a trusted LAN. Those two facts together meant anything that
        could reach port 8999 could read the cookie back out of
        `/api/status`. So it is withheld by default and only the fact of its
        existence is reported; callers that genuinely need the value (writing
        session state to disk, rebuilding an FFmpeg command) opt in.
        """
        data = {
            "url": self.url,
            "name": self.name,
            "m3u8_url": self.m3u8_url,
            "referer": self.referer,
            "user_agent": self.user_agent,
            "has_cookie": bool(self.cookie),
            "detected": self.detected,
            "detect_source": self.detect_source,
            "detect_note": self.detect_note,
            "used_proxy": self.used_proxy,
            "needs_relay": self.needs_relay,
            "png_wrapped": self.png_wrapped,
            "json_playlist": self.json_playlist,
            "fail_count": self.fail_count,
            "dns_blocked": self.dns_blocked,
            "last_error": self.last_error,
        }
        if include_secrets:
            data["cookie"] = self.cookie
        return data


class StreamFailoverRecorder:
    # How long one select() wait on the FFmpeg pipe may last. This is the
    # resolution at which a stall, a stop, or a force-failover is noticed, not
    # a poll of the stream itself: when bytes are flowing select returns at
    # once and the wait never happens.
    READ_POLL_SEC = 0.5
    # Read whatever has arrived, up to this much. Never wait for a full buffer.
    READ_CHUNK_BYTES = 65536
    # How long an attempt may go before its FIRST byte, as opposed to how long
    # it may go quiet once bytes are flowing. FFmpeg emits nothing until it has
    # spawned, opened TLS to the edge, fetched the playlist, pulled enough
    # segments to probe the streams, and muxed the first packets -- on a cold
    # connect to a live HLS source that is routinely longer than the freeze
    # timeout, and the freeze clock used to cover it. A perfectly healthy
    # candidate was therefore abandoned for the crime of being slow to warm up,
    # and with three candidates and three laps a flaky edge could cost every
    # attempt in a row. Measured on 2026-09-20: six consecutive attempts, direct
    # and proxy alike, each abandoned at exactly 15.0s; the seventh connected in
    # under 15s and then ran for an hour. Nothing was wrong with the streams.
    #
    # This is a bound, not a wait. A candidate that is genuinely dead makes
    # FFmpeg exit non-zero within a second or two and is caught by poll() long
    # before this expires, and every individual socket read is already capped by
    # FFmpeg's own -rw_timeout. Only a candidate that is connected but slow ever
    # spends this budget.
    STARTUP_GRACE_SEC = 30
    # Tail of FFmpeg's stderr kept for diagnostics when an attempt fails.
    STDERR_TAIL_LINES = 15
    # Only tail lines this recent are reported as the cause of a failure. The
    # tail spans the whole FFmpeg run, so without this a message from when it
    # joined the stream, hours earlier, was reported as if it caused a freeze.
    STDERR_RECENT_SEC = 60
    SEGMENT_LOSS_LOG_INTERVAL_SEC = 60
    _MAX_TRACKED_FAILURES = 256
    # Lines of recorder log kept for the dashboard. Older lines are dropped.
    LOG_HISTORY_LIMIT = 500
    # How often free space is checked while recording. statvfs is cheap but not
    # free, and checking per chunk would mean thousands of calls a second.
    DISK_CHECK_INTERVAL_SEC = 15.0

    # How often to confirm our open handle still refers to output_filepath,
    # and how many times to recreate it before giving up. Same cadence as the
    # disk guard: two stats every 15s is nothing next to the write path.
    OUTPUT_CHECK_INTERVAL_SEC = 15.0
    MAX_OUTPUT_REOPENS = 3

    def __init__(
        self,
        recording_id: str,
        candidates: List[str],
        output_filepath: str,
        base_port: int = 8090,
        ring=None,
        channel_name: Optional[str] = None,
        freeze_timeout_sec: int = 15,
        log_callback: Optional[Callable[[str], None]] = None,
        on_completion_callback: Optional[Callable[[str], None]] = None,
        on_failover_callback: Optional[Callable[[str, str], None]] = None,
        on_alert_callback: Optional[Callable[[str, str, str], None]] = None,
        header_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        auto_probe: bool = True,
        max_cycles: int = 3,
        min_free_gb: Optional[float] = None,
        end_time: Optional[float] = None,
        max_hours: Optional[float] = None,
    ):
        self.recording_id = recording_id
        # Per-URL header overrides from the dashboard's "advanced" fields, keyed
        # by URL rather than position so an empty backup slot cannot shift them
        # onto the wrong candidate.
        self.header_overrides: Dict[str, Dict[str, str]] = header_overrides or {}
        self.auto_probe = auto_probe
        self.candidates: List[CandidateStream] = [
            CandidateStream(url, name=f"Candidate {i+1}")
            for i, url in enumerate(candidates)
            if url and url.strip()
        ]
        for candidate in self.candidates:
            override = self.header_overrides.get(candidate.url) or {}
            candidate.referer = override.get("referer", "") or ""
            candidate.cookie = override.get("cookie", "") or ""
            if override.get("user_agent"):
                candidate.user_agent = override["user_agent"]
        # Re-checking the event page (set by the server when the session came
        # from one). The snapshot is each URL's fail_count at the last check:
        # a candidate is "dead since" it when its count has gone up.
        self.has_event_page: bool = False
        self.rescan_state: str = ""
        self.last_rescan_at: float = 0.0
        self._fail_snapshot: Dict[str, int] = {}
        self.output_filepath = Path(output_filepath).resolve()
        # Set by the post-processor once the .ts has been remuxed. The original
        # .ts is deleted at that point, so size/name lookups must follow here.
        self.final_filepath: Optional[Path] = None
        self.base_port = base_port
        # Rebroadcast: bytes go to a bounded ring instead of a growing file,
        # and nothing is kept. None means this is an ordinary recording.
        self.ring = ring
        # What Plex should call this channel. A rebroadcast writes no file, so
        # there is no filename for the guide to fall back on.
        self.channel_name = channel_name
        self.freeze_timeout_sec = freeze_timeout_sec
        self.log_callback = log_callback
        self.on_completion_callback = on_completion_callback
        self.on_failover_callback = on_failover_callback
        # (recording_id, title, body): something the operator should hear about
        # while the recording runs, e.g. a burst of lost segments.
        self.on_alert_callback = on_alert_callback
        # Monitor thread state: bytes_written samples for the stream rate, lost
        # segment times for the burst alert, when that alert last fired.
        self._rate_samples: "collections.deque" = collections.deque(maxlen=7)
        self._loss_times: "collections.deque" = collections.deque(maxlen=200)
        self._last_alert_at: float = 0.0
        self._monitor_thread: Optional[threading.Thread] = None
        # Step-down on a slow connection (_check_step). Lost-segment events the
        # connection caused (one per FFmpeg line, not per segment, so one long
        # freeze is one event); when a source error was last seen, so expiries
        # that follow it are not blamed on the connection.
        self._slow_events: "collections.deque" = collections.deque(maxlen=200)
        self._source_fail_at: float = 0.0
        # A pending step for the recording loop: (from index, to index, ceiling).
        # Its own slot, not _manual_target_index, so it never overrides the
        # operator or a wrong-game mark, and is dropped if a failover moved on.
        self._step_request: Optional[Tuple[int, int, Optional[int]]] = None
        self._step_at: float = 0.0           # last step, either way
        self._step_up_at: float = 0.0
        self._step_downs: int = 0
        self._stepped_from: Optional[int] = None  # candidate to go back up to
        self._stepped_to: Optional[int] = None
        self._stay_down: bool = False
        self._no_lighter_logged: bool = False

        self.current_candidate_index: int = 0
        # How many complete laps of the candidate list may pass without a
        # single byte arriving before the recording is given up on. Reset the
        # moment any candidate delivers data, so a long capture that fails over
        # occasionally never exhausts its budget. A lap only counts when no
        # candidate delivered anything during it (_lap_had_data).
        self.max_cycles = max(1, int(max_cycles))
        self.cycles_without_data: int = 0
        self._lap_had_data: bool = False
        # Recordings are uncompressed TS and grow without bound. The recordings
        # volume is usually the same filesystem as everything else, so an
        # unattended 24/7 capture does not just lose itself -- it takes the host
        # down with it. Abort while there is still room to operate.
        self.min_free_bytes = int(
            (DEFAULT_MIN_FREE_GB if min_free_gb is None else float(min_free_gb))
            * 1024 ** 3
        )
        # An absolute epoch deadline, or None to run until the stream ends.
        # Absolute rather than a duration so it survives a restart unchanged:
        # a resumed recording must finish when it was always going to finish,
        # not start its clock again. The same reasoning applies to the backstop
        # below, which is measured from the original start_time.
        self.end_time: Optional[float] = float(end_time) if end_time else None
        self.max_hours: Optional[float] = (
            None if max_hours is None else max(0.0, float(max_hours))
        )
        # Where this recording's output timeline has reached, in seconds. Every
        # FFmpeg after the first is offset by it so the appended segments form
        # one rising timeline instead of each restarting at zero.
        # What the operator asked for when this session was created, set by
        # the server once the record exists. Kept here so it lives and dies
        # with the recorder instead of in a second dict that has to be pruned
        # in step with it.
        self.session_record: Optional[Dict[str, Any]] = None
        self._timeline_offset: float = 0.0
        self._timeline_seeded: bool = False
        self._last_disk_check: float = 0.0
        self._last_output_check: float = 0.0
        self._output_reopens: int = 0
        self.is_running: bool = False
        self.status: str = "initialized"  # initialized, recording, failing_over, completed, failed
        self.start_time: Optional[float] = None
        self.stop_time: Optional[float] = None
        self.bytes_written: int = 0
        # Segments FFmpeg reported lost: failed to download, or expired from
        # the live window before it reached them. Each is a hole of a few
        # seconds that nothing else can see -- the freeze detector watches
        # bytes, and holes this short never trip it. A recording lost 20% of
        # its footage this way without a line in the log.
        self.segments_failed: int = 0
        self.segments_expired: int = 0
        self._loss_unlogged: int = 0
        self._loss_logged_at: float = 0.0
        self.log_history: List[str] = []
        # Total lines ever logged, never reset. log_history is trimmed to the
        # last LOG_HISTORY_LIMIT, so a plain index into it stops advancing once
        # trimming begins -- which silently froze the live log view. Readers
        # track this sequence number instead.
        self._log_seq: int = 0
        # Why the recorder stopped: "operator" (a person clicked stop) or
        # "shutdown" (the container is going away). Drives whether the session
        # is forgotten or kept for resume.
        self.stop_reason: str = "operator"

        self._thread: Optional[threading.Thread] = None
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self._proxy_process: Optional[subprocess.Popen] = None
        self._force_failover_flag: bool = False
        # Set by switch_to_candidate() to redirect the next hop to a specific
        # candidate rather than simply the next one.
        self._manual_target_index: Optional[int] = None
        # Streams the operator marked as the wrong game, by quality.source_key:
        # never connected to again in this recording, under any link. Mirrors
        # of one origin share a key, so a backup that turns out to be the same
        # feed is skipped too -- the case that made "switch to the backup"
        # land on the same wrong game.
        self.wrong_sources: Set[str] = set()
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Resolve executables
        self.hls_proxy_path = find_executable("hls-proxy.py", ["hls-proxy"])
        self.detect_headers_path = find_executable("detect-headers-py.py", ["detect-headers.sh", "detect-headers"])
        self.ffmpeg_path = find_executable("ffmpeg")

    def _log(self, message: str, level: str = "INFO"):
        # Redacted here, at the sink, rather than at each call site. log_history
        # is served by /api/status and the log endpoint, so a token in a log
        # line is a token readable by anything that can reach port 8999 -- and
        # not every URL in these lines is ours to sanitise at the source, since
        # FFmpeg's own error text quotes the URL it was given.
        message = redact_url_secrets(message)
        formatted = f"[{datetime.now().strftime('%H:%M:%S')}] [{level}] [{self.recording_id}] {message}"
        with self._lock:
            self.log_history.append(formatted)
            self._log_seq += 1
            if len(self.log_history) > self.LOG_HISTORY_LIMIT:
                del self.log_history[:-self.LOG_HISTORY_LIMIT]
        logger.info(f"[{self.recording_id}] {message}")
        if self.log_callback:
            try:
                self.log_callback(formatted)
            except Exception:
                pass

    def detect_candidate_headers(self, candidate: CandidateStream) -> bool:
        """Resolve a candidate URL to a playlist plus the headers it needs.

        Detection runs here, at connect time, rather than reusing whatever the
        dashboard found when the recording was created: playlist URLs carry
        short-lived tokens, and a failover an hour in needs a fresh answer.

        Order is cheapest-first: the in-process probe, then yt-dlp, then the
        external detect-headers script, then the URL as given.

        The probe handles the ordinary cases with no extra install. yt-dlp is
        next because it is the one that resolves a page whose player fetches
        its manifest over XHR -- the URL never enters the document, so no
        amount of HTML scraping will find it -- and because it can use a
        browser-compatible TLS client, which some origins require before they
        answer at all.
        """
        candidate.slug = candidate.slug or f"cand_{self.current_candidate_index}"
        candidate.needs_relay = False
        candidate.png_wrapped = False
        candidate.json_playlist = False
        candidate.dns_blocked = False
        candidate.variants = []

        if self.auto_probe and self._probe_candidate(candidate):
            return True

        # yt-dlp, detect-headers and the raw URL would all ask the same blocked
        # host, so skip them and let the caller fail over.
        if candidate.dns_blocked:
            return False

        if self._resolve_via_ytdlp(candidate):
            return True

        if self._detect_via_script(candidate):
            return True

        candidate.m3u8_url = candidate.url
        candidate.detected = True
        candidate.detect_source = "raw"
        candidate.detect_note = "Using the URL as given; no headers detected."
        return True

    @staticmethod
    def _relay_headers(candidate: CandidateStream) -> Dict[str, str]:
        """The headers the probe verified the playlist with, for the relay."""
        headers = {"User-Agent": candidate.user_agent, "Accept": "*/*"}
        if candidate.referer:
            headers["Referer"] = candidate.referer
            headers["Origin"] = origin_of(candidate.referer)
        if candidate.cookie:
            headers["Cookie"] = candidate.cookie
        # Same rule as FFmpeg's -headers: a line break would inject headers.
        return {k: v for k, v in headers.items() if safe_header_value(v)}

    def _probe_candidate(self, candidate: CandidateStream) -> bool:
        """Try the built-in probe. Returns False so the caller can fall through."""
        self._log(f"Probing {candidate.name}: {url_for_log(candidate.url)}...")
        try:
            result = probe_stream(
                candidate.url,
                referer=candidate.referer or None,
                user_agent=candidate.user_agent,
                cookie=candidate.cookie or None,
            )
        except Exception as exc:  # a probe failure must never kill a recording
            self._log(f"Probe error for {candidate.name}: {exc}", "WARN")
            return False

        candidate.dns_blocked = bool(result.get("dns_blocked"))
        source = "probe"
        if not result.get("ok") and browser.worth_trying(result):
            # The page loaded but holds no playlist (and the probe's own yt-dlp
            # pass found none): a player that builds it in JavaScript. Ask the
            # browser sidecar (PVARR_BROWSER_URL), fresh on
            # every connect since playlist tokens expire. Stop abandons the wait.
            self._log(f"Probe found no playlist on {candidate.name}'s page; "
                      "asking the browser sidecar...")
            found = browser.resolve(candidate.url, abort=self._stop_event)
            if found.get("ok"):
                result, source = found, "browser"
            else:
                self._log(f"{candidate.name}: {found.get('message', '')}", "WARN")
        if not result.get("ok"):
            candidate.last_error = result.get("message", "Probe failed")
            self._log(f"Probe found nothing playable for {candidate.name}: {candidate.last_error}", "WARN")
            return False

        candidate.m3u8_url = result["m3u8_url"]
        candidate.variants = list(result.get("variants") or [])
        levels = usable_variants(candidate.m3u8_url, candidate.variants)
        candidate.declared_kbps = levels[0]["kbps"] if levels else None
        candidate.needs_relay = bool(result.get("impersonate"))
        candidate.png_wrapped = bool(result.get("wrapped"))
        candidate.json_playlist = bool(result.get("json_playlist"))
        candidate.referer = result.get("referer", "")
        candidate.cookie = result.get("cookie", "")
        if result.get("user_agent"):
            candidate.user_agent = result["user_agent"]
        candidate.detected = True
        candidate.detect_source = source
        candidate.detect_note = result.get("message", "")
        required = result.get("headers_required") or ["none"]
        self._log(
            f"{'Browser sidecar' if source == 'browser' else 'Probe'} resolved {candidate.name}: "
            f"{result.get('kind', 'playlist')}, "
            f"headers {', '.join(required)}."
        )
        return True

    def _resolve_via_ytdlp(self, candidate: CandidateStream) -> bool:
        """Ask yt-dlp to resolve the candidate. False so the caller falls through.

        Absent yt-dlp is normal, not an error: it is an optional dependency and
        the probe already handles most streams without it.
        """
        if not ytdlp.ytdlp_path():
            return False
        # Skip it when the operator pasted a playlist directly. The probe has
        # already tried that exact URL with every header combination it knows,
        # and yt-dlp's generic extractor would do strictly less -- so this
        # would be up to 45 seconds of stall, on the failover path, to learn
        # nothing. yt-dlp earns its place on *page* URLs, where the manifest is
        # fetched over XHR and there is nothing in the HTML to find.
        if ".m3u8" in urlsplit(candidate.url).path.lower():
            return False
        self._log(f"Asking yt-dlp to resolve {candidate.name}...")
        try:
            found = ytdlp.resolve(candidate.url)
        except Exception as exc:  # a resolver must never kill a recording
            self._log(f"yt-dlp error for {candidate.name}: {exc}", "WARN")
            return False
        if not found:
            self._log(f"yt-dlp found nothing playable for {candidate.name}.", "WARN")
            return False

        candidate.m3u8_url = found["m3u8_url"]
        # Only overwrite what yt-dlp actually supplied. An operator's manually
        # entered Referer outranks a blank one from the extractor.
        for field in ("referer", "user_agent", "cookie"):
            if found.get(field):
                setattr(candidate, field, found[field])
        candidate.detected = True
        candidate.detect_source = "yt-dlp"
        extractor = found.get("extractor") or "generic"
        candidate.detect_note = f"Resolved by yt-dlp ({extractor})."
        self._log(f"yt-dlp resolved {candidate.name} via the {extractor} extractor.")
        return True

    def _detect_via_script(self, candidate: CandidateStream) -> bool:
        """Fallback to the optional detect-headers CLI.

        NOT browser-backed, despite what this used to say. The image clones
        upstream hls-restream-proxy, which ships only the shell version --
        curl following the iframe chain, trying more header combinations than
        the built-in probe. Useful for a page whose m3u8 is reachable by
        following redirects and iframes; useless for one that builds its URL in
        JavaScript, which is the case it was documented as solving.
        """
        if not self.detect_headers_path or not os.path.exists(self.detect_headers_path):
            return False

        self._log(f"Falling back to detect-headers for {candidate.name}...")
        # check_deps also accepts detect-headers.sh, and upstream currently
        # ships only the shell version. Running that through sys.executable
        # makes Python choke on shell syntax, so every detection silently
        # failed and fell through to the undetected path.
        if self.detect_headers_path.endswith(".py"):
            cmd = [sys.executable, self.detect_headers_path]
        else:
            cmd = [self.detect_headers_path]
        cmd += [candidate.url, "--json"]
        if ".m3u8" in candidate.url.split("?")[0].lower():
            cmd.append("--direct")

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                candidate.m3u8_url = data.get("m3u8_url", candidate.url)
                candidate.referer = data.get("referer", "") or candidate.referer
                if data.get("user_agent"):
                    candidate.user_agent = data["user_agent"]
                candidate.slug = data.get("slug", candidate.slug)
                candidate.detected = True
                candidate.detect_source = "detect-headers"
                candidate.detect_note = "Headers supplied by detect-headers."
                self._log(f"Header detection successful for {candidate.name}.")
                return True
            candidate.last_error = res.stderr.strip() or "Detection failed"
        except Exception as e:
            candidate.last_error = str(e)
        return False

    def start_proxy(self, candidate: CandidateStream) -> Optional[str]:
        """Start local hls-proxy instance for candidate stream (Fallback Mode)."""
        if not self.hls_proxy_path or not os.path.exists(self.hls_proxy_path):
            return candidate.m3u8_url
        if self._proxy_process is not None:
            # stop_proxy() keeps hold of a proxy it could not kill. Starting a
            # second would overwrite the only reference to the first -- the
            # forgetting that keeping hold of it exists to prevent -- and could
            # try to bind the port it is still holding. Go direct instead.
            self._log("Not starting hls-proxy: this session's previous proxy "
                      "has not exited.", "ERROR")
            return candidate.m3u8_url

        # Modulo keeps a session inside the block reserved for it even if it
        # somehow carries more candidates than the stride allows.
        port = self.base_port + (self.current_candidate_index % PROXY_PORT_STRIDE)
        conf_dir = self.output_filepath.parent / ".proxy_conf"
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_file = conf_dir / f"channels_{self.recording_id}.conf"

        # hls-proxy's "literal" mode means "this URL *is* the playlist".
        # Every other mode makes it scrape the URL as an HTML page, hunting for
        # an iframe and then an m3u8 inside it.
        #
        # This used to key off the referer, which decides nothing of the sort. A
        # stream needing no Referer -- the common case -- got mode="direct", so
        # the proxy fetched our already-resolved playlist, looked for an
        # <iframe> in what is actually MPEG-TS playlist text, found none, and
        # answered "Channel not found or scrape failed". That is the 404 the
        # fallback died on every time, on a stream that was perfectly healthy.
        #
        # What actually decides the mode is whether we hold a playlist or a page
        # to scrape. The referer is written to its own field either way.
        resolved = candidate.m3u8_url or candidate.url
        is_playlist = ".m3u8" in urlsplit(resolved).path.lower()
        mode = "literal" if is_playlist else "direct"
        # channels.conf is line- and pipe-delimited, so a newline in either
        # field injects an extra channel definition into hls-proxy's config.
        conf_url = safe_header_value(candidate.m3u8_url)
        conf_referer = safe_header_value(candidate.referer) or ""
        if conf_url is None:
            self._log("Refusing to start the proxy: the URL contains a line break.", "ERROR")
            return candidate.m3u8_url
        # Registered *before* the write, not after. This file holds the fully
        # tokenised stream URL, and _remove_proxy_conf() can only delete what it
        # has been told about -- so anything raising between the write and the
        # bookkeeping used to strand a readable credential on the recordings
        # volume with nothing left holding a reference to it.
        self._proxy_conf_file = conf_file
        with open(conf_file, "w", encoding="utf-8") as f:
            f.write(f"{candidate.slug}|{candidate.name}|1||Sports|{conf_url}|{mode}|{conf_referer}|\n")

        env = os.environ.copy()
        # hls-proxy binds 127.0.0.1 unless HLS_PROXY_BIND says otherwise (checked in the pinned source,
        # hls-proxy.py line 22). Pin it so an inherited variable can't open a second unauthenticated relay.
        env["HLS_PROXY_BIND"] = "127.0.0.1"
        env["HLS_PROXY_PORT"] = str(port)
        env["CHANNELS_CONF"] = str(conf_file)
        if candidate.referer:
            env["HLS_PROXY_REFERER"] = candidate.referer

        self._log(f"[Fallback Mode] Launching hls-proxy on port {port} for {candidate.name}...")
        self._proxy_stderr_tail: "collections.deque" = collections.deque()
        try:
            self._proxy_process = subprocess.Popen(
                [sys.executable, self.hls_proxy_path],
                env=env,
                # Nothing ever read these pipes. Once the proxy had written
                # 64KB of its own logging the pipe filled, the proxy blocked
                # writing to it, and the fallback stream wedged with no error
                # -- the same defect that stopped FFmpeg dead at ~7 minutes.
                # stdout is discarded; stderr is drained to a bounded tail so a
                # proxy that fails to bind can still say why.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            self._proxy_stderr_tail = self._drain_stderr(self._proxy_process)
            time.sleep(1.5)
            if self._proxy_process.poll() is not None:
                detail = "; ".join(
                    line for _, line in self._proxy_stderr_tail) or "no output"
                self._log(
                    f"hls-proxy exited immediately (code "
                    f"{self._proxy_process.returncode}): {detail}", "ERROR")
                self._proxy_process = None
                return candidate.m3u8_url
            candidate.used_proxy = True
            return f"http://127.0.0.1:{port}/channel/{candidate.slug}"
        except Exception as e:
            self._log(f"Failed to start hls-proxy: {e}", "ERROR")
            return candidate.m3u8_url

    def _remove_proxy_conf(self):
        """Delete this session's channels.conf.

        It holds the fully tokenised stream URL and lives on the mounted
        recordings volume. Nothing removed it, so every session that ever fell
        back to the proxy left a readable credential behind indefinitely.
        """
        conf = getattr(self, "_proxy_conf_file", None)
        if not conf:
            return
        try:
            Path(conf).unlink(missing_ok=True)
        except OSError as exc:
            self._log(f"Could not remove proxy config: {exc}", "WARN")
        finally:
            self._proxy_conf_file = None

    @property
    def holds_proxy_port(self) -> bool:
        """True while an hls-proxy this session started may still be bound."""
        return self._proxy_process is not None

    def stop_proxy(self):
        """Terminate active hls-proxy subprocess.

        The reference is dropped only once the proxy is confirmed reaped. It
        used to be dropped unconditionally, so a proxy that survived both
        terminate() and kill() was forgotten while still bound, and its port
        was reported free to the next session.

        Works on a local reference: an operator stop now runs in a worker
        thread, concurrently with the recorder thread's own teardown.
        """
        proc = self._proxy_process
        if proc:
            exited = False
            try:
                proc.terminate()
                proc.wait(timeout=2)
                exited = True
            except Exception:
                try:
                    proc.kill()
                    # kill() only delivers SIGKILL. Without the wait() the
                    # dead child stays in the process table as a zombie, and a
                    # long session that fails over repeatedly accumulates one
                    # per switch. Same defect _reap_ffmpeg() documents.
                    proc.wait(timeout=2)
                    exited = True
                except Exception:
                    pass
            if exited:
                if self._proxy_process is proc:
                    self._proxy_process = None
            else:
                self._log(
                    f"hls-proxy (pid {getattr(proc, 'pid', '?')}) did not exit "
                    "after SIGKILL. Keeping its port block reserved rather than "
                    "handing it to another session.", "ERROR")
        self._remove_proxy_conf()

    def logs_since(self, seq: int) -> Tuple[List[str], int]:
        """Log lines added since sequence number `seq`, plus the new sequence.

        The live log view used to track a plain index into log_history. That
        list is trimmed to the newest LOG_HISTORY_LIMIT lines, so once a
        recording passed that many the length stopped growing, the "is there
        anything new" test could never be true again, and the log view sat
        silently frozen for the rest of the session. A monotonic sequence
        survives trimming. A reader that has fallen further behind than the
        buffer is deep gets what is still held rather than nothing.
        """
        with self._lock:
            total = self._log_seq
            history = list(self.log_history)
        if seq >= total:
            return [], total
        missed = total - seq
        if missed >= len(history):
            return history, total
        return history[len(history) - missed:], total

    def _reap_ffmpeg(self):
        """Terminate and reap the FFmpeg child.

        terminate() only delivers the signal. Without a wait() the exited
        child stays in the process table as a zombie until the Popen object
        happens to be collected, so a long recording that fails over
        repeatedly accumulates them.
        """
        proc = self._ffmpeg_process
        if not proc:
            return
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=2)
            except Exception:
                pass
        self._ffmpeg_process = None

    def start_recording(self):
        """Start recording thread."""
        if self.is_running:
            return
        self.is_running = True
        self.status = "recording"
        self.start_time = time.time()
        self.output_filepath.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._recording_loop, daemon=True)
        self._thread.start()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, name=f"pvarr-monitor-{self.recording_id}", daemon=True)
        self._monitor_thread.start()

    @property
    def has_next_candidate(self) -> bool:
        """Is there another candidate a failover could move to?

        Now that the list cycles, this is simply "more than one candidate":
        from the last one, the next is the first. It used to mean "not yet at
        the end of the list", which was right only while the walk was one-way.
        A single-URL session still has nowhere to go, and forcing a failover
        there would end the recording rather than switch it.
        """
        return len(self.candidates) > 1

    def switch_to_candidate(self, index: int) -> bool:
        """Manually move to a specific candidate, by 0-based index.

        Force-failover only ever means *next*. With the index moving in one
        direction there was no route back to the primary once it recovered,
        which is the common case: a token expires, the recorder moves to a
        backup, and the original is healthy again minutes later.
        """
        if not 0 <= index < len(self.candidates):
            self._log(f"Switch refused: no candidate {index + 1}.", "WARN")
            return False
        if index == self.current_candidate_index:
            self._log(
                f"Switch refused: already on {self.candidates[index].name}.", "WARN"
            )
            return False

        self._log(f"Manual switch to {self.candidates[index].name} requested.", "WARN")
        self._manual_target_index = index
        # Reuse the force-failover abort path: it stops the current attempt
        # without letting the proxy fallback run on the stream being left.
        self._force_failover_flag = True
        if self.is_running:
            self.status = "failing_over"
        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
            except Exception:
                pass
        return True

    def free_bytes(self) -> Optional[int]:
        """Free space on the volume the recording is being written to."""
        try:
            return shutil.disk_usage(self.output_filepath.parent).free
        except OSError:
            return None

    def _output_ok(self, sink) -> bool:
        """Rate-limited check that our bytes are still landing at the path.

        An append handle keeps working after its file is deleted: writes
        succeed, nothing raises, and the data goes to an unnamed inode that is
        freed when the handle closes. Neither the freeze detector nor the size
        readout can see this -- the freeze detector watches successful writes,
        which these are, and the size readout stats the path, which reports 0.
        A recording was lost to exactly that combination.

        Returns False when the recording should stop.
        """
        now = time.time()
        if now - self._last_output_check < self.OUTPUT_CHECK_INTERVAL_SEC:
            return True
        self._last_output_check = now

        try:
            if sink.is_intact():
                return True
        except Exception:
            return True  # never let a diagnostic take a recording down

        self._output_reopens += 1
        if self._output_reopens > self.MAX_OUTPUT_REOPENS:
            self._log(
                f"Output file {self.output_filepath.name} has been removed "
                f"{self._output_reopens} times. Something outside PVArr keeps "
                "deleting it; stopping rather than writing into a hole.",
                "ERROR",
            )
            self.status = "aborted_output_lost"
            return False

        self._log(
            f"Output file {self.output_filepath.name} vanished from under an "
            f"open handle after {self.bytes_written} bytes -- deleted by "
            "something outside this recording. Recreating it and continuing; "
            "footage written since it was removed is not recoverable.",
            "ERROR",
        )
        try:
            sink.reopen()
        except OSError as exc:
            self._log(f"Could not recreate {self.output_filepath}: {exc}", "ERROR")
            self.status = "aborted_output_lost"
            return False
        return True

    def _disk_space_ok(self) -> bool:
        """Rate-limited free-space check. False means stop recording.

        A failover cannot help here -- the problem is local -- so a breach ends
        the recording rather than moving to the next candidate. What has been
        captured is kept and post-processed, exactly as an operator stop would.
        """
        if self.min_free_bytes <= 0:
            return True
        now = time.time()
        if now - self._last_disk_check < self.DISK_CHECK_INTERVAL_SEC:
            return True
        self._last_disk_check = now

        free = self.free_bytes()
        if free is None or free >= self.min_free_bytes:
            return True

        self._log(
            f"Only {free / 1024 ** 3:.2f} GB free on "
            f"{self.output_filepath.parent} -- below the "
            f"{self.min_free_bytes / 1024 ** 3:.2f} GB floor. Aborting to keep "
            "the host usable; the footage recorded so far is kept.",
            "ERROR",
        )
        self.status = "aborted_no_space"
        self._stop_event.set()
        return False

    def set_end_time(self, end_time: float) -> None:
        """Move this recording's end (the dashboard's +15m / Edit buttons).

        One float assignment: the capture thread reads it through deadline()
        on every write-path check and failover lap, so no lock is needed.
        Logged as minutes from now, not a clock time -- the container's
        timezone is usually UTC and would only confuse.
        """
        self.end_time = float(end_time)
        self._log(f"End time moved by the operator: now stops in "
                  f"{max(0.0, self.end_time - time.time()) / 60:.0f} min.", "WARN")

    def deadline(self) -> Optional[float]:
        """When this recording must stop, as an absolute epoch time.

        An explicit end time wins outright. Otherwise the global backstop
        applies, measured from `start_time` -- the *original* one, carried
        across a resume in the session record. Measuring it from "now" would
        hand every restart a fresh 6 hours, so a crash-looping recording could
        run indefinitely, which is the exact thing the backstop exists to stop.

        None means no deadline: an explicit 0 duration, or the backstop
        disabled with PVARR_MAX_HOURS=0.
        """
        if self.end_time:
            return self.end_time
        # The backstop is a *recording* limit. A rebroadcast channel is meant to
        # sit there indefinitely -- the sponsor starts one and expects it to
        # still be in Plex tomorrow -- and it writes into a fixed-size ring, so
        # none of the reasoning behind the backstop (an unbounded file eating
        # the volume) applies to it. Applying it anyway would silently kill
        # every live channel at the 6 hour mark. An explicit end time is still
        # honoured: that is someone deliberately asking for a finite channel.
        if self.is_rebroadcast:
            return None
        if not self.max_hours or not self.start_time:
            return None
        return self.start_time + (self.max_hours * 3600.0)

    def seconds_remaining(self) -> Optional[float]:
        """Time left before the deadline, or None if there is not one."""
        deadline = self.deadline()
        if deadline is None:
            return None
        return deadline - time.time()

    def _within_window(self) -> bool:
        """True while a deadline exists and has not yet passed.

        A recording with no deadline is deliberately *not* "within a window" --
        it has no window, so the retry-forever rule below must not apply to it.
        """
        remaining = self.seconds_remaining()
        return remaining is not None and remaining > 0

    def _deadline_ok(self, capturing: bool = False) -> bool:
        """False once the recording has run to its scheduled end.

        Checked on the write path beside the disk and output guards, and again
        at the top of each failover lap so a deadline reached during a backoff
        is not sat through. Ends the recording the same way an operator stop
        does: it post-processes and is announced normally.

        `capturing` says whether bytes were flowing when the deadline arrived,
        and it decides what the recording is *called*. Reaching the end of the
        window mid-capture is a complete recording. Reaching it after the
        stream died twenty minutes in and never came back is a truncated one,
        and calling that "finished on schedule" would hide a short file behind
        a reassuring word -- the same failure as reporting "completed" while a
        remux was still running.
        """
        remaining = self.seconds_remaining()
        if remaining is None or remaining > 0:
            return True

        if capturing or self.cycles_without_data == 0:
            self._log(
                f"Reached the scheduled end of this recording "
                f"({self._deadline_reason()}). Stopping cleanly and "
                "post-processing what was captured.",
            )
            self.status = "completed_window"
        elif self.bytes_written > 0:
            self._log(
                f"Reached the scheduled end of this recording "
                f"({self._deadline_reason()}), but no candidate has delivered "
                f"data for {self.cycles_without_data} full attempts. Keeping "
                f"the {self.bytes_written} bytes already recorded.", "WARN",
            )
            self.status = "completed_partial"
        else:
            self._log(
                f"Reached the scheduled end of this recording "
                f"({self._deadline_reason()}) without capturing anything.",
                "ERROR",
            )
            self.status = "failed"
        self._stop_event.set()
        return False

    def _deadline_reason(self) -> str:
        """Which limit is doing the stopping -- worth saying, since one is a
        deliberate choice and the other is a default the operator may not know
        about."""
        if self.end_time:
            return "requested end time"
        return f"PVARR_MAX_HOURS backstop, {self.max_hours:g}h"

    def _failover_delay(self, wrapped: bool) -> float:
        """Pause before the next attempt.

        Within a lap this is the original short breath. After a whole lap that
        produced nothing, back off so a set of genuinely dead origins is not
        hammered in a tight loop: 5s, 10s, 20s, capped at 60s.
        """
        if not wrapped:
            return 1.0
        return min(5.0 * (2 ** max(0, self.cycles_without_data - 1)), 60.0)

    # -- monitor: standby checks, stream rate, loss alert ------------------

    STANDBY_INTERVAL_SEC = 90.0
    MONITOR_TICK_SEC = 10.0
    LOSS_BURST_COUNT = 3
    LOSS_BURST_WINDOW_SEC = 300.0
    ALERT_INTERVAL_SEC = 900.0

    def standby_state(self, candidate: CandidateStream, now: Optional[float] = None) -> str:
        """A backup's last standby result, or "" once it is too old to trust
        (three missed intervals) or was never taken."""
        now = time.time() if now is None else now
        if not candidate.standby or now - candidate.standby_at > 3 * self.STANDBY_INTERVAL_SEC:
            return ""
        return candidate.standby

    def _next_candidate_index(self) -> int:
        """The next candidate in cycle order that a standby check has not seen
        dead (or on the wrong game). If every other one has, plain next: never
        worse than before the checks existed."""
        n = len(self.candidates)
        here = self.current_candidate_index
        for step in range(1, n):
            i = (here + step) % n
            if self.standby_state(self.candidates[i]) not in ("dead", "wrong_game"):
                return i
        return (here + 1) % n

    def check_standby(self, index: int) -> str:
        """Probe one backup without touching it: the probe runs on a copy, so
        the recording loop's candidate (which it may enter at any moment) never
        sees half-written fields. Light: no segment download, no yt-dlp.
        Blocking network I/O, so only ever called from the monitor thread."""
        live = self.candidates[index]
        copy = CandidateStream(live.url, name=live.name)
        copy.referer, copy.cookie, copy.user_agent = live.referer, live.cookie, live.user_agent
        try:
            result = probe_stream(copy.url, referer=copy.referer or None,
                                  user_agent=copy.user_agent, cookie=copy.cookie or None,
                                  check_segment=False, use_ytdlp=False)
        except Exception:
            result = {"ok": False}
        if not result.get("ok"):
            state = "dead"
        else:
            copy.m3u8_url = result.get("m3u8_url") or ""
            state = "wrong_game" if self.is_wrong_source(copy) else "alive"
            # Feeds the step-down's choice of a lighter backup (no download).
            levels = usable_variants(copy.m3u8_url, result.get("variants") or [])
            live.declared_kbps = levels[0]["kbps"] if levels else None
        before = self.standby_state(live)
        if copy.m3u8_url:
            live.standby_m3u8 = copy.m3u8_url
        live.standby, live.standby_at = state, time.time()
        if before and before != state:
            word = {"alive": "is answering again", "dead": "is not answering",
                    "wrong_game": "plays the stream marked as the wrong game"}[state]
            self._log(f"Standby check: {live.name} {word}.", "WARN" if state != "alive" else "INFO")
        return state

    def rate_mbps(self) -> Optional[float]:
        """Stream rate over the monitor's samples (~60 s), in Mbit/s."""
        samples = list(self._rate_samples)
        if len(samples) < 2 or samples[-1][0] <= samples[0][0]:
            return None
        (t0, b0), (t1, b1) = samples[0], samples[-1]
        return round(max(0, b1 - b0) * 8 / (t1 - t0) / 1e6, 1)

    def _check_loss_burst(self, now: float) -> None:
        recent = sum(1 for t in self._loss_times if now - t <= self.LOSS_BURST_WINDOW_SEC)
        if recent < self.LOSS_BURST_COUNT or now - self._last_alert_at < self.ALERT_INTERVAL_SEC:
            return
        self._last_alert_at = now
        rate = self.rate_mbps()
        need = f" This stream needs about {rate} Mbps;" if rate else ""
        body = (f"{recent} stream segments lost in the last "
                f"{self.LOSS_BURST_WINDOW_SEC / 60:.0f} min.{need} check what else is using "
                "your connection.")
        self._log(f"Losing segments: {body}", "WARN")
        if self.on_alert_callback:
            try:
                self.on_alert_callback(self.recording_id, "PVArr — Losing stream segments", body)
            except Exception as exc:
                logger.warning("Alert callback failed for %s: %s", self.recording_id, exc)

    # -- step-down on a slow connection ------------------------------------
    #
    # Records the best quality, and drops to a lighter stream only when the
    # connection (not the source) is losing segments: a lower level of the same
    # playlist, else a backup that is a different, clearly lighter feed. Each
    # step restarts FFmpeg, a gap of a few seconds like a failover, so the
    # trigger needs sustained loss and stepping back up waits for a long quiet
    # spell. A step-up that loses again soon stays down for good.

    STEP_DOWN_EVENTS = 4
    STEP_WINDOW_SEC = 300.0
    STEP_UP_QUIET_SEC = 1200.0
    RELAPSE_SEC = 600.0
    MAX_STEP_DOWNS = 3
    LIGHTER_RATIO = 0.75

    def _capped_level(self, candidate: CandidateStream) -> Optional[Dict[str, Any]]:
        """The level to record under the candidate's ceiling, or None for the
        playlist as probed (no ceiling, a single level, or none fits)."""
        if candidate.max_kbps is None:
            return None
        levels = usable_variants(candidate.m3u8_url, candidate.variants)
        if len(levels) < 2:
            return None
        fit = [v for v in levels if v["kbps"] <= candidate.max_kbps]
        if not fit or fit[0]["kbps"] == levels[0]["kbps"]:
            return None
        return fit[0]

    def _current_kbps(self, candidate: CandidateStream) -> Optional[float]:
        level = self._capped_level(candidate)
        if level:
            return level["kbps"]
        if candidate.declared_kbps:
            return candidate.declared_kbps
        rate = self.rate_mbps()
        return rate * 1000 if rate else None

    def _lighter_option(self) -> Optional[Tuple[int, Optional[int], str]]:
        """(candidate index, ceiling, description) one step lighter than now."""
        here = self.current_candidate_index
        cur = self.candidates[here]
        levels = usable_variants(cur.m3u8_url, cur.variants)
        now_kbps = self._current_kbps(cur)
        if len(levels) > 1 and now_kbps:
            lower = [v for v in levels if v["kbps"] < now_kbps]
            if lower:
                v = lower[0]
                return here, v["kbps"], describe_level(v["kbps"], v["height"])
        if not now_kbps:
            return None
        key = self._candidate_key(cur)
        best = None
        for i, c in enumerate(self.candidates):
            if i == here or self.standby_state(c) != "alive" or not c.declared_kbps:
                continue
            other = quality.source_key(c.standby_m3u8) if c.standby_m3u8 else ""
            if not other or other == key or c.declared_kbps > self.LIGHTER_RATIO * now_kbps:
                continue
            if best is None or c.declared_kbps > self.candidates[best].declared_kbps:
                best = i
        if best is None:
            return None
        c = self.candidates[best]
        return best, None, f"{c.name} ({describe_level(c.declared_kbps)})"

    def _request_step(self, target: int, cap: Optional[int], now: float) -> None:
        """Hand the recording loop a step and end the current attempt."""
        here = self.current_candidate_index
        if target == here:
            self.candidates[here].max_kbps = cap
        self._step_request = (here, target, cap)
        self._step_at = now
        self._force_failover_flag = True
        proc = self._ffmpeg_process
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass

    def _step_dropped(self, step: Tuple[int, int, Optional[int]]) -> None:
        """The loop discarded a step (a failover or the operator got there
        first). Undo its bookkeeping so it does not use up a step-down."""
        here, target, _cap = step
        if self._stepped_to is not None:      # it was a step down
            self._step_downs = max(0, self._step_downs - 1)
            if target == here:
                self.candidates[here].max_kbps = None
        self._stepped_from = self._stepped_to = None

    def _alert(self, title: str, body: str) -> None:
        if self.on_alert_callback:
            try:
                self.on_alert_callback(self.recording_id, title, body)
            except Exception as exc:
                logger.warning("Alert callback failed for %s: %s", self.recording_id, exc)

    def _check_step(self, now: float) -> None:
        """Step down on sustained slow-connection loss; back up after quiet."""
        if self.status != "recording" or self._step_request is not None:
            return
        since = max(self._step_at, now - self.STEP_WINDOW_SEC)
        slow = sum(1 for t in self._slow_events if t > since)
        here = self.current_candidate_index
        # A failover moved us off the stepped-down stream: that step is over.
        if self._stepped_to is not None and here != self._stepped_to:
            self._stepped_from = self._stepped_to = None

        if slow >= self.STEP_DOWN_EVENTS:
            if self._step_downs >= self.MAX_STEP_DOWNS:
                return
            option = self._lighter_option()
            if option is None:
                if not self._no_lighter_logged:
                    self._no_lighter_logged = True
                    body = (f"Slow connection: {slow} segment losses in "
                            f"{self.STEP_WINDOW_SEC / 60:.0f} min were timeouts, not the "
                            "source. Nothing lighter to switch to; recording continues "
                            "at the current quality.")
                    self._log(body, "WARN")
                    self._alert("PVArr — Slow connection", body)
                return
            target, cap, what = option
            if self._step_up_at and now - self._step_up_at < self.RELAPSE_SEC:
                self._stay_down = True
            if self._stepped_from is None:
                self._stepped_from = here
            self._stepped_to = target
            self._step_downs += 1
            body = (f"Slow connection: {slow} segment losses in "
                    f"{self.STEP_WINDOW_SEC / 60:.0f} min were timeouts, not the source. "
                    f"Switching to {what}; a few seconds are lost at the switch. "
                    + ("Staying there for the rest of the recording (it lost segments "
                       "again soon after going back up)." if self._stay_down else
                       f"Back to the best quality after {self.STEP_UP_QUIET_SEC / 60:.0f} "
                       "quiet minutes."))
            self._log(body, "WARN")
            self._alert("PVArr — Stepped down to a lighter stream", body)
            self._request_step(target, cap, now)
            return

        # Back up: only from where the step-down put us, after a quiet spell.
        if (self._stepped_from is None or self._stay_down
                or here != self._stepped_to
                or now - self._step_at < self.STEP_UP_QUIET_SEC
                or any(t > now - self.STEP_UP_QUIET_SEC for t in self._slow_events)):
            return
        back = self._stepped_from
        self._stepped_from = self._stepped_to = None
        self._step_up_at = now
        self.candidates[back].max_kbps = None
        self._log(f"Connection quiet for {self.STEP_UP_QUIET_SEC / 60:.0f} min: going back "
                  f"to the best quality on {self.candidates[back].name}.")
        self._request_step(back, None, now)

    def _monitor_loop(self) -> None:
        """Side thread for the life of the recording: samples the write rate,
        watches for bursts of lost segments, and probes the backups now and
        then so a failover goes to one that answers. Starts no processes."""
        next_standby = time.time() + self.STANDBY_INTERVAL_SEC / 3
        failed_logged = False
        while self.is_running and not self._stop_event.wait(self.MONITOR_TICK_SEC):
            # A fault here must not end the thread quietly: the rate, the
            # alerts and the standby checks would all stop with no trace.
            try:
                now = time.time()
                self._rate_samples.append((now, self.bytes_written))
                self._check_loss_burst(now)
                # Before the standby checks, which block on the network: it
                # uses their cached results.
                self._check_step(now)
                if len(self.candidates) > 1 and now >= next_standby:
                    next_standby = now + self.STANDBY_INTERVAL_SEC
                    for i in range(len(self.candidates)):
                        if not self.is_running:
                            return
                        c = self.candidates[i]
                        # A backup that was dead is re-checked every other
                        # round: a dead page is not fetched every 90 s all game.
                        if (i == self.current_candidate_index
                                or (c.standby == "dead"
                                    and now - c.standby_at < 2 * self.STANDBY_INTERVAL_SEC)):
                            continue
                        self.check_standby(i)
            except Exception as exc:
                if not failed_logged:
                    failed_logged = True
                    logger.warning("[%s] Monitor tick failed (it keeps running): %s",
                                   self.recording_id, exc)

    @staticmethod
    def _candidate_key(candidate: CandidateStream) -> str:
        """The feed a candidate plays (quality.source_key); "" before its
        playlist is known. A raw playlist URL is its own playlist."""
        return quality.source_key(candidate.m3u8_url or (
            candidate.url if ".m3u8" in candidate.url.lower() else ""))

    def is_wrong_source(self, candidate: CandidateStream) -> bool:
        key = self._candidate_key(candidate)
        return bool(key) and key in self.wrong_sources

    def mark_wrong_game(self) -> Optional[str]:
        """The operator says the stream being recorded is the wrong game.

        Marks its feed, counts every candidate on that feed as failed (so the
        event-page re-check refills those slots) and leaves it now, through
        the force-failover abort path. Unlike force_failover() a single-URL
        session is not refused: recording nothing beats recording the wrong
        game, and the loop keeps retrying (and re-checking) until the window
        closes. Returns the key, or None when the playlist is not known yet.
        """
        if not 0 <= self.current_candidate_index < len(self.candidates):
            return None
        current = self.candidates[self.current_candidate_index]
        key = self._candidate_key(current)
        if not key:
            self._log("Wrong-game mark refused: this stream's playlist is not known yet.", "WARN")
            return None
        self.wrong_sources.add(key)
        same = [c for c in self.candidates if self._candidate_key(c) == key]
        for c in same:
            c.fail_count += 1
        names = ", ".join(c.name for c in same)
        self._log(f"Marked as the wrong game: {names}. That stream will not be used again "
                  "in this recording, under any link.", "WARN")
        self._force_failover_flag = True
        if self.is_running:
            self.status = "failing_over"
        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
            except Exception:
                pass
        return key

    def force_failover(self) -> bool:
        """Manual trigger to force switch to the next stream candidate.

        Refused when no backup remains. Advancing past the last candidate ends
        the recording -- with a single URL the button silently killed a live
        capture and the API still answered "success", which is the opposite of
        what "fail over to the backup" promises.
        """
        if not self.has_next_candidate:
            self._log("Force-failover refused: no backup candidate remains.", "WARN")
            return False

        self._log("Manual force-failover requested!", "WARN")
        self._force_failover_flag = True
        # Reflect the request in the status right away. The loop sets
        # "failing_over" itself, but only after the current attempt unwinds,
        # and holds it for about a second -- invisible to a 3s dashboard poll,
        # so the operator saw nothing happen. Guarded on is_running so a
        # session finishing at this instant cannot latch the status.
        if self.is_running:
            self.status = "failing_over"
        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
            except Exception:
                pass
        return True

    def stop(self, reason: str = "operator"):
        """Gracefully stop recording.

        `reason` matters, and conflating the two cases is why a restart used to
        lose a recording. An **operator** stop means the recording is finished:
        mark it completed and let the session state be forgotten. A
        **shutdown** stop means the process is going away with the recording
        still wanted -- the status must not claim "completed", or the persisted
        state says there is nothing to come back to and the resume never
        happens.
        """
        self.stop_reason = reason
        self._log(f"Stopping PVArr recorder gracefully ({reason})...")
        self._stop_event.set()
        self.is_running = False
        self.status = "completed" if reason == "operator" else "interrupted"
        self.stop_time = time.time()

        self._reap_ffmpeg()
        self.stop_proxy()

    def wait_until_finished(self, timeout: Optional[float] = None) -> bool:
        """Block until the recorder thread has finished its completion work.

        stop() only *asks* the thread to stop. The completion block -- remux,
        final_filepath, notification -- runs afterwards on that thread, and
        nothing used to wait for it. Since the thread is a daemon, an
        interpreter exit killed it mid-remux, which is why every container stop
        left an un-remuxed .ts behind.

        Returns True if the thread finished within the timeout.
        """
        thread = self._thread
        if thread is None or not thread.is_alive():
            return True
        thread.join(timeout)
        return not thread.is_alive()

    def _build_ffmpeg_cmd(
        self, stream_url: str, referer: str = "", user_agent: str = "",
        cookie: str = "", local_proxy: bool = False
    ) -> List[str]:
        """Build FFmpeg command line with custom HTTP headers if present.

        `local_proxy` marks the fallback path, where the playlist comes from our
        own hls-proxy on 127.0.0.1 rather than from the internet.
        """
        cmd = [
            self.ffmpeg_path or "ffmpeg",
            # FFmpeg's periodic progress line is ~124 bytes/sec on stderr. That
            # pipe is 64KB and only drained on failure, so at the default log
            # level it filled in well under ten minutes, at which point FFmpeg
            # blocked writing to it and stopped producing video entirely --
            # every recording longer than that wedged. Errors still come
            # through; only the stats spam is suppressed.
            "-hide_banner",
            "-nostats",
            # Warning, not error: the HLS demuxer reports a lost segment ("Failed
            # to open segment", "skipping N segments ahead") only at warning
            # level, so at "error" whole segments vanished from recordings
            # without a trace. "level" tags every line with its severity, which
            # is how _drain_stderr() keeps warnings apart from real errors.
            # "repeat" stops FFmpeg collapsing identical lines into an untagged
            # "Last message repeated N times", which would hide repeated
            # expiries from the count and push real errors out of the tail.
            "-loglevel", "repeat+level+warning",
            # Belt and braces with safe_stream_url(): even if a non-http URL
            # reached here, FFmpeg is not permitted to open a local file or an
            # arbitrary socket. 'file' is deliberately absent. crypto and data
            # are required for AES-128 encrypted HLS, which is common.
            "-protocol_whitelist", "http,https,tcp,tls,crypto,data",
            "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
        ]

        # Direct mode needs this as much as the proxy path: a live origin
        # served real MPEG-TS under ".image" URLs, and direct mode refused every
        # segment. The protocol whitelist above, not the extension list, is
        # what keeps FFmpeg off local files.
        cmd.extend(hls_extension_flags(self.ffmpeg_path))

        # Construct HTTP headers for Direct Mode. Each value is checked because
        # FFmpeg copies this block verbatim into the request; see
        # safe_header_value().
        # Origin goes with Referer because the probe always verifies the two
        # together (probe._header_attempts), and a browser's hls.js sends both
        # on every segment fetch. Sending only the Referer meant FFmpeg asked
        # for segments with less than the probe had proven was accepted.
        origin = origin_of(referer)
        headers_str = ""
        for name, raw in (("User-Agent", user_agent), ("Referer", referer),
                          ("Origin", origin), ("Cookie", cookie)):
            if not raw:
                continue
            checked = safe_header_value(raw)
            if checked is None:
                self._log(
                    f"Dropping {name}: it contains a line break or NUL, which "
                    "would inject additional HTTP headers.", "ERROR",
                )
                continue
            headers_str += f"{name}: {checked}\r\n"

        if headers_str:
            cmd.extend(["-headers", headers_str])

        cmd.extend([
            "-i", stream_url,
            "-c", "copy",
        ])

        # Continue the timeline instead of restarting it. FFmpeg normalises
        # every input to its own zero, so without this each failover segment
        # would be appended starting at ~1.42s again and the file would tell a
        # player that time had run backwards. Empty for the first segment,
        # which has nothing to continue from.
        cmd.extend(output_ts_offset_flags(self._timeline_offset))

        cmd.extend([
            "-f", "mpegts",
            "pipe:1"
        ])

        return cmd

    def _stream_ffmpeg_process(
        self, ffmpeg_cmd: List[str], candidate: CandidateStream
    ) -> "StreamOutcome":
        """Stream FFmpeg stdout to the output file and report how the attempt ended.

        The timeline bookkeeping lives out here, in a `finally`, because the
        capture body returns from half a dozen places -- forced failover, a
        freeze, a non-zero exit, a clean exit -- and all but one of them are
        followed by another FFmpeg appending to the same file. Advancing the
        offset "at the end of" the body would therefore run only on the
        operator-stop path: the single case where no next segment exists and
        the offset is not needed. Every path that matters would skip it and the
        discontinuity would survive the fix.
        """
        tail = _TailBuffer()
        try:
            return self._capture_ffmpeg_output(ffmpeg_cmd, candidate, tail)
        finally:
            self._advance_timeline(tail)
            self._flush_segment_loss()

    def _advance_timeline(self, tail: "_TailBuffer") -> None:
        """Move `_timeline_offset` to the end of the segment just captured.

        An empty tail means the attempt never delivered a byte, so there is
        nothing appended to the file and nothing to move past -- and a stream
        that fails instantly can cycle candidates quickly, so skipping the scan
        there also keeps a failure storm from paying for it.

        Everything in here is best-effort. It runs on the capture thread, where
        an unhandled exception would kill a recording that is otherwise
        healthy, and the worst a failure costs is one uncorrected splice.
        """
        if not tail:
            return
        try:
            observed = last_timeline_position(tail.bytes())
            if observed is None:
                return
            self._timeline_offset = advance_timeline_position(
                self._timeline_offset, observed
            )
        except Exception as exc:  # noqa: BLE001 - never lose a recording to this
            self._log(f"Could not read the output timeline: {exc}", "WARN")

    def _capture_ffmpeg_output(
        self, ffmpeg_cmd: List[str], candidate: CandidateStream,
        tail: "_TailBuffer",
    ) -> "StreamOutcome":
        """Stream FFmpeg stdout to the output file and report how the attempt ended."""
        written_for_this_session = 0

        with self._open_sink() as out_f:
            self._ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            stdout_fd = self._ffmpeg_process.stdout.fileno()
            stderr_tail = self._drain_stderr(self._ffmpeg_process, ffmpeg=True)
            at_eof = False
            # Started here, not before Popen: spawning the process is this
            # recorder's own overhead and has no business inside a budget that
            # exists to judge the source.
            last_write_time = time.time()

            def finish(outcome: "StreamOutcome") -> "StreamOutcome":
                """Attach FFmpeg's own words to a failed attempt."""
                if outcome is not StreamOutcome.COMPLETED and stderr_tail:
                    # Stored as logged (redacted), since last_error is served
                    # by /api/status.
                    detail = self._describe_stderr_tail(stderr_tail, time.time())
                    candidate.last_error = detail
                    self._log(f"FFmpeg said: {detail}", "ERROR")
                return outcome

            while not self._stop_event.is_set():
                if self._force_failover_flag:
                    self._log(f"Forced failover triggered on {candidate.name}", "WARN")
                    return StreamOutcome.INTERRUPTED

                # A blocking read(32768) parked this loop inside the kernel
                # until a full 32KB had arrived, so a source that stalled
                # mid-buffer was never noticed: the freeze timeout below could
                # not be reached, a stop or force-failover was not seen until
                # the pipe closed, and bytes_written advanced in 32KB steps so
                # the dashboard read 0.00 MB for the first seconds of a
                # low-bitrate stream. select() bounds the wait; os.read then
                # takes whatever has actually arrived.
                chunk = b""
                if not at_eof:
                    try:
                        ready, _, _ = select.select(
                            [stdout_fd], [], [], self.READ_POLL_SEC
                        )
                    except (OSError, ValueError):
                        ready = []
                        at_eof = True
                    if ready:
                        try:
                            chunk = os.read(stdout_fd, self.READ_CHUNK_BYTES)
                        except OSError:
                            chunk = b""
                        if not chunk:
                            at_eof = True  # FFmpeg closed the pipe

                if chunk:
                    out_f.write(chunk)
                    out_f.flush()
                    tail.append(chunk)
                    len_chunk = len(chunk)
                    self.bytes_written += len_chunk
                    written_for_this_session += len_chunk
                    last_write_time = time.time()
                    # Checked on the write path, where the space is actually
                    # being consumed. Rate-limited internally.
                    if not self._disk_space_ok():
                        break
                    if not self._output_ok(out_f):
                        break
                    # Checked here rather than only between candidates: a
                    # healthy stream never leaves this loop, so a deadline
                    # tested only on failover would never fire on exactly the
                    # long unattended capture the backstop is for.
                    if not self._deadline_ok(capturing=True):
                        break
                    continue

                ret_code = self._ffmpeg_process.poll()
                if ret_code is not None:
                    if written_for_this_session == 0:
                        return finish(StreamOutcome.FAILED)
                    if ret_code == 0:
                        return StreamOutcome.COMPLETED
                    # Non-zero exit after delivering data: FFmpeg died partway
                    # through. Previously indistinguishable from a clean finish.
                    self._log(
                        f"FFmpeg exited {ret_code} after {written_for_this_session} bytes "
                        f"on {candidate.name}; treating as interrupted", "ERROR"
                    )
                    candidate.fail_count += 1
                    return finish(StreamOutcome.INTERRUPTED)

                # Two different failures share this one check. Before the first
                # byte the question is "did this source ever start?", which is
                # allowed the startup grace; after it, "has this source gone
                # quiet?", which is the freeze timeout the operator configured.
                # The grace never applies to a stream that has already
                # delivered, so a mid-recording stall is still caught as fast as
                # it always was.
                started = written_for_this_session > 0
                idle_budget = (
                    self.freeze_timeout_sec if started
                    else max(self.freeze_timeout_sec, self.STARTUP_GRACE_SEC)
                )
                if (time.time() - last_write_time) > idle_budget:
                    candidate.fail_count += 1
                    if not started:
                        self._log(
                            f"No data from {candidate.name} in the first "
                            f"{idle_budget}s; giving up on this attempt", "ERROR")
                        return finish(StreamOutcome.FAILED)
                    self._log(f"Stream freeze detected! No data received for {idle_budget}s", "ERROR")
                    return finish(StreamOutcome.INTERRUPTED)

                if at_eof:
                    # Pipe closed but the exit status has not landed yet. Short
                    # sleep so this does not spin; the freeze timeout above is
                    # the backstop if FFmpeg never reaps.
                    time.sleep(0.05)

        # Loop exited because stop() was requested -- an operator stop is a
        # clean end, not a failure.
        return (
            StreamOutcome.COMPLETED
            if written_for_this_session > 0
            else StreamOutcome.FAILED
        )

    @property
    def is_rebroadcast(self) -> bool:
        """True when this session streams without keeping anything."""
        return self.ring is not None

    def _open_sink(self):
        """Where captured bytes go: a growing file, or a bounded ring.

        Append mode for a recording is what makes failover invisible -- every
        attempt continues the same file. The ring is the same idea with a
        ceiling.
        """
        if self.ring is not None:
            return _RingSink(self.ring)
        return _FileSink(self.output_filepath)

    @classmethod
    def _describe_stderr_tail(cls, tail: "collections.deque", now: float) -> str:
        """The stderr tail as one line, limited to what is near the failure.

        Lines from the last STDERR_RECENT_SEC are joined. If there are none,
        the newest line is shown with its age, so an old message is not read
        as the cause. Redacted per line *before* the 500-char cut: truncating
        first can shorten a path token until it no longer looks like one.
        """
        entries: List[Tuple[float, str]] = list(tail)  # the pump may be appending
        if not entries:
            return ""
        recent = [line for ts, line in entries if now - ts <= cls.STDERR_RECENT_SEC]
        if recent:
            return " | ".join(redact_url_secrets(l) for l in recent)[:500]
        ts, line = entries[-1]
        # Seconds under a minute: "0 min earlier" would read as "just now".
        secs = max(0.0, now - ts)
        when = f"{secs:.0f} s" if secs < 59.5 else f"{secs / 60:.0f} min"
        return (f"(from {when} earlier) " + redact_url_secrets(line))[:500]

    def _drain_stderr(self, proc: subprocess.Popen, ffmpeg: bool = False) -> "collections.deque":
        """Continuously drain a child's stderr, keeping only the tail.

        Two jobs. The pipe must be read or FFmpeg eventually blocks writing to
        it and stops producing video -- that is a hang, not a stream fault, and
        no amount of failover logic can recover from it. And when an attempt
        does fail, FFmpeg's last few lines are usually the only explanation of
        why (403, 404, bad codec), which previously went nowhere.

        With `ffmpeg=True` each line carries FFmpeg's severity tag (see
        _build_ffmpeg_cmd). Warnings are counted for lost segments and kept out
        of the tail, so a burst of "Packet corrupt" cannot push the line that
        explains a failure out of a 15-line buffer -- the tail holds what it
        held when FFmpeg ran at "error". hls-proxy has no tags; kept whole.

        Each entry is `(time received, line)`, so a failure can tell lines
        near it from lines left over from FFmpeg's start-up.

        Daemon thread, bounded buffer: it holds at most STDERR_TAIL_LINES lines
        and exits on its own when the pipe closes.
        """
        tail: "collections.deque" = collections.deque(maxlen=self.STDERR_TAIL_LINES)
        stream = proc.stderr
        if stream is None:
            return tail
        seen_failures: "collections.OrderedDict" = collections.OrderedDict()
        # Reason for the next failed segment (see _SOURCE_REASON_RE). Held
        # here, per FFmpeg process, and cleared when a failure takes it.
        reason: Dict[str, str] = {}

        def pump():
            try:
                reader = stream
                if isinstance(stream, io.RawIOBase):
                    # bufsize=0 makes FFmpeg's stderr a raw FileIO, whose
                    # readline() costs one system call per byte: measured at
                    # 86 us a line against 1 us buffered. Harmless at "error",
                    # where lines were rare; not at "warning".
                    reader = io.BufferedReader(stream, buffer_size=65536)
                # Bounded: a line with no newline must not grow without limit.
                for raw in iter(lambda: reader.readline(4096), b""):
                    line = raw.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    if ffmpeg:
                        if _SOURCE_REASON_RE.search(line):
                            reason.update(why="source", at=str(time.time()))
                        elif _SLOW_REASON_RE.search(line):
                            reason.update(why="slow", at=str(time.time()))
                        level = _FFMPEG_LEVEL_RE.match(line)
                        if level and level.group(1) == "warning":
                            try:
                                self._account_ffmpeg_warning(line, seen_failures, reason)
                            except Exception:
                                # Counting is a diagnostic. Letting it raise
                                # would end this loop, stop draining the pipe,
                                # and hang FFmpeg -- the one failure this
                                # thread exists to prevent.
                                pass
                            continue
                    tail.append((time.time(), line))
            except Exception:
                pass  # pipe closed under us during shutdown; nothing to do

        threading.Thread(
            target=pump, name=f"pvarr-stderr-{self.recording_id}", daemon=True
        ).start()
        return tail

    @property
    def segments_lost(self) -> int:
        return self.segments_failed + self.segments_expired

    def _account_ffmpeg_warning(self, line: str, seen: "collections.OrderedDict",
                                reason: Optional[Dict[str, str]] = None) -> None:
        """Count a lost segment reported in one FFmpeg warning line.

        A failed segment is keyed by playlist and sequence number: FFmpeg 6.1
        logs "Failed to open segment" again on each retry, and one hole must
        count once. Expiry carries its own count. Anything else is ignored --
        "Packet corrupt" follows a lost segment and would count it twice.
        """
        failed = _SEGMENT_FAILED_RE.search(line)
        if failed:
            # The reason belongs to this line even when it is a retry of a
            # segment already counted: left in place it would be pinned on the
            # next, unrelated failure.
            why = "slow"
            if reason:
                if time.time() - float(reason.get("at") or 0) <= 30:
                    why = reason.get("why", "slow")
                reason.clear()
            if why == "source":
                self._source_fail_at = time.time()
            key = (failed.group(2), failed.group(1))
            if key in seen:
                return
            seen[key] = None
            if len(seen) > self._MAX_TRACKED_FAILURES:
                seen.popitem(last=False)
            self._note_segment_loss(failed=1, slow=(why == "slow"))
            return
        expired = _SEGMENTS_EXPIRED_RE.search(line)
        if expired:
            # FFmpeg fell behind the live window: the connection, unless it
            # was busy retrying a segment the source broke.
            slow = time.time() - self._source_fail_at > SOURCE_SHADOW_SEC
            self._note_segment_loss(
                expired=min(int(expired.group(1)), _MAX_EXPIRED_PER_LINE), slow=slow)

    def _note_segment_loss(self, failed: int = 0, expired: int = 0, slow: bool = False) -> None:
        """Count lost segments. Log the first at once, then at most once a minute.

        Called from the stderr pump thread. A source dropping one segment in
        three warns every few seconds for hours; a log line for each would bury
        everything else the dashboard's log shows.
        """
        with self._lock:
            self.segments_failed += failed
            self.segments_expired += expired
            self._loss_unlogged += failed + expired
            now = time.time()
            for _ in range(failed + expired):
                self._loss_times.append(now)
            if slow:
                self._slow_events.append(now)  # one per line: a freeze is one event
            if (self._loss_logged_at
                    and now - self._loss_logged_at < self.SEGMENT_LOSS_LOG_INTERVAL_SEC):
                return
            message = self._take_segment_loss_message(now)
        if message:
            self._log(message, "WARN")  # outside the lock: _log() takes it

    def _flush_segment_loss(self) -> None:
        """Log losses still held back by the once-a-minute limit."""
        with self._lock:
            message = self._take_segment_loss_message(time.time())
        if message:
            self._log(message, "WARN")

    def _take_segment_loss_message(self, now: float) -> Optional[str]:
        """The log line for losses not yet logged, resetting them. Holds _lock."""
        pending = self._loss_unlogged
        if not pending:
            return None
        first = not self._loss_logged_at
        self._loss_unlogged = 0
        self._loss_logged_at = now
        detail = (f"{self.segments_failed} failed to download, "
                  f"{self.segments_expired} expired before FFmpeg reached them")
        if first:
            return (f"Stream segments lost: {pending} ({detail}). Each is a gap of "
                    "a few seconds in the recording, picture and sound alike.")
        return (f"{pending} more stream segments lost -- "
                f"{self.segments_failed + self.segments_expired} in total ({detail}).")

    def _seed_timeline(self) -> None:
        """Pick the timeline up where a previous process left it, if there is one.

        A resumed session is a brand-new recorder object appending to a .ts an
        earlier process already wrote, so `_timeline_offset` would start at zero
        against a file whose timeline is hours in -- reintroducing, on every
        container restart, exactly the backward jump this machinery exists to
        prevent. The splice at a resume is no different from the splice at a
        failover; it just spans a process boundary.

        Rebroadcast is exempt: a ring holds only a moving window, is discarded
        when the process goes away, and has no prior timeline to rejoin. Its
        failover splices are still corrected -- the tail is captured from the
        bytes on their way to the sink, so it works the same for either one --
        only this start-up seeding does not apply.
        """
        if self._timeline_seeded:
            return
        self._timeline_seeded = True
        if self.is_rebroadcast:
            return
        observed = tail_timeline_position(self.output_filepath)
        if observed is None:
            return
        self._timeline_offset = observed
        self._log(
            f"Continuing the timeline of an existing recording at "
            f"{observed:.2f}s; appended segments will follow it."
        )

    def _recording_loop(self):
        """Main recording & failover loop.

        The candidate index used to only ever increment, so the list was a
        one-way walk: run off the end and the recording stopped, with no route
        back to candidate 1 even after it recovered. The most common failure
        here is an expiring token, which fixes itself in minutes -- so a blip
        that touched all three sources could end a three-hour capture while
        every one of them was healthy again. The index now wraps.
        """
        self._seed_timeline()

        while not self._stop_event.is_set():
            # A deadline can pass while the loop is in a failover backoff,
            # which is up to 60s of not writing anything -- so the write-path
            # check alone would overshoot, or miss it entirely if every
            # candidate is down at the moment the window closes.
            if not self._deadline_ok():
                break
            candidate = self.candidates[self.current_candidate_index]
            bytes_before = self.bytes_written
            # Clear any lingering "failing_over" from the previous iteration so
            # the dashboard shows the candidate we are actually recording.
            self.status = "recording"
            self._log(f"=== Active Stream: Candidate {self.current_candidate_index+1}/{len(self.candidates)} ({candidate.name}) ===")

            # 1. Detect headers
            self.detect_candidate_headers(candidate)

            # A source this network's DNS refuses to name is dead from here:
            # retrying it, or routing it through the proxy, asks the same
            # blocked host. Count it dead (so the event page is re-checked for
            # another source) and move straight on.
            skip = candidate.dns_blocked
            if skip:
                candidate.fail_count += 1
                self._log(f"{candidate.name}: {candidate.last_error} Skipping it.", "WARN")
                outcome = StreamOutcome.FAILED
            # The operator marked this feed as the wrong game, possibly under
            # another link: skip it the same way, counted dead for a re-check.
            elif self.is_wrong_source(candidate):
                skip = True
                candidate.fail_count += 1
                self._log(f"{candidate.name} plays the stream marked as the wrong game. "
                          "Skipping it.", "WARN")
                outcome = StreamOutcome.FAILED

            # 2. Attempt Direct Mode (Direct FFmpeg with -headers)
            #    A playlist only a browser TLS profile can fetch is read
            #    through a loopback relay; segments still go straight to the CDN
            #    with these same headers.
            if not skip:
                self._log(f"[Direct Mode] Connecting FFmpeg directly to {url_for_log(candidate.m3u8_url)}...")
                relay = None
                try:
                    input_url = candidate.m3u8_url
                    # Stepped down: record a lighter level of this playlist.
                    # Always through the relay, which checks the level's own
                    # contents for private addresses on every reload.
                    level = self._capped_level(candidate)
                    if level:
                        self._log(f"Recording a lighter level of this stream "
                                  f"({describe_level(level['kbps'], level['height'])}) "
                                  "because the connection could not keep up.", "WARN")
                    if (level or candidate.needs_relay or candidate.png_wrapped
                            or candidate.json_playlist):
                        try:
                            relay = PlaylistRelay(level["url"] if level else candidate.m3u8_url,
                                                  self._relay_headers(candidate),
                                                  unwrap_segments=candidate.png_wrapped)
                            input_url = relay.start()
                            why = "; ".join(filter(None, (
                                "recording a lighter level" if level else "",
                                "segments are video packed inside images, unwrapping them"
                                if candidate.png_wrapped else "",
                                "playlist is a JSON segment list" if candidate.json_playlist else "",
                                "playlist is only served to a browser TLS profile"
                                if candidate.needs_relay else "")))
                            why = why[:1].upper() + why[1:]
                            self._log(f"[Relay] {why}; relaying on {input_url}.")
                        except OSError as exc:
                            relay = None
                            self._log(f"[Relay] Could not start the playlist relay: {exc}", "ERROR")
                    direct_cmd = self._build_ffmpeg_cmd(
                        input_url, candidate.referer, candidate.user_agent, candidate.cookie
                    )
                    outcome = self._stream_ffmpeg_process(direct_cmd, candidate)
                finally:
                    self._reap_ffmpeg()
                    if relay is not None:
                        relay.stop()

            # 3. Fallback Mode: retry this candidate through hls-proxy -- but
            #    only where that can plausibly help.
            #
            #    FAILED (never produced a byte direct) is what the proxy is
            #    for: non-video segment extensions and header quirks that
            #    stop FFmpeg at connect time.
            #
            #    INTERRUPTED (delivered, then froze or died) used to get the
            #    same retry, on the theory that the proxy "re-scrapes" an
            #    expired token. It does not: it is handed the very playlist URL
            #    that just went quiet, in literal mode, so it asks the same dead
            #    edge again and FFmpeg waits out -rw_timeout (~17s measured on
            #    2026-10-07) before the backup is even tried. Re-resolving is
            #    already done by detect_candidate_headers() on every connect.
            #    So with a backup on hand, fail over now; with no backup, the
            #    proxy retry is still better than nothing.
            dead_mid_recording = (outcome is StreamOutcome.INTERRUPTED
                                  and self.has_next_candidate)
            if skip:
                pass    # already failed over above; the proxy asks the same host (or the wrong game)
            elif candidate.png_wrapped or candidate.json_playlist:
                pass    # hls-proxy can read neither the images nor a JSON playlist
            elif (dead_mid_recording
                    and not self._stop_event.is_set()
                    and not self._force_failover_flag):
                self._log(
                    f"[Direct Mode Interrupted] {candidate.name} stopped "
                    f"mid-recording; skipping the hls-proxy retry and failing "
                    f"over now.", "WARN")
            elif (outcome is not StreamOutcome.COMPLETED
                    and not self._stop_event.is_set()
                    and not self._force_failover_flag):
                self._log(f"[Direct Mode Failed] Falling back to hls-proxy-stream for {candidate.name}...", "WARN")
                # try/finally, because the teardown is not merely tidiness: it
                # kills the proxy child and deletes the channels.conf holding
                # the tokenised URL. On the straight-line version, anything
                # raising in here left both behind -- an orphaned proxy holding
                # its port, and a readable credential on the recordings volume.
                try:
                    proxy_url = self.start_proxy(candidate)
                    proxy_cmd = self._build_ffmpeg_cmd(proxy_url, local_proxy=True)

                    outcome = self._stream_ffmpeg_process(proxy_cmd, candidate)
                finally:
                    self._reap_ffmpeg()
                    self.stop_proxy()

            if self._stop_event.is_set():
                break

            # Any data at all means the sources are not collectively dead, so
            # the "fruitless laps" budget starts over. Without this reset, a
            # long capture that fails over now and then would eventually spend
            # its budget and stop despite recording perfectly well.
            # The flag stops the wrap below from counting this lap as
            # fruitless right after the reset.
            if self.bytes_written > bytes_before:
                self.cycles_without_data = 0
                self._lap_had_data = True

            # Consume the force-failover request: it applies to the candidate we
            # are leaving, not the one we are about to try. Leaving it latched
            # makes every remaining candidate abort on entry, turning a single
            # button press into a dead recording.
            forced = self._force_failover_flag
            self._force_failover_flag = False
            manual_target = self._manual_target_index
            self._manual_target_index = None
            # A step-down/up from the monitor. Dropped if the operator asked
            # for something else or a failover already moved us on.
            step = self._step_request
            self._step_request = None
            stepping = (step is not None and manual_target is None
                        and step[0] == self.current_candidate_index)
            if stepping:
                manual_target = step[1]
            elif step is not None:
                self._step_dropped(step)

            # A clean finish ends the recording -- unless the operator asked to
            # move, in which case honour that instead.
            #
            # Or unless the recording has an explicit end time still ahead.
            # FFmpeg exits 0 whenever the playlist says it is over (an
            # ENDLIST, or a reload it reads as the end), and a flaky upstream
            # does that mid-game: on 2026-10-11 a recording with ~230 min
            # left ended itself 13s after a reconnect. The end time is the
            # operator saying when the event ends, so before it a clean exit
            # is a drop like any other. Local aborts (disk, deleted output)
            # set an "aborted_*" status and still end it.
            if (outcome is StreamOutcome.COMPLETED
                    and not forced and manual_target is None):
                if not (self.end_time and self._within_window()
                        and not self.status.startswith("aborted")):
                    break
                remaining = self.seconds_remaining() or 0
                self._log(
                    f"{candidate.name} ended cleanly, but the recording window "
                    f"is open for another {remaining / 60:.0f} min -- treating "
                    f"it as a drop.", "WARN",
                )
                candidate.fail_count += 1

            # Where next? An explicit request wins; otherwise step forward and
            # wrap around the end of the list.
            if manual_target is not None:
                next_index, wrapped = manual_target, False
            else:
                next_index = self._next_candidate_index()
                # A single-candidate session wraps onto itself, which is what
                # gives it retries at all rather than one attempt and out.
                wrapped = next_index <= self.current_candidate_index

            if wrapped:
                if not self._lap_had_data:
                    self.cycles_without_data += 1
                self._lap_had_data = False
                # Sponsor decision: a recording with a window keeps trying
                # until that window closes. max_cycles is a guess at "these
                # sources are dead"; an explicit end time is a statement that
                # the event runs until then, and a stream that is down at
                # kick-off is often back minutes later. The backoff from
                # Phase 14 already keeps a dead set of origins from being
                # hammered, and _deadline_ok() above is what finally ends it.
                #
                # Deliberately keyed off the *window*, not the backstop: with
                # no explicit end time this is a plain 6h default the operator
                # may not know about, and retrying for six hours against three
                # dead URLs is not what anyone meant by it.
                if self.cycles_without_data == 0:
                    pass  # this lap delivered data: nothing to warn about
                elif self.end_time and self._within_window():
                    remaining = self.seconds_remaining() or 0
                    self._log(
                        f"No data from any candidate in "
                        f"{self.cycles_without_data} full attempts, but the "
                        f"recording window is open for another "
                        f"{remaining / 60:.0f} min -- still trying.", "WARN",
                    )
                elif self.is_rebroadcast:
                    # A channel is meant to be there until the operator stops
                    # it: an upstream that is down now is often back later, and
                    # nothing is kept, so there is nothing to finalise. The
                    # backoff (capped at 60 s) keeps dead origins from being
                    # hammered while it waits.
                    if self.cycles_without_data == self.max_cycles:
                        self._log(
                            f"No data from any candidate in {self.max_cycles} "
                            f"full attempts; a channel keeps trying until it "
                            f"is stopped.", "WARN",
                        )
                elif self.cycles_without_data >= self.max_cycles:
                    # Giving up after capturing real footage is not the same as
                    # never recording anything. Keeping these distinct is what
                    # lets post-processing still run on a long recording whose
                    # stream died near the end.
                    laps = self.cycles_without_data
                    if self.bytes_written > 0:
                        self._log(
                            f"No data from any candidate in {laps} full attempts; "
                            f"keeping {self.bytes_written} bytes already recorded.",
                            "WARN",
                        )
                        self.status = "completed_partial"
                    else:
                        self._log(
                            f"No data from any candidate in {laps} full attempts.",
                            "ERROR",
                        )
                        self.status = "failed"
                    break

            self.current_candidate_index = next_index
            next_name = self.candidates[next_index].name
            self.status = "failing_over"
            delay = self._failover_delay(wrapped)
            if wrapped:
                # Only a fruitless lap is worth counting out loud; after a lap
                # that delivered data, "lap 0 of 3" would read as a mistake.
                fruitless = (
                    f" (fruitless lap {self.cycles_without_data}"
                    + ("" if self.is_rebroadcast else f" of {self.max_cycles}")
                    + ")"
                ) if self.cycles_without_data else ""
                self._log(
                    f"Cycling back to Candidate {next_index + 1} ({next_name}) "
                    f"after {delay:.0f}s{fruitless}...", "WARN",
                )
            elif stepping:
                pass  # _check_step already said why, and it is not a failover
            else:
                self._log(
                    f"Failing over to Candidate {next_index + 1} ({next_name})...",
                    "WARN",
                )
            if self.on_failover_callback and not stepping:
                try:
                    self.on_failover_callback(self.recording_id, next_name)
                except Exception:
                    pass
            # Interruptible: a plain sleep here held a stop for up to 60s,
            # past the 20s shutdown budget and the 30s stop_grace_period, so
            # Docker SIGKILLed the container mid-shutdown.
            if self._stop_event.wait(delay):
                break

        if self.status != "failed":
            # These must survive: each says the file is worth keeping but the
            # stream did not run to its natural end. Overwriting them with
            # "completed" would hide why the recording is short.
            # "interrupted" joins these: the container is going away with the
            # recording still wanted, and overwriting it with "completed" is
            # what told the resume logic there was nothing to come back to.
            if self.status not in (
                "completed_partial", "aborted_no_space", "aborted_output_lost",
                "interrupted", "completed_window",
            ):
                self.status = "completed"
            if self.on_completion_callback:
                # Remuxing a long recording is minutes of work on this thread
                # (263 MB took 2.5 of them on the test server), and until it
                # finishes there is no .mp4 in the library. Reporting
                # "completed" through that window told the operator the job was
                # done while the file did not yet exist anywhere they could see
                # -- and left the status dot pulsing green next to the word
                # "completed", which is the contradiction that surfaced this.
                final_status = self.status
                self.status = "post_processing"
                try:
                    self.on_completion_callback(str(self.output_filepath))
                except Exception:
                    pass
                finally:
                    self.status = final_status

        self._flush_segment_loss()
        self.is_running = False
        self.stop_time = time.time()
        lost = f", {self.segments_lost} stream segments lost" if self.segments_lost else ""
        self._log(f"Recorder finished. Total recorded: {self.get_filesize_mb():.2f} MB ({self.bytes_written} bytes){lost}")

    def get_elapsed_seconds(self) -> float:
        if not self.start_time:
            return 0.0
        end = self.stop_time or time.time()
        return round(end - self.start_time, 1)

    @property
    def current_filepath(self) -> Path:
        """The file that currently represents this recording on disk."""
        return self.final_filepath or self.output_filepath

    def get_filesize_mb(self) -> float:
        # A rebroadcast keeps nothing. The ring's backing file is a fixed size
        # regardless of how much has flowed through it, so reporting it here
        # would show a constant 75 MB "recording" that never grows.
        if self.is_rebroadcast:
            return 0.0
        target = self.current_filepath
        if target.exists():
            return round(target.stat().st_size / (1024 * 1024), 2)
        return 0.0

    def dead_since_rescan(self) -> int:
        """How many candidates have failed since the event page was last checked."""
        return sum(1 for c in self.candidates
                   if c.fail_count > self._fail_snapshot.get(c.url, 0))

    def slots_to_refill(self, max_candidates: int = 3) -> int:
        """How many slots replace_dead_candidates() would fill right now:
        failed slots other than the one being recorded, plus room to top up."""
        dead = sum(1 for i, c in enumerate(self.candidates)
                   if i != self.current_candidate_index
                   and c.fail_count > self._fail_snapshot.get(c.url, 0))
        return dead + max(0, max_candidates - len(self.candidates))

    def fresh_link_plays(self, url: str) -> bool:
        """Probe a fresh event-page link the way a connect does, before it
        replaces a source.

        The page check probed it already, but minutes can pass between that
        and the swap, and a link that "worked" for the scan was twice found
        with nothing playable once it was swapped in, while the source it
        replaced still played. Uses the connect's own probe; with auto-probe
        off the recorder never probes, so neither does this. Blocking (network
        I/O): call it from a worker thread, never the event loop.
        """
        if not self.auto_probe:
            return True
        fresh = CandidateStream(url, name="fresh link from the event page")
        if not self._probe_candidate(fresh):
            return False
        if self.is_wrong_source(fresh):
            self._log("A fresh event-page link plays the stream marked as the wrong game; "
                      "not using it.", "WARN")
            return False
        return True

    def replace_dead_candidates(self, urls: List[str],
                                max_candidates: int = 3) -> List[Tuple[int, str]]:
        """Put fresh links from the event page into the slots that died.

        Never touches the slot being recorded: the capture loop holds that
        candidate for the whole attempt, and it is the one thing still
        working. Only ever called by one re-check at a time (rescan_state),
        so it needs no lock of its own; the list never shrinks, so the
        loop's index stays valid. Other slots are swapped by assignment,
        which the loop picks up the next time it moves -- it reads
        candidates[index] fresh on every iteration. A list shorter than
        max_candidates is topped up. Returns (slot index, url) for every
        slot that changed.
        """
        have = {c.url for c in self.candidates}
        fresh = [u for u in urls if u and u.strip() and u not in have]
        changed: List[Tuple[int, str]] = []
        for i, c in enumerate(self.candidates):
            if not fresh:
                break
            if i == self.current_candidate_index:
                continue
            if c.fail_count > self._fail_snapshot.get(c.url, 0):
                self.candidates[i] = CandidateStream(fresh.pop(0), name=c.name)
                changed.append((i, self.candidates[i].url))
        while fresh and len(self.candidates) < max_candidates:
            n = len(self.candidates)
            self.candidates.append(CandidateStream(fresh.pop(0), name=f"Candidate {n + 1}"))
            changed.append((n, self.candidates[n].url))
        self._fail_snapshot = {c.url: c.fail_count for c in self.candidates}
        if changed:
            # New sources get a fresh lap budget, as a fresh start would.
            self.cycles_without_data = 0
        for i, _ in changed:
            self._log(f"Event page re-checked: {self.candidates[i].name} now has a fresh link.", "WARN")
        return changed

    def get_status_summary(self) -> Dict[str, Any]:
        # Only a resolved playlist says which feed a candidate is (a backup's
        # standby check resolves one too); before that the URL is a page.
        letters = quality.source_letters(
            [c.m3u8_url if c.detected else c.standby_m3u8 for c in self.candidates])
        return {
            "id": self.recording_id,
            "status": self.status,
            "is_running": self.is_running,
            "output_file": "" if self.is_rebroadcast else str(self.current_filepath),
            "output_filename": "" if self.is_rebroadcast else self.current_filepath.name,
            # The guide falls back to this when there is no filename.
            "channel_name": self.channel_name or "",
            "is_rebroadcast": self.is_rebroadcast,
            "filesize_mb": self.get_filesize_mb(),
            "bytes_written": self.bytes_written,
            "elapsed_seconds": self.get_elapsed_seconds(),
            "started_at": self.start_time,
            # Clamped: the index runs one past the end when the candidate list
            # is exhausted, which the dashboard rendered as "Stream 2 of 1".
            "current_candidate": min(self.current_candidate_index + 1, len(self.candidates)),
            "cycles_without_data": self.cycles_without_data,
            "segments_lost": self.segments_lost,
            "segments_failed": self.segments_failed,
            "segments_expired": self.segments_expired,
            "free_disk_gb": (
                round(free / 1024 ** 3, 2) if (free := self.free_bytes()) is not None else None
            ),
            "min_free_disk_gb": round(self.min_free_bytes / 1024 ** 3, 2),
            "max_cycles": self.max_cycles,
            # Absolute, so the dashboard renders it in the viewer's local time
            # and the container's TZ never enters into it. None means the
            # recording runs until the stream ends.
            "ends_at": self.deadline(),
            "seconds_remaining": (
                round(remaining, 1)
                if (remaining := self.seconds_remaining()) is not None else None
            ),
            "total_candidates": len(self.candidates),
            "has_event_page": self.has_event_page,
            "rescan_state": self.rescan_state,
            "candidates": [dict(c.to_dict(), source=letter,
                                wrong_game=self.is_wrong_source(c),
                                standby=self.standby_state(c))
                           for c, letter in zip(self.candidates, letters)],
            # What this stream needs from the connection, over the last ~60 s.
            "rate_mbps": self.rate_mbps(),
            # Distinct real feeds behind the candidates that have a known
            # playlist; mirrors of one origin count once (quality.source_key).
            "distinct_sources": len({x for x in letters if x}),
            "unknown_sources": sum(1 for x in letters if not x),
            "logs": self.log_history[-30:]
        }
