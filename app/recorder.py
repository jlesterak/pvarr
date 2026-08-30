#!/usr/bin/env python3
"""
PVArr Core Recorder & Multi-Stream Failover Engine
Implements Direct-First FFmpeg connection with automatic fallback to hls-proxy-stream,
dynamic HTTP header injection, freeze detection, and continuous segment appending.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any

from app.check_deps import find_executable
from app.probe import DEFAULT_USER_AGENT, probe_stream

logger = logging.getLogger("PVArrRecorder")


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

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "m3u8_url": self.m3u8_url,
            "referer": self.referer,
            "user_agent": self.user_agent,
            "cookie": self.cookie,
            "detected": self.detected,
            "detect_source": self.detect_source,
            "detect_note": self.detect_note,
            "used_proxy": self.used_proxy,
            "fail_count": self.fail_count,
            "last_error": self.last_error,
        }


class StreamFailoverRecorder:
    def __init__(
        self,
        recording_id: str,
        candidates: List[str],
        output_filepath: str,
        base_port: int = 8090,
        freeze_timeout_sec: int = 15,
        log_callback: Optional[Callable[[str], None]] = None,
        on_completion_callback: Optional[Callable[[str], None]] = None,
        on_failover_callback: Optional[Callable[[str, str], None]] = None,
        header_overrides: Optional[Dict[str, Dict[str, str]]] = None,
        auto_probe: bool = True
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
        self.freeze_timeout_sec = freeze_timeout_sec
        self.log_callback = log_callback
        self.on_completion_callback = on_completion_callback
        self.on_failover_callback = on_failover_callback

        self.current_candidate_index: int = 0
        self.is_running: bool = False
        self.status: str = "initialized"  # initialized, recording, failing_over, completed, failed
        self.start_time: Optional[float] = None
        self.stop_time: Optional[float] = None
        self.bytes_written: int = 0
        self.log_history: List[str] = []

        self._thread: Optional[threading.Thread] = None
        self._ffmpeg_process: Optional[subprocess.Popen] = None
        self._proxy_process: Optional[subprocess.Popen] = None
        self._force_failover_flag: bool = False
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
            if len(self.log_history) > 500:
                self.log_history.pop(0)
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

        port = self.base_port + self.current_candidate_index
        conf_dir = self.output_filepath.parent / ".proxy_conf"
        conf_dir.mkdir(parents=True, exist_ok=True)
        conf_file = conf_dir / f"channels_{self.recording_id}.conf"

        mode = "literal" if candidate.referer else "direct"
        with open(conf_file, "w", encoding="utf-8") as f:
            f.write(f"{candidate.slug}|{candidate.name}|1||Sports|{candidate.m3u8_url}|{mode}|{candidate.referer}|\n")

        env = os.environ.copy()
        env["HLS_PROXY_PORT"] = str(port)
        env["CHANNELS_CONF"] = str(conf_file)
        if candidate.referer:
            env["HLS_PROXY_REFERER"] = candidate.referer

        self._log(f"[Fallback Mode] Launching hls-proxy on port {port} for {candidate.name}...")
        try:
            self._proxy_process = subprocess.Popen(
                [sys.executable, self.hls_proxy_path],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            time.sleep(1.5)
            candidate.used_proxy = True
            return f"http://127.0.0.1:{port}/channel/{candidate.slug}"
        except Exception as e:
            self._log(f"Failed to start hls-proxy: {e}", "ERROR")
            return candidate.m3u8_url

    def stop_proxy(self):
        """Terminate active hls-proxy subprocess."""
        if self._proxy_process:
            try:
                self._proxy_process.terminate()
                self._proxy_process.wait(timeout=2)
            except Exception:
                try:
                    self._proxy_process.kill()
                except Exception:
                    pass
            self._proxy_process = None

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

    def force_failover(self):
        """Manual trigger to force switch to next stream candidate."""
        self._log("Manual force-failover requested!", "WARN")
        self._force_failover_flag = True
        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
            except Exception:
                pass

    def stop(self):
        """Gracefully stop recording."""
        self._log("Stopping PVArr recorder gracefully...")
        self._stop_event.set()
        self.is_running = False
        self.status = "completed"
        self.stop_time = time.time()

        self._reap_ffmpeg()
        self.stop_proxy()

    def _build_ffmpeg_cmd(
        self, stream_url: str, referer: str = "", user_agent: str = "", cookie: str = ""
    ) -> List[str]:
        """Build FFmpeg command line with custom HTTP headers if present."""
        cmd = [
            self.ffmpeg_path or "ffmpeg",
            "-y",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-rw_timeout", "15000000",
        ]

        # Construct HTTP headers for Direct Mode
        headers_str = ""
        if user_agent:
            headers_str += f"User-Agent: {user_agent}\r\n"
        if referer:
            headers_str += f"Referer: {referer}\r\n"
        if cookie:
            headers_str += f"Cookie: {cookie}\r\n"

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

        with open(self.output_filepath, "ab") as out_f:
            self._ffmpeg_process = subprocess.Popen(
                ffmpeg_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=65536
            )

            while not self._stop_event.is_set():
                if self._force_failover_flag:
                    self._log(f"Forced failover triggered on {candidate.name}", "WARN")
                    return StreamOutcome.INTERRUPTED

                chunk = self._ffmpeg_process.stdout.read(32768)
                if chunk:
                    out_f.write(chunk)
                    out_f.flush()
                    len_chunk = len(chunk)
                    self.bytes_written += len_chunk
                    written_for_this_session += len_chunk
                    last_write_time = time.time()
                    continue

                ret_code = self._ffmpeg_process.poll()
                if ret_code is not None:
                    if written_for_this_session == 0:
                        return StreamOutcome.FAILED
                    if ret_code == 0:
                        return StreamOutcome.COMPLETED
                    # Non-zero exit after delivering data: FFmpeg died partway
                    # through. Previously indistinguishable from a clean finish.
                    self._log(
                        f"FFmpeg exited {ret_code} after {written_for_this_session} bytes "
                        f"on {candidate.name}; treating as interrupted", "ERROR"
                    )
                    candidate.fail_count += 1
                    return StreamOutcome.INTERRUPTED

                if (time.time() - last_write_time) > self.freeze_timeout_sec:
                    self._log(f"Stream freeze detected! No data received for {self.freeze_timeout_sec}s", "ERROR")
                    candidate.fail_count += 1
                    if written_for_this_session == 0:
                        return StreamOutcome.FAILED
                    return StreamOutcome.INTERRUPTED
                time.sleep(0.2)

        # Loop exited because stop() was requested -- an operator stop is a
        # clean end, not a failure.
        return (
            StreamOutcome.COMPLETED
            if written_for_this_session > 0
            else StreamOutcome.FAILED
        )

    def _recording_loop(self):
        """Main recording & failover loop."""
        while not self._stop_event.is_set() and self.current_candidate_index < len(self.candidates):
            candidate = self.candidates[self.current_candidate_index]
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
                proxy_cmd = self._build_ffmpeg_cmd(proxy_url)
                
                outcome = self._stream_ffmpeg_process(proxy_cmd, candidate)

                self._reap_ffmpeg()
                self.stop_proxy()

            if self._stop_event.is_set():
                break

            # Consume the force-failover request: it applies to the candidate we
            # are leaving, not the one we are about to try. Leaving it latched
            # makes every remaining candidate abort on entry, turning a single
            # button press into a dead recording.
            forced = self._force_failover_flag
            self._force_failover_flag = False

            # Anything short of a clean completion means try the next candidate.
            # INTERRUPTED lands here now; it used to be read as success, which
            # ended the recording at the point of the stall.
            if forced or outcome is not StreamOutcome.COMPLETED:
                self.current_candidate_index += 1
                if self.current_candidate_index < len(self.candidates):
                    next_name = self.candidates[self.current_candidate_index].name
                    self.status = "failing_over"
                    self._log(f"Failing over to Candidate {self.current_candidate_index+1} ({next_name})...", "WARN")
                    if self.on_failover_callback:
                        try:
                            self.on_failover_callback(self.recording_id, next_name)
                        except Exception:
                            pass
                    time.sleep(1)
                else:
                    # Exhausting the list after capturing real footage is not
                    # the same as never recording anything. Keeping these
                    # distinct is what lets post-processing still run on a
                    # long recording whose stream died near the end.
                    if self.bytes_written > 0:
                        self._log(
                            "All stream candidates exhausted; keeping "
                            f"{self.bytes_written} bytes already recorded.", "WARN"
                        )
                        self.status = "completed_partial"
                    else:
                        self._log("All stream candidates exhausted!", "ERROR")
                        self.status = "failed"
                    break
            else:
                # Stream completed naturally
                break

        if self.status != "failed":
            # completed_partial must survive: it tells the caller the file is
            # worth keeping but the stream did not run to its natural end.
            if self.status != "completed_partial":
                self.status = "completed"
            if self.on_completion_callback:
                try:
                    self.on_completion_callback(str(self.output_filepath))
                except Exception:
                    pass

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
        target = self.current_filepath
        if target.exists():
            return round(target.stat().st_size / (1024 * 1024), 2)
        return 0.0

    def get_status_summary(self) -> Dict[str, Any]:
        return {
            "id": self.recording_id,
            "status": self.status,
            "is_running": self.is_running,
            "output_file": str(self.current_filepath),
            "output_filename": self.current_filepath.name,
            "filesize_mb": self.get_filesize_mb(),
            "bytes_written": self.bytes_written,
            "elapsed_seconds": self.get_elapsed_seconds(),
            "started_at": self.start_time,
            "current_candidate": self.current_candidate_index + 1,
            "total_candidates": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "logs": self.log_history[-30:]
        }
