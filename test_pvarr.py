#!/usr/bin/env python3
"""
PVArr unit test suite.

Stdlib-only (unittest) so the tests run with no extra dependencies beyond
what requirements.txt already installs. Tests that genuinely need FFmpeg on
the host are skipped rather than failed when it is absent.

Run:
    python3 test_pvarr.py
    python3 -m unittest discover -v
"""

import logging
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from app import check_deps, tuner
from app.naming import (
    StorageManager,
    generate_sports_filename,
    sanitize_token,
)
from app.post_processor import remux_recording
from app.recorder import CandidateStream, StreamFailoverRecorder

# The modules log at INFO on import; silence it so test output stays readable.
logging.disable(logging.CRITICAL)

HAS_FFMPEG = bool(check_deps.find_executable("ffmpeg"))


# --------------------------------------------------------------------------
# naming.sanitize_token
# --------------------------------------------------------------------------
class TestSanitizeToken(unittest.TestCase):
    def test_spaces_become_underscores(self):
        self.assertEqual(sanitize_token("Kansas City Chiefs"), "Kansas_City_Chiefs")

    def test_path_separators_are_stripped(self):
        # Critical: these values land in a filename, so a token that survives
        # with a "/" in it would let a caller escape the recordings directory.
        self.assertEqual(sanitize_token("../../etc/passwd"), "etc_passwd")
        self.assertNotIn("/", sanitize_token("a/b/c"))
        self.assertNotIn("\\", sanitize_token("a\\b\\c"))

    def test_empty_and_whitespace_use_fallback(self):
        self.assertEqual(sanitize_token("", "Fallback"), "Fallback")
        self.assertEqual(sanitize_token("   ", "Fallback"), "Fallback")

    def test_all_punctuation_uses_fallback(self):
        self.assertEqual(sanitize_token("!!!", "Fallback"), "Fallback")

    def test_leading_trailing_underscores_trimmed(self):
        self.assertEqual(sanitize_token("  Lakers  "), "Lakers")

    def test_alphanumeric_preserved(self):
        self.assertEqual(sanitize_token("49ers"), "49ers")
        self.assertEqual(sanitize_token("Team-A_1"), "Team-A_1")


# --------------------------------------------------------------------------
# naming.generate_sports_filename
# --------------------------------------------------------------------------
class TestGenerateSportsFilename(unittest.TestCase):
    def test_standard_format(self):
        name = generate_sports_filename(
            "NFL", "Chiefs", "Bills", "1080p", date_str="2026-01-15"
        )
        self.assertEqual(name, "2026-01-15_NFL_Chiefs_vs_Bills_1080p.ts")

    def test_extension_normalised(self):
        # A caller passing ".mkv" must not produce a double dot.
        name = generate_sports_filename(
            "NFL", "A", "B", "720p", date_str="2026-01-15", ext=".mkv"
        )
        self.assertTrue(name.endswith("_720p.mkv"))
        self.assertNotIn("..", name)

    def test_missing_teams_fall_back(self):
        name = generate_sports_filename("", "", "", date_str="2026-01-15")
        self.assertEqual(name, "2026-01-15_Sports_TeamA_vs_TeamB_1080p.ts")

    def test_date_defaults_to_today(self):
        name = generate_sports_filename("NFL", "A", "B")
        # YYYY-MM-DD prefix
        self.assertRegex(name, r"^\d{4}-\d{2}-\d{2}_NFL_A_vs_B_1080p\.ts$")

    def test_dirty_input_yields_safe_filename(self):
        name = generate_sports_filename(
            "NFL/../", "A B", "C:D", date_str="2026-01-15"
        )
        self.assertNotIn("/", name)
        self.assertNotIn(":", name)


