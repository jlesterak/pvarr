#!/usr/bin/env python3
"""
PVArr Core Recorder & Multi-Stream Failover Engine
Implements Direct-First FFmpeg connection with automatic fallback to hls-proxy-stream,
dynamic HTTP header injection, freeze detection, and continuous segment appending.
"""

import collections
import json
import logging
import os
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
from typing import List, Optional, Callable, Dict, Any, Tuple

from app.check_deps import find_executable
from app.probe import DEFAULT_USER_AGENT, probe_stream

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

    Anti-leech streams disguise segments as something else. The sponsor's
    candidate 1 serves MPEG-TS from a TikTok *image* CDN, on URLs ending
    ".image", and FFmpeg's HLS demuxer refuses any extension outside its
    allowlist. hls-proxy mirrors the upstream extension onto its own
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
        self.fail_count: int = 0
        self.last_error: str = ""

    def to_dict(self, include_secrets: bool = False) -> Dict[str, Any]:
        """Serialise this candidate.

        The cookie is a live session credential -- often the only thing
        standing between a stranger and the sponsor's paid account -- and
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
            "fail_count": self.fail_count,
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
    # Tail of FFmpeg's stderr kept for diagnostics when an attempt fails.
    STDERR_TAIL_LINES = 15
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
        header_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        auto_probe: bool = True,
        max_cycles: int = 3,
        min_free_gb: Optional[float] = None,
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

        self.current_candidate_index: int = 0
        # How many complete laps of the candidate list may pass without a
        # single byte arriving before the recording is given up on. Reset the
        # moment any candidate delivers data, so a long capture that fails over
        # occasionally never exhausts its budget.
        self.max_cycles = max(1, int(max_cycles))
        self.cycles_without_data: int = 0
        # Recordings are uncompressed TS and grow without bound. The recordings
        # volume is usually the same filesystem as everything else, so an
        # unattended 24/7 capture does not just lose itself -- it takes the host
        # down with it. Abort while there is still room to operate.
        self.min_free_bytes = int(
            (DEFAULT_MIN_FREE_GB if min_free_gb is None else float(min_free_gb))
            * 1024 ** 3
        )
        self._last_disk_check: float = 0.0
        self._last_output_check: float = 0.0
        self._output_reopens: int = 0
        self.is_running: bool = False
        self.status: str = "initialized"  # initialized, recording, failing_over, completed, failed
        self.start_time: Optional[float] = None
        self.stop_time: Optional[float] = None
        self.bytes_written: int = 0
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
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        # Resolve executables
        self.hls_proxy_path = find_executable("hls-proxy.py", ["hls-proxy"])
        self.detect_headers_path = find_executable("detect-headers-py.py", ["detect-headers.sh", "detect-headers"])
        self.ffmpeg_path = find_executable("ffmpeg")

    def _log(self, message: str, level: str = "INFO"):
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

        Order is in-process probe, then the external detect-headers script, then
        the URL as given. The probe handles the ordinary cases with no extra
        install; the script is worth keeping for pages that only assemble their
        m3u8 in JavaScript, which needs a real browser.
        """
        candidate.slug = candidate.slug or f"cand_{self.current_candidate_index}"

        if self.auto_probe and self._probe_candidate(candidate):
            return True

        if self._detect_via_script(candidate):
            return True

        candidate.m3u8_url = candidate.url
        candidate.detected = True
        candidate.detect_source = "raw"
        candidate.detect_note = "Using the URL as given; no headers detected."
        return True

    def _probe_candidate(self, candidate: CandidateStream) -> bool:
        """Try the built-in probe. Returns False so the caller can fall through."""
        self._log(f"Probing {candidate.name}: {candidate.url[:70]}...")
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

        if not result.get("ok"):
            candidate.last_error = result.get("message", "Probe failed")
            self._log(f"Probe found nothing playable for {candidate.name}: {candidate.last_error}", "WARN")
            return False

        candidate.m3u8_url = result["m3u8_url"]
        candidate.referer = result.get("referer", "")
        candidate.cookie = result.get("cookie", "")
        if result.get("user_agent"):
            candidate.user_agent = result["user_agent"]
        candidate.detected = True
        candidate.detect_source = "probe"
        candidate.detect_note = result.get("message", "")
        required = result.get("headers_required") or ["none"]
        self._log(
            f"Probe resolved {candidate.name}: {result.get('kind', 'playlist')}, "
            f"headers {', '.join(required)}."
        )
        return True

    def _detect_via_script(self, candidate: CandidateStream) -> bool:
        """Fallback to the optional detect-headers CLI (browser-backed)."""
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
        with open(conf_file, "w", encoding="utf-8") as f:
            f.write(f"{candidate.slug}|{candidate.name}|1||Sports|{conf_url}|{mode}|{conf_referer}|\n")

        env = os.environ.copy()
        env["HLS_PROXY_PORT"] = str(port)
        env["CHANNELS_CONF"] = str(conf_file)
        self._proxy_conf_file = conf_file
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
                detail = "; ".join(self._proxy_stderr_tail) or "no output"
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

    def stop_proxy(self):
        """Terminate active hls-proxy subprocess."""
        if self._proxy_process:
            try:
                self._proxy_process.terminate()
                self._proxy_process.wait(timeout=2)
            except Exception:
                try:
                    self._proxy_process.kill()
                    # kill() only delivers SIGKILL. Without the wait() the
                    # dead child stays in the process table as a zombie, and a
                    # long session that fails over repeatedly accumulates one
                    # per switch. Same defect _reap_ffmpeg() documents.
                    self._proxy_process.wait(timeout=2)
                except Exception:
                    pass
            self._proxy_process = None
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

    def _failover_delay(self, wrapped: bool) -> float:
        """Pause before the next attempt.

        Within a lap this is the original short breath. After a whole lap that
        produced nothing, back off so a set of genuinely dead origins is not
        hammered in a tight loop: 5s, 10s, 20s, capped at 60s.
        """
        if not wrapped:
            return 1.0
        return min(5.0 * (2 ** max(0, self.cycles_without_data - 1)), 60.0)

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
            "-loglevel", "error",
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

        if local_proxy:
            cmd.extend(hls_extension_flags(self.ffmpeg_path))

        # Construct HTTP headers for Direct Mode. Each value is checked because
        # FFmpeg copies this block verbatim into the request; see
        # safe_header_value().
        headers_str = ""
        for name, raw in (("User-Agent", user_agent), ("Referer", referer),
                          ("Cookie", cookie)):
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
            "-f", "mpegts",
            "pipe:1"
        ])

        return cmd

    def _stream_ffmpeg_process(
        self, ffmpeg_cmd: List[str], candidate: CandidateStream
    ) -> "StreamOutcome":
        """Stream FFmpeg stdout to the output file and report how the attempt ended."""
        written_for_this_session = 0
        last_write_time = time.time()

        with self._open_sink() as out_f:
            self._ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            stdout_fd = self._ffmpeg_process.stdout.fileno()
            stderr_tail = self._drain_stderr(self._ffmpeg_process)
            at_eof = False

            def finish(outcome: "StreamOutcome") -> "StreamOutcome":
                """Attach FFmpeg's own words to a failed attempt."""
                if outcome is not StreamOutcome.COMPLETED and stderr_tail:
                    detail = " | ".join(stderr_tail)[:500]
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

                if (time.time() - last_write_time) > self.freeze_timeout_sec:
                    self._log(f"Stream freeze detected! No data received for {self.freeze_timeout_sec}s", "ERROR")
                    candidate.fail_count += 1
                    if written_for_this_session == 0:
                        return finish(StreamOutcome.FAILED)
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

    def _drain_stderr(self, proc: subprocess.Popen) -> "collections.deque":
        """Continuously drain FFmpeg's stderr, keeping only the tail.

        Two jobs. The pipe must be read or FFmpeg eventually blocks writing to
        it and stops producing video -- that is a hang, not a stream fault, and
        no amount of failover logic can recover from it. And when an attempt
        does fail, FFmpeg's last few lines are usually the only explanation of
        why (403, 404, bad codec), which previously went nowhere.

        Daemon thread, bounded buffer: it holds at most STDERR_TAIL_LINES lines
        and exits on its own when the pipe closes.
        """
        tail: "collections.deque" = collections.deque(maxlen=self.STDERR_TAIL_LINES)
        stream = proc.stderr
        if stream is None:
            return tail

        def pump():
            try:
                for raw in iter(stream.readline, b""):
                    line = raw.decode("utf-8", "replace").strip()
                    if line:
                        tail.append(line)
            except Exception:
                pass  # pipe closed under us during shutdown; nothing to do

        threading.Thread(
            target=pump, name=f"pvarr-stderr-{self.recording_id}", daemon=True
        ).start()
        return tail

    def _recording_loop(self):
        """Main recording & failover loop.

        The candidate index used to only ever increment, so the list was a
        one-way walk: run off the end and the recording stopped, with no route
        back to candidate 1 even after it recovered. The most common failure
        here is an expiring token, which fixes itself in minutes -- so a blip
        that touched all three sources could end a three-hour capture while
        every one of them was healthy again. The index now wraps.
        """
        while not self._stop_event.is_set():
            candidate = self.candidates[self.current_candidate_index]
            bytes_before = self.bytes_written
            # Clear any lingering "failing_over" from the previous iteration so
            # the dashboard shows the candidate we are actually recording.
            self.status = "recording"
            self._log(f"=== Active Stream: Candidate {self.current_candidate_index+1}/{len(self.candidates)} ({candidate.name}) ===")

            # 1. Detect headers
            self.detect_candidate_headers(candidate)

            # 2. Attempt Direct Mode (Direct FFmpeg with -headers)
            self._log(f"[Direct Mode] Connecting FFmpeg directly to {candidate.m3u8_url[:70]}...")
            direct_cmd = self._build_ffmpeg_cmd(
                candidate.m3u8_url, candidate.referer, candidate.user_agent, candidate.cookie
            )
            
            outcome = self._stream_ffmpeg_process(direct_cmd, candidate)

            # Clean up FFmpeg process
            self._reap_ffmpeg()

            # 3. Fallback Mode: direct attempt did not finish cleanly. Worth a
            #    proxy retry either way -- FAILED usually means headers/token
            #    were rejected, and INTERRUPTED often means a token expired
            #    mid-stream, which is exactly what the proxy re-scrapes.
            if (outcome is not StreamOutcome.COMPLETED
                    and not self._stop_event.is_set()
                    and not self._force_failover_flag):
                self._log(f"[Direct Mode Failed] Falling back to hls-proxy-stream for {candidate.name}...", "WARN")
                proxy_url = self.start_proxy(candidate)
                proxy_cmd = self._build_ffmpeg_cmd(proxy_url, local_proxy=True)
                
                outcome = self._stream_ffmpeg_process(proxy_cmd, candidate)

                self._reap_ffmpeg()
                self.stop_proxy()

            if self._stop_event.is_set():
                break

            # Any data at all means the sources are not collectively dead, so
            # the "fruitless laps" budget starts over. Without this reset, a
            # long capture that fails over now and then would eventually spend
            # its budget and stop despite recording perfectly well.
            if self.bytes_written > bytes_before:
                self.cycles_without_data = 0

            # Consume the force-failover request: it applies to the candidate we
            # are leaving, not the one we are about to try. Leaving it latched
            # makes every remaining candidate abort on entry, turning a single
            # button press into a dead recording.
            forced = self._force_failover_flag
            self._force_failover_flag = False
            manual_target = self._manual_target_index
            self._manual_target_index = None

            # A clean finish ends the recording -- unless the operator asked to
            # move, in which case honour that instead.
            if (outcome is StreamOutcome.COMPLETED
                    and not forced and manual_target is None):
                break

            # Where next? An explicit request wins; otherwise step forward and
            # wrap around the end of the list.
            if manual_target is not None:
                next_index, wrapped = manual_target, False
            else:
                next_index = (self.current_candidate_index + 1) % len(self.candidates)
                # A single-candidate session wraps onto itself, which is what
                # gives it retries at all rather than one attempt and out.
                wrapped = next_index <= self.current_candidate_index

            if wrapped:
                self.cycles_without_data += 1
                if self.cycles_without_data >= self.max_cycles:
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
                self._log(
                    f"Cycling back to Candidate {next_index + 1} ({next_name}) "
                    f"after {delay:.0f}s (lap {self.cycles_without_data} of "
                    f"{self.max_cycles})...", "WARN",
                )
            else:
                self._log(
                    f"Failing over to Candidate {next_index + 1} ({next_name})...",
                    "WARN",
                )
            if self.on_failover_callback:
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
                "interrupted",
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

        self.is_running = False
        self.stop_time = time.time()
        self._log(f"Recorder finished. Total recorded: {self.get_filesize_mb():.2f} MB ({self.bytes_written} bytes)")

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

    def get_status_summary(self) -> Dict[str, Any]:
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
            "free_disk_gb": (
                round(free / 1024 ** 3, 2) if (free := self.free_bytes()) is not None else None
            ),
            "min_free_disk_gb": round(self.min_free_bytes / 1024 ** 3, 2),
            "max_cycles": self.max_cycles,
            "total_candidates": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "logs": self.log_history[-30:]
        }
