#!/usr/bin/env python3
"""
Core Failover Engine Module - Stream Failover Studio
Manages multi-candidate m3u8/HLS recordings with dynamic header detection,
automatic freeze/crash failover, and continuous binary segment appending.
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

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("FailoverEngine")


class CandidateStream:
    def __init__(self, url: str, name: str = "Stream"):
        self.url = url.strip()
        self.name = name
        self.m3u8_url: str = ""
        self.referer: str = ""
        self.user_agent: str = ""
        self.slug: str = ""
        self.detected: bool = False
        self.fail_count: int = 0
        self.last_error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url": self.url,
            "name": self.name,
            "m3u8_url": self.m3u8_url,
            "referer": self.referer,
            "detected": self.detected,
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

        self.current_candidate_index: int = 0
        self.is_running: bool = False
        self.is_stopped: bool = False
        self.status: str = "initialized"  # initialized, detecting, recording, failing_over, completed, failed
        self.start_time: Optional[float] = None
        self.stop_time: Optional[float] = None
        self.bytes_written: int = 0
        self.log_history: List[str] = []

        self._thread: Optional[threading.Thread] = None
        self._monitor_thread: Optional[threading.Thread] = None
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
            self._log("detect-headers script not found. Using raw URL.", "WARN")
            candidate.m3u8_url = candidate.url
            candidate.slug = f"cand_{id(candidate)}"
            candidate.detected = True
            return True

        self._log(f"Detecting headers for {candidate.name}: {candidate.url[:70]}...")
        cmd = [
            sys.executable,
            self.detect_headers_path,
            candidate.url,
            "--json"
        ]
        if ".m3u8" in candidate.url.split("?")[0].lower():
            cmd.append("--direct")

        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                data = json.loads(res.stdout.strip())
                candidate.m3u8_url = data.get("m3u8_url", candidate.url)
                candidate.referer = data.get("referer", "")
                candidate.user_agent = data.get("user_agent", "")
                candidate.slug = data.get("slug", f"cand_{self.current_candidate_index}")
                candidate.detected = True
                self._log(f"Header detection successful for {candidate.name}. m3u8: {candidate.m3u8_url[:60]}...")
                return True
            else:
                candidate.last_error = res.stderr.strip() or "Detection failed"
                self._log(f"Header detection failed for {candidate.name}: {candidate.last_error}", "WARN")
        except Exception as e:
            candidate.last_error = str(e)
            self._log(f"Header detection error for {candidate.name}: {e}", "WARN")

        # Fallback: assume URL is m3u8 directly if detection failed
        candidate.m3u8_url = candidate.url
        candidate.slug = f"cand_{self.current_candidate_index}"
        candidate.detected = True
        return True

    def start_proxy(self, candidate: CandidateStream) -> Optional[str]:
        """Start dynamic hls-proxy instance for candidate stream."""
        if not self.hls_proxy_path or not os.path.exists(self.hls_proxy_path):
            self._log("hls-proxy script not found. Using direct candidate URL.", "WARN")
            return candidate.m3u8_url

        port = self.base_port
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

        self._log(f"Starting hls-proxy on port {port} for {candidate.name}...")
        try:
            self._proxy_process = subprocess.Popen(
                [sys.executable, self.hls_proxy_path],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            time.sleep(1.5)  # Wait for proxy to bind port
            proxy_url = f"http://127.0.0.1:{port}/channel/{candidate.slug}"
            return proxy_url
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

    def start_recording(self):
        """Start the recording thread."""
        if self.is_running:
            return
        self.is_running = True
        self.status = "recording"
        self.start_time = time.time()
        self.output_filepath.parent.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._recording_loop, daemon=True)
        self._thread.start()

    def force_failover(self):
        """User or API trigger to manually failover to next candidate."""
        self._log("Manual force-failover requested!", "WARN")
        self._force_failover_flag = True
        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
            except Exception:
                pass

    def stop(self):
        """Gracefully stop recording."""
        self._log("Stopping recorder gracefully...")
        self._stop_event.set()
        self.is_running = False
        self.status = "completed"
        self.stop_time = time.time()

        if self._ffmpeg_process:
            try:
                self._ffmpeg_process.terminate()
                self._ffmpeg_process.wait(timeout=3)
            except Exception:
                try:
                    self._ffmpeg_process.kill()
                except Exception:
                    pass
            self._ffmpeg_process = None

        self.stop_proxy()

    def _recording_loop(self):
        """Main recording & failover monitoring loop."""
        while not self._stop_event.is_set() and self.current_candidate_index < len(self.candidates):
            candidate = self.candidates[self.current_candidate_index]
            self._log(f"=== Active Stream: Candidate {self.current_candidate_index+1}/{len(self.candidates)} ({candidate.name}) ===")

            # 1. Detect headers
            self.detect_candidate_headers(candidate)

            # 2. Start Proxy
            stream_input_url = self.start_proxy(candidate)

            # 3. Spawn FFmpeg & append output to destination .ts file
            ffmpeg_cmd = [
                self.ffmpeg_path or "ffmpeg",
                "-y",
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_delay_max", "5",
                "-rw_timeout", "15000000",
                "-i", stream_input_url,
                "-c", "copy",
                "-f", "mpegts",
                "pipe:1"
            ]

            self._log(f"Launching FFmpeg stream fetcher for {candidate.name}...")
            
            try:
                # Open main file in append binary mode ("ab")
                with open(self.output_filepath, "ab") as out_f:
                    self._ffmpeg_process = subprocess.Popen(
                        ffmpeg_cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        bufsize=65536
                    )

                    last_write_time = time.time()
                    self._force_failover_flag = False

                    # Monitor output chunk writing
                    while not self._stop_event.is_set():
                        if self._force_failover_flag:
                            self._log(f"Forced failover triggered on {candidate.name}!", "WARN")
                            break

                        # Read binary segment chunk
                        chunk = self._ffmpeg_process.stdout.read(32768)
                        if chunk:
                            out_f.write(chunk)
                            out_f.flush()
                            self.bytes_written += len(chunk)
                            last_write_time = time.time()
                        else:
                            # stdout EOF or ffmpeg exited
                            ret_code = self._ffmpeg_process.poll()
                            if ret_code is not None:
                                self._log(f"FFmpeg process exited with code {ret_code} for {candidate.name}", "WARN")
                                break
                            
                            # Check freeze / stale timeout
                            if (time.time() - last_write_time) > self.freeze_timeout_sec:
                                self._log(f"STREAM FREEZE DETECTED! No data received for {self.freeze_timeout_sec}s on {candidate.name}", "ERROR")
                                candidate.fail_count += 1
                                break
                            time.sleep(0.2)

            except Exception as e:
                self._log(f"Error during ffmpeg streaming for {candidate.name}: {e}", "ERROR")
                candidate.fail_count += 1

            # Cleanup current candidate FFmpeg and Proxy
            if self._ffmpeg_process:
                try:
                    self._ffmpeg_process.terminate()
                    self._ffmpeg_process.wait(timeout=2)
                except Exception:
                    pass
                self._ffmpeg_process = None

            self.stop_proxy()

            if self._stop_event.is_set():
                break

            # Move to next candidate for failover
            self.current_candidate_index += 1
            if self.current_candidate_index < len(self.candidates):
                self.status = "failing_over"
                self._log(f"Failing over to Candidate {self.current_candidate_index+1}...", "WARN")
                time.sleep(1)
            else:
                self._log("All stream candidates exhausted!", "ERROR")
                self.status = "failed"
                break

        if self.status != "failed":
            self.status = "completed"
        self.is_running = False
        self.stop_time = time.time()
        self._log(f"Recorder finished. Total bytes recorded: {self.bytes_written} bytes ({self.get_filesize_mb():.2f} MB)")

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