# --------------------------------------------------------------------------
# naming.StorageManager
# --------------------------------------------------------------------------
class TestStorageManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.mgr = StorageManager(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_creates_directory(self):
        self.assertTrue(Path(self.tmp).is_dir())

    def test_output_path_inside_record_dir(self):
        path = self.mgr.get_output_path("NFL", "A", "B")
        self.assertEqual(path.parent, Path(self.tmp).resolve())

    def test_collision_avoidance(self):
        first = self.mgr.get_output_path("NFL", "A", "B")
        first.touch()
        second = self.mgr.get_output_path("NFL", "A", "B")
        self.assertNotEqual(first, second)
        self.assertTrue(second.stem.endswith("_1"))

        second.touch()
        third = self.mgr.get_output_path("NFL", "A", "B")
        self.assertNotIn(third, (first, second))

    def test_list_recordings_only_returns_ts(self):
        (Path(self.tmp) / "a.ts").write_bytes(b"x" * 2048)
        (Path(self.tmp) / "b.mkv").write_bytes(b"x" * 2048)
        (Path(self.tmp) / "c.txt").write_text("not a recording")

        names = [r["filename"] for r in self.mgr.list_recordings()]
        self.assertEqual(names, ["a.ts"])

    def test_list_recordings_metadata(self):
        (Path(self.tmp) / "a.ts").write_bytes(b"x" * (1024 * 1024))
        rec = self.mgr.list_recordings()[0]
        for key in ("filename", "filepath", "size_mb", "created_at", "modified_timestamp"):
            self.assertIn(key, rec)
        self.assertAlmostEqual(rec["size_mb"], 1.0, places=1)

    def test_list_recordings_missing_dir_is_empty(self):
        self.assertEqual(self.mgr.list_recordings("/nonexistent/pvarr/path"), [])

    def test_rename_appends_ts_extension(self):
        (Path(self.tmp) / "old.ts").write_bytes(b"x")
        self.assertTrue(self.mgr.rename_recording("old.ts", "new"))
        self.assertTrue((Path(self.tmp) / "new.ts").exists())

    def test_rename_refuses_to_clobber(self):
        (Path(self.tmp) / "old.ts").write_bytes(b"old")
        (Path(self.tmp) / "new.ts").write_bytes(b"new")
        self.assertFalse(self.mgr.rename_recording("old.ts", "new.ts"))
        # Both survive, and the existing file is untouched.
        self.assertEqual((Path(self.tmp) / "new.ts").read_bytes(), b"new")

    def test_rename_missing_source_returns_false(self):
        self.assertFalse(self.mgr.rename_recording("ghost.ts", "new.ts"))

    def test_delete(self):
        (Path(self.tmp) / "a.ts").write_bytes(b"x")
        self.assertTrue(self.mgr.delete_recording("a.ts"))
        self.assertFalse((Path(self.tmp) / "a.ts").exists())

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.mgr.delete_recording("ghost.ts"))


# --------------------------------------------------------------------------
# tuner
# --------------------------------------------------------------------------
class TestTuner(unittest.TestCase):
    def setUp(self):
        self.sessions = [
            {"id": "rec1", "output_filename": "game1.ts", "is_running": True},
            {"id": "rec2", "output_filename": "game2.ts", "is_running": False},
            {"id": "rec3", "output_filename": "game3.ts", "is_running": True},
        ]

    def test_playlist_header(self):
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        self.assertTrue(out.startswith("#EXTM3U"))

    def test_playlist_excludes_stopped_sessions(self):
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        self.assertIn("game1.ts", out)
        self.assertIn("game3.ts", out)
        self.assertNotIn("game2.ts", out)

    def test_playlist_stream_urls(self):
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        self.assertIn("http://host:8999/api/recordings/rec1/stream", out)

    def test_playlist_strips_trailing_slash(self):
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999/")
        self.assertIn("http://host:8999/api/recordings/rec1/stream", out)
        self.assertNotIn("8999//api", out)

    def test_empty_playlist_still_valid(self):
        self.assertEqual(tuner.generate_m3u_playlist([], "http://host:8999"), "#EXTM3U")

    def test_epg_is_wellformed_xml(self):
        import xml.etree.ElementTree as ET
        xml = tuner.generate_xmltv_epg(self.sessions)
        # Strip the DOCTYPE, which ElementTree will not parse.
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        root = ET.fromstring(body)
        self.assertEqual(root.tag, "tv")
        self.assertEqual(len(root.findall("channel")), 3)

    def test_epg_empty_is_wellformed(self):
        import xml.etree.ElementTree as ET
        xml = tuner.generate_xmltv_epg([])
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        self.assertEqual(ET.fromstring(body).tag, "tv")


