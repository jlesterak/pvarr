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
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Callable, Dict, Any

from app.check_deps import find_executable

logger = logging.getLogger("PVArrRecorder")


class CandidateStream:
    def __init__(self, url: str, name: str = "Stream"):
        self.url = url.strip()
        self.name = name
        self.m3u8_url: str = ""
        self.referer: str = ""
        self.user_agent: str = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.slug: str = ""
        self.detected: bool = False
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
            "detected": self.detected,
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
        on_failover_callback: Optional[Callable[[str, str], None]] = None
    ):
        self.recording_id = recording_id
        self.candidates: List[CandidateStream] = [
            CandidateStream(url, name=f"Candidate {i+1}")
            for i, url in enumerate(candidates)
            if url and url.strip()
        ]
        self.output_filepath = Path(output_filepath).resolve()
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
        """Run detect-headers CLI script to inspect m3u8 and required HTTP headers."""
        if not self.detect_headers_path or not os.path.exists(self.detect_headers_path):
            candidate.m3u8_url = candidate.url
            candidate.slug = f"cand_{id(candidate)}"
            candidate.detected = True
            return True

        self._log(f"Detecting headers for {candidate.name}: {candidate.url[:70]}...")
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
                candidate.referer = data.get("referer", "")
                if data.get("user_agent"):
                    candidate.user_agent = data.get("user_agent")
                candidate.slug = data.get("slug", f"cand_{self.current_candidate_index}")
                candidate.detected = True
                self._log(f"Header detection successful for {candidate.name}.")
                return True
            else:
                candidate.last_error = res.stderr.strip() or "Detection failed"
        except Exception as e:
            candidate.last_error = str(e)

        candidate.m3u8_url = candidate.url
        candidate.slug = f"cand_{self.current_candidate_index}"
        candidate.detected = True
        return True

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

    def _build_ffmpeg_cmd(self, stream_url: str, referer: str = "", user_agent: str = "") -> List[str]:
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

        if headers_str:
            cmd.extend(["-headers", headers_str])

        cmd.extend([
            "-i", stream_url,
            "-c", "copy",
            "-f", "mpegts",
            "pipe:1"
        ])

        return cmd

    def _stream_ffmpeg_process(self, ffmpeg_cmd: List[str], candidate: CandidateStream) -> bool:
        """Stream chunks from FFmpeg stdout to destination file. Returns True if successful data was written."""
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
                    return False

                chunk = self._ffmpeg_process.stdout.read(32768)
                if chunk:
                    out_f.write(chunk)
                    out_f.flush()
                    len_chunk = len(chunk)
                    self.bytes_written += len_chunk
                    written_for_this_session += len_chunk
                    last_write_time = time.time()
                else:
                    ret_code = self._ffmpeg_process.poll()
                    if ret_code is not None:
                        if ret_code != 0 and written_for_this_session == 0:
                            return False  # Failed immediately without writing data
                        break

                    if (time.time() - last_write_time) > self.freeze_timeout_sec:
                        self._log(f"Stream freeze detected! No data received for {self.freeze_timeout_sec}s", "ERROR")
                        candidate.fail_count += 1
                        return False if written_for_this_session == 0 else True
                    time.sleep(0.2)

        return written_for_this_session > 0

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
            direct_cmd = self._build_ffmpeg_cmd(candidate.m3u8_url, candidate.referer, candidate.user_agent)
            
            success = self._stream_ffmpeg_process(direct_cmd, candidate)

            # Clean up FFmpeg process
            self._reap_ffmpeg()

            # 3. Fallback Mode: If Direct Mode failed without yielding data, attempt hls-proxy-stream
            if not success and not self._stop_event.is_set() and not self._force_failover_flag:
                self._log(f"[Direct Mode Failed] Falling back to hls-proxy-stream for {candidate.name}...", "WARN")
                proxy_url = self.start_proxy(candidate)
                proxy_cmd = self._build_ffmpeg_cmd(proxy_url)
                
                success = self._stream_ffmpeg_process(proxy_cmd, candidate)

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

            # If stream ended or failed, check if we need to failover to next candidate
            if forced or not success:
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
                    self._log("All stream candidates exhausted!", "ERROR")
                    self.status = "failed"
                    break
            else:
                # Stream completed naturally
                break

        if self.status != "failed":
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

    def get_filesize_mb(self) -> float:
        if self.output_filepath.exists():
            return round(self.output_filepath.stat().st_size / (1024 * 1024), 2)
        return 0.0

    def get_status_summary(self) -> Dict[str, Any]:
        return {
            "id": self.recording_id,
            "status": self.status,
            "is_running": self.is_running,
            "output_file": str(self.output_filepath),
            "output_filename": self.output_filepath.name,
            "filesize_mb": self.get_filesize_mb(),
            "bytes_written": self.bytes_written,
            "elapsed_seconds": self.get_elapsed_seconds(),
            "current_candidate": self.current_candidate_index + 1,
            "total_candidates": len(self.candidates),
            "candidates": [c.to_dict() for c in self.candidates],
            "logs": self.log_history[-30:]
        }