# --------------------------------------------------------------------------
# check_deps
# --------------------------------------------------------------------------
class TestCheckDeps(unittest.TestCase):
    def test_finds_binary_on_path(self):
        self.assertTrue(check_deps.find_executable("sh"))

    def test_missing_returns_empty_string(self):
        self.assertEqual(
            check_deps.find_executable("pvarr-definitely-not-a-real-binary-xyz"), ""
        )

    def test_alt_names_are_tried(self):
        self.assertTrue(check_deps.find_executable("pvarr-nope-xyz", ["sh"]))

    def test_check_dependencies_shape(self):
        res = check_deps.check_dependencies(verbose=False)
        self.assertIn("status", res)
        self.assertIn("dependencies", res)
        for tool in ("ffmpeg", "ffprobe", "hls-proxy", "detect-headers"):
            self.assertIn(tool, res["dependencies"])

    def test_optional_tools_do_not_affect_status(self):
        # status must reflect only ffmpeg/ffprobe; hls-proxy is optional.
        res = check_deps.check_dependencies(verbose=False)
        expected = bool(res["dependencies"]["ffmpeg"]) and bool(res["dependencies"]["ffprobe"])
        self.assertEqual(res["status"], expected)


# --------------------------------------------------------------------------
# recorder.CandidateStream
# --------------------------------------------------------------------------
class TestCandidateStream(unittest.TestCase):
    def test_url_is_stripped(self):
        self.assertEqual(CandidateStream("  http://x/s.m3u8  ").url, "http://x/s.m3u8")

    def test_defaults(self):
        c = CandidateStream("http://x/s.m3u8")
        self.assertFalse(c.detected)
        self.assertFalse(c.used_proxy)
        self.assertEqual(c.fail_count, 0)
        self.assertTrue(c.user_agent)

    def test_to_dict_keys(self):
        d = CandidateStream("http://x/s.m3u8", name="Primary").to_dict()
        for key in ("url", "name", "m3u8_url", "referer", "user_agent",
                    "detected", "used_proxy", "fail_count", "last_error"):
            self.assertIn(key, d)
        self.assertEqual(d["name"], "Primary")


# --------------------------------------------------------------------------
# recorder.StreamFailoverRecorder  (construction / command building only —
# no subprocesses are spawned by these tests)
# --------------------------------------------------------------------------
class TestRecorderConstruction(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.out = str(Path(self.tmp) / "out.ts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, candidates):
        return StreamFailoverRecorder("test-id", candidates, self.out)

    def test_three_stage_failover_candidates(self):
        rec = self._rec(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"])
        self.assertEqual(len(rec.candidates), 3)
        self.assertEqual(rec.candidates[0].name, "Candidate 1")
        self.assertEqual(rec.candidates[2].name, "Candidate 3")

    def test_blank_candidates_are_dropped(self):
        # The dashboard submits two backup fields whether or not they are
        # filled in, so empty strings must not become real candidates.
        rec = self._rec(["http://a/1.m3u8", "", "   ", None])
        self.assertEqual(len(rec.candidates), 1)

    def test_initial_state(self):
        rec = self._rec(["http://a/1.m3u8"])
        self.assertFalse(rec.is_running)
        self.assertEqual(rec.status, "initialized")
        self.assertEqual(rec.current_candidate_index, 0)
        self.assertIsNone(rec.start_time)

    def test_output_dir_not_created_until_start(self):
        nested = str(Path(self.tmp) / "sub" / "out.ts")
        StreamFailoverRecorder("test-id", ["http://a/1.m3u8"], nested)
        self.assertFalse((Path(self.tmp) / "sub").exists())

    def test_status_summary_shape(self):
        rec = self._rec(["http://a/1.m3u8", "http://b/2.m3u8"])
        s = rec.get_status_summary()
        for key in ("id", "status", "is_running", "output_file", "output_filename",
                    "filesize_mb", "bytes_written", "elapsed_seconds",
                    "current_candidate", "total_candidates", "candidates", "logs"):
            self.assertIn(key, s)
        self.assertEqual(s["total_candidates"], 2)
        self.assertEqual(s["current_candidate"], 1)  # 1-indexed for display

    def test_elapsed_is_zero_before_start(self):
        self.assertEqual(self._rec(["http://a/1.m3u8"]).get_elapsed_seconds(), 0.0)

    def test_filesize_zero_when_absent(self):
        self.assertEqual(self._rec(["http://a/1.m3u8"]).get_filesize_mb(), 0.0)

    def test_log_history_is_capped(self):
        rec = self._rec(["http://a/1.m3u8"])
        for i in range(600):
            rec._log(f"line {i}")
        self.assertLessEqual(len(rec.log_history), 500)
        self.assertIn("line 599", rec.log_history[-1])

    def test_status_summary_truncates_logs(self):
        rec = self._rec(["http://a/1.m3u8"])
        for i in range(100):
            rec._log(f"line {i}")
        self.assertLessEqual(len(rec.get_status_summary()["logs"]), 30)


class TestFFmpegCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.rec = StreamFailoverRecorder(
            "test-id", ["http://a/1.m3u8"], str(Path(self.tmp) / "out.ts")
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_remux_not_transcode(self):
        # "-c copy" is the whole point: PVArr must never re-encode.
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], "copy")

    def test_input_url_follows_dash_i(self):
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8")
        self.assertEqual(cmd[cmd.index("-i") + 1], "http://a/1.m3u8")

    def test_reconnect_flags_present(self):
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8")
        for flag in ("-reconnect", "-reconnect_streamed", "-reconnect_delay_max"):
            self.assertIn(flag, cmd)

    def test_headers_omitted_when_not_supplied(self):
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8")
        self.assertNotIn("-headers", cmd)

    def test_referer_and_user_agent_injected(self):
        cmd = self.rec._build_ffmpeg_cmd(
            "http://a/1.m3u8", referer="http://site/", user_agent="UA/1.0"
        )
        self.assertIn("-headers", cmd)
        headers = cmd[cmd.index("-headers") + 1]
        self.assertIn("Referer: http://site/", headers)
        self.assertIn("User-Agent: UA/1.0", headers)
        self.assertTrue(headers.endswith("\r\n"))

    def test_referer_only(self):
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8", referer="http://site/")
        headers = cmd[cmd.index("-headers") + 1]
        self.assertIn("Referer:", headers)
        self.assertNotIn("User-Agent:", headers)

    def test_command_is_argv_list_not_shell_string(self):
        # Guards against reintroducing shell interpolation of scraped URLs.
        cmd = self.rec._build_ffmpeg_cmd("http://a/1.m3u8?token=a&b=c")
        self.assertIsInstance(cmd, list)
        self.assertTrue(all(isinstance(part, str) for part in cmd))
        self.assertIn("http://a/1.m3u8?token=a&b=c", cmd)


# --------------------------------------------------------------------------
# post_processor
# --------------------------------------------------------------------------
class TestPostProcessor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_missing_source_fails_cleanly(self):
        res = remux_recording(str(Path(self.tmp) / "ghost.ts"))
        self.assertEqual(res["status"], "failed")
        self.assertIn("error", res)

    def test_empty_source_fails_cleanly(self):
        empty = Path(self.tmp) / "empty.ts"
        empty.touch()
        res = remux_recording(str(empty))
        self.assertEqual(res["status"], "failed")
        # An empty file must never be deleted as if it had been converted.
        self.assertTrue(empty.exists())

    def test_garbage_source_does_not_delete_original(self):
        junk = Path(self.tmp) / "junk.ts"
        junk.write_bytes(b"not a transport stream")
        res = remux_recording(str(junk), delete_source=True)
        self.assertEqual(res["status"], "failed")
        self.assertTrue(junk.exists(), "source deleted despite failed remux")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_real_remux_roundtrip(self):
        import subprocess
        src = Path(self.tmp) / "sample.ts"
        # 1 second of black video + silence, encoded to MPEG-TS.
        subprocess.run(
            [check_deps.find_executable("ffmpeg"), "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
             "-t", "1", "-c:v", "libx264", "-c:a", "aac",
             "-f", "mpegts", str(src)],
            check=True, capture_output=True, timeout=60,
        )
        self.assertTrue(src.exists() and src.stat().st_size > 0)

        res = remux_recording(str(src), target_format="mkv", delete_source=False)
        self.assertEqual(res["status"], "success", res.get("error"))
        self.assertTrue(Path(res["output_filepath"]).exists())
        self.assertTrue(res["output_filename"].endswith(".mkv"))
        self.assertTrue(src.exists(), "delete_source=False must keep the source")

    @unittest.skipUnless(HAS_FFMPEG, "ffmpeg not installed")
    def test_delete_source_removes_original_on_success(self):
        import subprocess
        src = Path(self.tmp) / "sample2.ts"
        subprocess.run(
            [check_deps.find_executable("ffmpeg"), "-y", "-v", "error",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:d=1",
             "-t", "1", "-c:v", "libx264",
             "-f", "mpegts", str(src)],
            check=True, capture_output=True, timeout=60,
        )
        res = remux_recording(str(src), target_format="mkv", delete_source=True)
        self.assertEqual(res["status"], "success", res.get("error"))
        self.assertFalse(src.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
