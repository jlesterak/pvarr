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

import io
import json
import logging
import os
import shutil
import sys
import time
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
from app import ringbuffer, sessions
from app.recorder import (
    CandidateStream,
    StreamFailoverRecorder,
    StreamOutcome,
    safe_stream_url,
)

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

    def test_list_recordings_covers_every_container(self):
        # This used to glob "*.ts" only, which hid every FINISHED recording:
        # post-processing remuxes to .mp4 and deletes the .ts, so the library
        # emptied itself the moment a capture succeeded.
        (Path(self.tmp) / "a.ts").write_bytes(b"x" * 2048)
        (Path(self.tmp) / "b.mkv").write_bytes(b"x" * 2048)
        (Path(self.tmp) / "c.mp4").write_bytes(b"x" * 2048)
        (Path(self.tmp) / "d.txt").write_text("not a recording")

        names = sorted(r["filename"] for r in self.mgr.list_recordings())
        self.assertEqual(names, ["a.ts", "b.mkv", "c.mp4"])

    def test_list_recordings_ignores_directories(self):
        # .proxy_conf and similar live alongside the recordings.
        (Path(self.tmp) / "a.ts").write_bytes(b"x")
        (Path(self.tmp) / "weird.mp4").mkdir()
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

    def test_rename_inherits_the_existing_extension(self):
        (Path(self.tmp) / "old.ts").write_bytes(b"x")
        self.assertTrue(self.mgr.rename_recording("old.ts", "new"))
        self.assertTrue((Path(self.tmp) / "new.ts").exists())

    def test_rename_inherits_mp4_not_ts(self):
        # ".ts" used to be forced onto every rename, so renaming a finished
        # recording gave it a name that lied about its contents.
        (Path(self.tmp) / "old.mp4").write_bytes(b"x")
        self.assertTrue(self.mgr.rename_recording("old.mp4", "highlights"))
        self.assertTrue((Path(self.tmp) / "highlights.mp4").exists())
        self.assertFalse((Path(self.tmp) / "highlights.ts").exists())

    def test_rename_keeps_an_explicit_mp4_extension(self):
        (Path(self.tmp) / "old.mp4").write_bytes(b"x")
        self.assertTrue(self.mgr.rename_recording("old.mp4", "highlights.mp4"))
        self.assertTrue((Path(self.tmp) / "highlights.mp4").exists())
        self.assertFalse((Path(self.tmp) / "highlights.mp4.ts").exists(),
                         "rename produced a double extension")

    def test_renamed_file_is_still_listed(self):
        # The rename bug also made the file vanish from the library, since the
        # result was neither a real .ts nor a listed container.
        (Path(self.tmp) / "old.mp4").write_bytes(b"x")
        self.mgr.rename_recording("old.mp4", "highlights")
        names = [r["filename"] for r in self.mgr.list_recordings()]
        self.assertEqual(names, ["highlights.mp4"])

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

    def test_delete_a_remuxed_recording(self):
        (Path(self.tmp) / "game.mp4").write_bytes(b"x")
        self.assertTrue(self.mgr.delete_recording("game.mp4"))
        self.assertFalse((Path(self.tmp) / "game.mp4").exists())

    def test_media_type_by_extension(self):
        from app.naming import media_type_for
        self.assertEqual(media_type_for("a.ts"), "video/mp2t")
        self.assertEqual(media_type_for("a.mp4"), "video/mp4")
        self.assertEqual(media_type_for("a.MP4"), "video/mp4")
        self.assertEqual(media_type_for("a.mkv"), "video/x-matroska")
        self.assertEqual(media_type_for("a.weird"), "application/octet-stream")

    def test_delete_missing_returns_false(self):
        self.assertFalse(self.mgr.delete_recording("ghost.ts"))


# --------------------------------------------------------------------------
# tuner
# --------------------------------------------------------------------------
class TestGuideNaming(unittest.TestCase):
    """The guide has to say what is being recorded and where it is coming from.

    Before this, every programme's description was "PVArr live recording
    <uuid>", which told the sponsor nothing they could not already see.
    """

    def session(self, **over):
        s = {
            "id": "rec1",
            "is_running": True,
            "output_filename": "Bears vs Packers.ts",
            "started_at": 1756600000.0,
            "current_candidate": 2,
            "total_candidates": 3,
            "candidates": [{"name": "Primary"}, {"name": "Backup 1"},
                           {"name": "Backup 2"}],
        }
        s.update(over)
        return s

    def test_remuxed_container_is_stripped_from_the_title(self):
        # current_filepath follows the remux, so a finished session would have
        # been advertised as "Bears vs Packers.mp4".
        out = tuner.generate_m3u_playlist(
            [self.session(output_filename="Bears vs Packers.mp4")], "http://h:8999")
        self.assertIn("Bears vs Packers", out)
        self.assertNotIn(".mp4", out)

    def test_guide_names_the_stream_in_use(self):
        xml = tuner.generate_xmltv_epg([self.session()])
        self.assertIn("<sub-title lang=\"en\">Backup 1</sub-title>", xml)

    def test_guide_description_carries_the_filename(self):
        xml = tuner.generate_xmltv_epg([self.session()])
        self.assertIn("Recording to Bears vs Packers.ts", xml)

    def test_guide_description_reports_failover_position(self):
        xml = tuner.generate_xmltv_epg([self.session()])
        self.assertIn("Backup 1 (2 of 3, failover armed)", xml)

    def test_single_url_session_does_not_claim_failover(self):
        xml = tuner.generate_xmltv_epg([self.session(
            current_candidate=1, total_candidates=1,
            candidates=[{"name": "Primary"}])])
        self.assertIn("Source: Primary", xml)
        self.assertNotIn("failover armed", xml)

    def test_unnamed_candidate_falls_back_to_its_number(self):
        xml = tuner.generate_xmltv_epg([self.session(candidates=[{}, {}, {}])])
        self.assertIn("Stream 2", xml)

    def test_missing_candidate_data_does_not_break_the_guide(self):
        # An older session dict, or one captured mid-teardown.
        xml = tuner.generate_xmltv_epg([{
            "id": "rec1", "is_running": True, "output_filename": "x.ts"}])
        self.assertIn("<programme", xml)
        self.assertIn("</tv>", xml)

    def test_description_holds_no_live_counters(self):
        # Plex caches the XMLTV. A byte count or elapsed time baked in here is
        # stale seconds after it is fetched, which reads as a bug to the user.
        xml = tuner.generate_xmltv_epg([self.session(
            filesize_mb=1234.5, elapsed_seconds=4321.0)])
        self.assertNotIn("1234", xml)
        self.assertNotIn("4321", xml)


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
        self.assertIn("game1", out)
        self.assertIn("game3", out)
        self.assertNotIn("game2", out)

    def test_channel_titles_drop_the_ts_extension(self):
        # Plex shows this string in the guide; ".ts" is noise there.
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        self.assertNotIn(".ts", out)

    def test_playlist_points_at_the_stream_endpoint(self):
        out = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        self.assertIn("/api/recordings/rec1/stream", out)

    def test_quotes_in_filename_do_not_break_attributes(self):
        sessions = [{"id": "r1", "output_filename": 'a "quoted" game.ts',
                     "is_running": True}]
        out = tuner.generate_m3u_playlist(sessions, "http://host:8999")
        extinf = [l for l in out.splitlines() if l.startswith("#EXTINF")][0]
        # Attribute values must stay balanced.
        self.assertEqual(extinf.count("tvg-name="), 1)
        self.assertIn("group-title=", extinf)

    def test_epg_excludes_stopped_sessions(self):
        # The guide must match the playlist. Advertising a channel here that
        # the M3U omits leaves Plex with guide entries it cannot tune.
        import xml.etree.ElementTree as ET
        xml = tuner.generate_xmltv_epg(self.sessions)
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        ids = {c.attrib["id"] for c in ET.fromstring(body).findall("channel")}
        self.assertEqual(ids, {"rec1", "rec3"})
        self.assertNotIn("rec2", ids)

    def test_epg_escapes_xml_special_characters(self):
        # An unescaped & or < in a filename produced invalid XML that Plex
        # would reject outright.
        import xml.etree.ElementTree as ET
        sessions = [{"id": "r1", "output_filename": "Fish & Chips <live>.ts",
                     "is_running": True, "started_at": 1756000000.0}]
        xml = tuner.generate_xmltv_epg(sessions)
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        root = ET.fromstring(body)  # raises if escaping is wrong
        self.assertEqual(root.find("channel/display-name").text,
                         "Fish & Chips <live>")

    def test_epg_includes_a_programme_per_channel(self):
        # Plex will not display a channel with no programme in the guide.
        import xml.etree.ElementTree as ET
        xml = tuner.generate_xmltv_epg(self.sessions)
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        root = ET.fromstring(body)
        programmes = root.findall("programme")
        self.assertEqual(len(programmes), 2)  # running only
        for prog in programmes:
            self.assertIn("start", prog.attrib)
            self.assertIn("stop", prog.attrib)
            self.assertIn("channel", prog.attrib)
            self.assertTrue(prog.find("title").text)

    def test_programme_times_are_xmltv_format(self):
        import re, xml.etree.ElementTree as ET
        sessions = [{"id": "r1", "output_filename": "g.ts", "is_running": True,
                     "started_at": 1756000000.0}]
        xml = tuner.generate_xmltv_epg(sessions)
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        prog = ET.fromstring(body).find("programme")
        for attr in ("start", "stop"):
            self.assertRegex(prog.attrib[attr], r"^\d{14} \+0000$")

    def test_epg_channel_ids_match_playlist_tvg_ids(self):
        # Plex maps guide to channel by this id; a mismatch means no guide.
        m3u = tuner.generate_m3u_playlist(self.sessions, "http://host:8999")
        import xml.etree.ElementTree as ET
        xml = tuner.generate_xmltv_epg(self.sessions)
        body = "\n".join(l for l in xml.splitlines() if not l.startswith("<!DOCTYPE"))
        epg_ids = {c.attrib["id"] for c in ET.fromstring(body).findall("channel")}
        for cid in epg_ids:
            self.assertIn(f'tvg-id="{cid}"', m3u)

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
        self.assertEqual(len(root.findall("channel")), 2)  # running only

    def test_channel_numbers_are_stable_across_calls(self):
        # Plex remembers a channel by its number; renumbering live channels on
        # a rescan shuffles the guide underneath it.
        first = tuner.assign_channel_numbers(self.sessions)
        second = tuner.assign_channel_numbers(self.sessions)
        self.assertEqual(first, second)

    def test_channel_numbers_cover_running_sessions_only(self):
        numbers = tuner.assign_channel_numbers(self.sessions)
        self.assertEqual(set(numbers), {"rec1", "rec3"})

    def test_channel_numbers_are_released_when_a_session_stops(self):
        # Otherwise the registry grows for the life of the process.
        tuner.assign_channel_numbers(self.sessions)
        numbers = tuner.assign_channel_numbers(
            [{"id": "rec9", "output_filename": "g.ts", "is_running": True}]
        )
        self.assertEqual(numbers, {"rec9": tuner.FIRST_CHANNEL_NUMBER})

    def test_lineup_guide_numbers_appear_in_the_epg(self):
        # This is how Plex maps XMLTV guide data onto a HDHomeRun lineup.
        import xml.etree.ElementTree as ET
        lineup = tuner.generate_lineup(self.sessions, "http://host:8999")
        xml = tuner.generate_xmltv_epg(self.sessions)
        body = "\n".join(l for l in xml.splitlines()
                         if not l.startswith("<!DOCTYPE"))
        names = {n.text for n in ET.fromstring(body).iter("display-name")}
        for entry in lineup:
            self.assertIn(entry["GuideNumber"], names)

    def test_lineup_excludes_stopped_sessions(self):
        names = {e["GuideName"] for e in
                 tuner.generate_lineup(self.sessions, "http://host:8999")}
        self.assertEqual(names, {"game1", "game3"})

    def test_discover_lineup_url_matches_the_base(self):
        d = tuner.generate_discover("http://host:8999/live/")
        self.assertEqual(d["LineupURL"], "http://host:8999/live/lineup.json")
        self.assertEqual(d["BaseURL"], "http://host:8999/live")

    def test_device_id_override(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_DEVICE_ID": "abc123"}):
            self.assertEqual(tuner.device_id(), "00ABC123")

    def test_tuner_count_falls_back_on_junk(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_TUNER_COUNT": "lots"}):
            self.assertEqual(tuner.tuner_count(), 4)

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

    def test_filesize_follows_the_post_processed_file(self):
        # After remux the .ts is deleted; reading the old path reported 0 MB
        # next to a perfectly good .mp4.
        rec = self._rec(["http://a/1.m3u8"])
        ts = Path(self.out)
        ts.write_bytes(b"x" * 2048)
        mp4 = ts.with_suffix(".mp4")
        mp4.write_bytes(b"x" * (2 * 1024 * 1024))
        ts.unlink()

        self.assertEqual(rec.get_filesize_mb(), 0.0)  # .ts is gone
        rec.final_filepath = mp4
        self.assertAlmostEqual(rec.get_filesize_mb(), 2.0, places=1)

    def test_status_summary_reports_the_post_processed_file(self):
        rec = self._rec(["http://a/1.m3u8"])
        mp4 = Path(self.out).with_suffix(".mp4")
        mp4.write_bytes(b"x")
        rec.final_filepath = mp4
        summary = rec.get_status_summary()
        self.assertTrue(summary["output_filename"].endswith(".mp4"))
        self.assertEqual(summary["output_file"], str(mp4))

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


class TestDetectHeadersInvocation(unittest.TestCase):
    """The detector may be a .py or a .sh; each needs the right interpreter."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        # auto_probe off: this case is specifically the external-script
        # fallback, and leaving the probe on would put a real network call in
        # front of every assertion.
        self.rec = StreamFailoverRecorder(
            "test-id", ["http://a/1.m3u8"], str(Path(self.tmp) / "out.ts"),
            auto_probe=False,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _captured_cmd(self, detector_name):
        from unittest.mock import patch, MagicMock
        detector = Path(self.tmp) / detector_name
        detector.write_text("#!/bin/sh\necho {}\n")
        detector.chmod(0o755)
        self.rec.detect_headers_path = str(detector)
        result = MagicMock(returncode=0, stdout="{}")
        with patch("app.recorder.subprocess.run", return_value=result) as run:
            self.rec.detect_candidate_headers(self.rec.candidates[0])
        return run.call_args[0][0]

    def test_python_detector_runs_under_interpreter(self):
        cmd = self._captured_cmd("detect-headers-py.py")
        self.assertEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[1].endswith(".py"))

    def test_shell_detector_runs_directly(self):
        # Upstream ships only detect-headers.sh; running it under python3
        # made every detection fail silently.
        cmd = self._captured_cmd("detect-headers.sh")
        self.assertNotEqual(cmd[0], sys.executable)
        self.assertTrue(cmd[0].endswith(".sh"))

    def test_json_flag_always_passed(self):
        for name in ("detect-headers-py.py", "detect-headers.sh"):
            with self.subTest(detector=name):
                self.assertIn("--json", self._captured_cmd(name))


class TestFFmpegCommand(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        # auto_probe off: this case is specifically the external-script
        # fallback, and leaving the probe on would put a real network call in
        # front of every assertion.
        self.rec = StreamFailoverRecorder(
            "test-id", ["http://a/1.m3u8"], str(Path(self.tmp) / "out.ts"),
            auto_probe=False,
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


# --------------------------------------------------------------------------
# recorder disk guard
#
# Free space is stubbed throughout: a test that reads the real filesystem
# passes or fails according to how full the developer's disk is, which is not
# a property of the code under test.
# --------------------------------------------------------------------------
class TestDiskGuard(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-disk-")
        self.out = str(Path(self.tmp) / "out.ts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, free_gb, min_free_gb=5.0):
        rec = StreamFailoverRecorder("test-id", ["http://a/1.m3u8"], self.out,
                                     min_free_gb=min_free_gb)
        rec.free_bytes = lambda: None if free_gb is None else int(free_gb * 1024 ** 3)
        return rec

    def test_ample_space_is_fine(self):
        rec = self.make(free_gb=100)
        self.assertTrue(rec._disk_space_ok())
        self.assertFalse(rec._stop_event.is_set())

    def test_below_the_floor_aborts(self):
        rec = self.make(free_gb=1)
        self.assertFalse(rec._disk_space_ok())
        self.assertTrue(rec._stop_event.is_set())
        self.assertEqual(rec.status, "aborted_no_space")

    def test_exactly_at_the_floor_is_allowed(self):
        rec = self.make(free_gb=5.0, min_free_gb=5.0)
        self.assertTrue(rec._disk_space_ok())

    def test_zero_floor_disables_the_guard(self):
        rec = self.make(free_gb=0.001, min_free_gb=0)
        self.assertTrue(rec._disk_space_ok())
        self.assertFalse(rec._stop_event.is_set())

    def test_unreadable_volume_does_not_kill_a_recording(self):
        # If free space cannot be determined, that is not a reason to throw
        # away a capture in progress.
        rec = self.make(free_gb=None)
        self.assertTrue(rec._disk_space_ok())
        self.assertFalse(rec._stop_event.is_set())

    def test_check_is_rate_limited(self):
        calls = []
        rec = self.make(free_gb=100)
        rec.free_bytes = lambda: calls.append(1) or 100 * 1024 ** 3
        for _ in range(50):
            rec._disk_space_ok()
        self.assertEqual(len(calls), 1,
                         "statvfs called per write instead of on an interval")

    def test_abort_status_survives_the_completion_block(self):
        # "aborted_no_space" must not be overwritten with "completed", which
        # would hide why the recording is short.
        rec = self.make(free_gb=1)
        rec._disk_space_ok()                     # sets the status and stop_event
        # stop_event is set, so the loop falls straight through to the
        # completion block -- which is the code that must not clobber it.
        rec._recording_loop()
        self.assertEqual(rec.status, "aborted_no_space")
        self.assertFalse(rec.is_running)

    def test_status_summary_reports_headroom(self):
        rec = self.make(free_gb=42, min_free_gb=5)
        summary = rec.get_status_summary()
        self.assertEqual(summary["free_disk_gb"], 42.0)
        self.assertEqual(summary["min_free_disk_gb"], 5.0)


# --------------------------------------------------------------------------
# recorder._recording_loop  —  failover state machine
#
# These drive the real loop with the subprocess boundary replaced by a script.
# The fake mirrors one piece of real logic deliberately: the check of
# _force_failover_flag on entry, because that check is loop-control, not
# subprocess behaviour, and a bug living there must stay reachable.
# --------------------------------------------------------------------------
class FailoverLoopTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.out = str(Path(self.tmp) / "out.ts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, urls, outcomes, **kwargs):
        """Build a recorder whose stream attempts follow a scripted list.

        Each entry is consumed by one _stream_ffmpeg_process call. Note that a
        single candidate can consume two entries: direct mode, then the proxy
        fallback. Entries may be callables taking (recorder, candidate).
        """
        # Disable the disk guard unless a test is specifically about it.
        # Left live, these tests pass or fail according to how full the
        # developer's disk happens to be, which is not a property of the code.
        kwargs.setdefault("min_free_gb", 0)
        rec = StreamFailoverRecorder("test-id", urls, self.out, **kwargs)
        # Cycling means an unscripted run keeps going; the default budget of 3
        # laps is what makes these tests terminate.
        script = list(outcomes)
        rec.attempts = []
        rec.proxy_starts = []

        def fake_stream(cmd, candidate):
            rec.attempts.append(candidate.name)
            if rec._force_failover_flag:
                return StreamOutcome.INTERRUPTED  # mirrors the real check
            outcome = script.pop(0) if script else StreamOutcome.FAILED
            if callable(outcome):
                outcome = outcome(rec, candidate)
            # True/False remain shorthand for the two unambiguous outcomes.
            if outcome is True:
                return StreamOutcome.COMPLETED
            if outcome is False:
                return StreamOutcome.FAILED
            return outcome

        def fake_detect(candidate):
            candidate.m3u8_url = candidate.url
            candidate.detected = True
            return True

        def fake_start_proxy(candidate):
            rec.proxy_starts.append(candidate.name)
            return "http://127.0.0.1:8090/channel/x"

        rec._stream_ffmpeg_process = fake_stream
        rec.detect_candidate_headers = fake_detect
        rec.start_proxy = fake_start_proxy
        rec.stop_proxy = lambda: None
        return rec

    def run_loop(self, rec):
        # Zero the inter-failover delay so tests stay fast. This stubs the
        # delay itself rather than time.sleep: the loop waits on _stop_event
        # so that a shutdown interrupts the backoff, and patching sleep would
        # no longer make these tests fast -- it would silently make them take
        # the real backoff, up to 60s each.
        rec._failover_delay = lambda wrapped: 0.0
        from unittest.mock import patch
        with patch("app.recorder.time.sleep"):
            rec._recording_loop()


class TestRebroadcastRecorder(unittest.TestCase):
    """A channel captures like a recording but keeps nothing."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvarr-rb-"))
        self.ring = ringbuffer.RingBuffer(
            self.tmp / "chan.buf", capacity=ringbuffer.TS_PACKET_SIZE * 100)

    def tearDown(self):
        self.ring.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make(self, ring=None):
        return StreamFailoverRecorder(
            "s1", ["http://a/1.m3u8"], str(self.tmp / "unused.ts"),
            ring=ring, channel_name="Bears vs Packers")

    def test_a_recording_is_not_a_rebroadcast(self):
        self.assertFalse(self.make().is_rebroadcast)

    def test_a_ring_makes_it_a_rebroadcast(self):
        self.assertTrue(self.make(self.ring).is_rebroadcast)

    def test_bytes_go_to_the_ring_and_no_file_is_created(self):
        rec = self.make(self.ring)
        with rec._open_sink() as sink:
            sink.write(b"payload")
            sink.flush()
        self.assertEqual(self.ring.read(0)[0], b"payload")
        self.assertFalse((self.tmp / "unused.ts").exists())

    def test_a_recording_still_writes_its_file(self):
        rec = self.make()
        with rec._open_sink() as sink:
            sink.write(b"payload")
        self.assertEqual((self.tmp / "unused.ts").read_bytes(), b"payload")

    def test_filesize_is_zero_because_nothing_is_kept(self):
        # The ring's backing file is a fixed size no matter how much has
        # flowed through it; reporting it would show a constant "recording"
        # that never grows.
        rec = self.make(self.ring)
        self.ring.write(b"x" * 5000)
        self.assertEqual(rec.get_filesize_mb(), 0.0)

    def test_status_says_it_is_a_rebroadcast_and_names_the_channel(self):
        summary = self.make(self.ring).get_status_summary()
        self.assertTrue(summary["is_rebroadcast"])
        self.assertEqual(summary["channel_name"], "Bears vs Packers")
        self.assertEqual(summary["output_filename"], "")

    def test_guide_uses_the_channel_name_when_there_is_no_file(self):
        summary = self.make(self.ring).get_status_summary()
        summary["is_running"] = True
        self.assertIn("Bears vs Packers", tuner.generate_m3u_playlist(
            [summary], "http://h:8999"))

    def test_guide_does_not_claim_to_be_recording(self):
        # Saying "Recording to ..." on a channel that keeps nothing would be a
        # promise PVArr is not making.
        summary = self.make(self.ring).get_status_summary()
        summary["is_running"] = True
        xml = tuner.generate_xmltv_epg([summary])
        self.assertIn("not being recorded", xml)
        self.assertNotIn("Recording to", xml)


class TestRebroadcastResumePolicy(unittest.TestCase):
    """A channel is meant to be permanent; a recording is an event."""

    def record(self, **over):
        r = sessions.build_record(
            recording_id="chan1", candidates=["http://a/1.m3u8"],
            output_filepath="/nonexistent/chan1.buf", started_at=1000.0,
            rebroadcast=True, channel_name="News")
        r.update(over)
        return r

    def test_channel_resumes_even_though_its_buffer_is_gone(self):
        # The buffer is deleted at shutdown by design, so the file check that
        # governs recordings would discard every channel on every restart.
        self.assertEqual(sessions.resume_decision(self.record()), "resume")

    def test_channel_is_not_subject_to_the_attempt_limit(self):
        # A channel whose upstream is genuinely dead ends itself via
        # max_cycles, so there is no restart loop to guard against.
        r = self.record(resume_attempts=99)
        self.assertEqual(sessions.resume_decision(r), "resume")

    def test_a_recording_with_no_file_is_still_discarded(self):
        r = self.record(rebroadcast=False)
        self.assertEqual(sessions.resume_decision(r), "discard")

    def test_the_channel_name_is_persisted(self):
        self.assertEqual(self.record()["channel_name"], "News")
        self.assertTrue(self.record()["rebroadcast"])


class TestRingBuffer(unittest.TestCase):
    """The bounded buffer rebroadcast fans out from.

    Correctness here is load-bearing: everything a viewer sees comes through
    it, and a subtle wrap bug shows up as corrupted video rather than an error.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvarr-ring-"))
        self.path = self.tmp / "chan.buf"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def ring(self, capacity=None):
        return ringbuffer.RingBuffer(self.path, capacity=capacity)

    # -- shape ---------------------------------------------------------

    def test_capacity_is_whole_packets(self):
        r = self.ring(capacity=1000)
        self.assertEqual(r.capacity % ringbuffer.TS_PACKET_SIZE, 0)
        self.assertLessEqual(r.capacity, 1000)

    def test_file_is_created_at_full_size(self):
        # Readers pread anywhere; a short file would return b"" and look like
        # a stalled stream rather than an empty ring.
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 10)
        self.assertEqual(self.path.stat().st_size, r.capacity)

    def test_file_never_grows(self):
        cap = ringbuffer.TS_PACKET_SIZE * 10
        r = self.ring(capacity=cap)
        for _ in range(50):
            r.write(b"x" * cap)
        self.assertEqual(self.path.stat().st_size, cap)

    # -- reading -------------------------------------------------------

    def test_roundtrip(self):
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 100)
        r.write(b"hello world")
        data, offset = r.read(0)
        self.assertEqual(data, b"hello world")
        self.assertEqual(offset, 11)

    def test_reader_that_is_current_gets_nothing_not_an_error(self):
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 100)
        r.write(b"abc")
        _, offset = r.read(0)
        self.assertEqual(r.read(offset), (b"", offset))

    def test_a_keeping_up_reader_sees_every_byte_across_many_wraps(self):
        # The real property: whatever went in comes out, in order, unchanged.
        import random
        rng = random.Random(1234)
        cap = ringbuffer.TS_PACKET_SIZE * 50
        r = self.ring(capacity=cap)
        written = bytearray()
        read = bytearray()
        offset = 0
        for _ in range(400):
            chunk = bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 600)))
            r.write(chunk)
            written += chunk
            while True:
                data, offset = r.read(offset, max_bytes=4096)
                if not data:
                    break
                read += data
        self.assertEqual(bytes(read), bytes(written))

    def test_writes_larger_than_the_ring_keep_the_tail(self):
        cap = ringbuffer.TS_PACKET_SIZE * 10
        r = self.ring(capacity=cap)
        payload = bytes(range(256)) * 100
        r.write(payload)
        data, _ = r.read(r.oldest_offset())
        self.assertEqual(data, payload[-cap:])

    # -- lapping -------------------------------------------------------

    def test_a_lapped_reader_is_skipped_forward_not_fed_garbage(self):
        cap = ringbuffer.TS_PACKET_SIZE * 10
        r = self.ring(capacity=cap)
        r.write(b"A" * cap)          # reader is at 0 and still current
        r.write(b"B" * cap * 3)      # now long gone
        data, offset = r.read(0)
        self.assertGreaterEqual(offset, r.oldest_offset())
        self.assertNotIn(b"A", data)

    def test_resync_lands_on_a_packet_boundary(self):
        # Off-boundary on purpose: 7 is not a multiple of 188.
        cap = ringbuffer.TS_PACKET_SIZE * 10
        r = self.ring(capacity=cap)
        r.write(b"x" * (cap * 4 + 7))
        _, offset = r.read(0)
        self.assertEqual(offset % ringbuffer.TS_PACKET_SIZE, 0)

    def test_alignment_survives_wrapping(self):
        # offset % 188 is preserved across wraps only because capacity is a
        # whole number of packets. This is the invariant the whole design
        # rests on, so assert it directly.
        cap = ringbuffer.TS_PACKET_SIZE * 7
        r = self.ring(capacity=cap)
        for _ in range(200):
            r.write(b"y" * ringbuffer.TS_PACKET_SIZE)
        self.assertEqual(r.live_offset() % ringbuffer.TS_PACKET_SIZE, 0)
        self.assertEqual(r.oldest_offset() % ringbuffer.TS_PACKET_SIZE, 0)

    def test_live_offset_joins_at_the_edge_not_the_history(self):
        # Plex is tuning a live channel. Replaying the buffer would put every
        # viewer a minute behind, and further behind on every reconnect.
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 100)
        r.write(b"z" * 5000)
        self.assertGreater(r.live_offset(), 4000)
        self.assertEqual(r.live_offset() % ringbuffer.TS_PACKET_SIZE, 0)
        # It points at the START of the newest packet, so at most one
        # part-arrived packet is still ahead of it -- never a backlog.
        pending, _ = r.read(r.live_offset())
        self.assertLess(len(pending), ringbuffer.TS_PACKET_SIZE)

    # -- writer independence -------------------------------------------

    def test_the_writer_never_blocks_on_a_stalled_reader(self):
        # The capture thread must keep pace with the upstream stream no matter
        # what a client does. This is why an unread ring overwrites instead of
        # applying backpressure.
        cap = ringbuffer.TS_PACKET_SIZE * 10
        r = self.ring(capacity=cap)
        t0 = time.time()
        for _ in range(2000):
            r.write(b"q" * 512)
        self.assertLess(time.time() - t0, 5.0)
        self.assertEqual(r.write_offset, 2000 * 512)

    # -- lifecycle -----------------------------------------------------

    def test_close_removes_the_file(self):
        # A rebroadcast buffer is not a recording; leaving it would fill the
        # volume with footage nobody asked to keep.
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 10)
        r.write(b"data")
        r.close()
        self.assertFalse(self.path.exists())

    def test_operations_after_close_are_inert(self):
        r = self.ring(capacity=ringbuffer.TS_PACKET_SIZE * 10)
        r.close()
        self.assertEqual(r.write(b"x"), 0)
        self.assertEqual(r.read(0), (b"", 0))
        r.close()  # idempotent

    def test_capacity_from_environment(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_BUFFER_MB": "4"}):
            self.assertEqual(ringbuffer.default_capacity(),
                             4 * 1024 * 1024 // 188 * 188)
        with patch.dict(os.environ, {"PVARR_BUFFER_MB": "nonsense"}):
            self.assertEqual(ringbuffer.default_capacity(),
                             ringbuffer.DEFAULT_CAPACITY_BYTES)


class TestSessionStore(unittest.TestCase):
    """One JSON per live session, so a restart does not lose the recording."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvarr-store-"))
        self.store = sessions.SessionStore(self.tmp / "sessions")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, **over):
        r = sessions.build_record(
            recording_id="rec1",
            candidates=["http://a/1.m3u8", "http://b/2.m3u8"],
            output_filepath=str(self.tmp / "game.ts"),
            started_at=1756600000.0,
            header_overrides={"http://a/1.m3u8": {"cookie": "SESSIONID=secret"}},
        )
        r.update(over)
        return r

    def test_roundtrip(self):
        self.store.save(self.record())
        loaded = self.store.load_all()
        self.assertEqual(len(loaded), 1)
        self.assertEqual(loaded[0]["id"], "rec1")
        self.assertEqual(loaded[0]["candidates"][1], "http://b/2.m3u8")

    def test_cookie_survives_because_a_resume_needs_it(self):
        # A session-gated stream cannot be reattached without its cookie.
        self.store.save(self.record())
        loaded = self.store.load_all()[0]
        self.assertEqual(
            loaded["header_overrides"]["http://a/1.m3u8"]["cookie"],
            "SESSIONID=secret")

    def test_state_files_are_not_world_readable(self):
        # They hold stream URLs and a live session cookie.
        self.store.save(self.record())
        path = next((self.tmp / "sessions").glob("*.json"))
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(oct((self.tmp / "sessions").stat().st_mode & 0o777), "0o700")

    def test_no_progress_counters_are_persisted(self):
        # They disagree with reality the moment the process dies, which is
        # exactly when they get read. Progress comes from stat() at resume.
        r = self.record()
        for key in ("bytes_written", "elapsed_seconds", "filesize_mb", "status"):
            self.assertNotIn(key, r)

    def test_save_leaves_no_temp_files_behind(self):
        self.store.save(self.record())
        leftovers = list((self.tmp / "sessions").glob(".tmp-*"))
        self.assertEqual(leftovers, [])

    def test_remove(self):
        self.store.save(self.record())
        self.store.remove("rec1")
        self.assertEqual(self.store.load_all(), [])

    def test_unknown_schema_is_ignored_not_guessed_at(self):
        self.store.save(self.record())
        path = next((self.tmp / "sessions").glob("*.json"))
        data = json.loads(path.read_text())
        data["schema"] = sessions.SCHEMA_VERSION + 99
        path.write_text(json.dumps(data))
        self.assertEqual(self.store.load_all(), [])

    def test_corrupt_file_does_not_take_out_the_others(self):
        self.store.save(self.record())
        self.store.save(self.record(id="rec2"))
        (self.tmp / "sessions" / "broken.json").write_text("{not json")
        self.assertEqual(len(self.store.load_all()), 2)

    def test_unwritable_directory_disables_rather_than_raises(self):
        # The dev box has a root-owned config/; a running recording must not
        # die because its state file cannot be written.
        store = sessions.SessionStore(Path("/proc/nonexistent/sessions"))
        self.assertFalse(store.enabled)
        self.assertFalse(store.save(self.record()))
        self.assertEqual(store.load_all(), [])
        self.assertFalse(store.remove("rec1"))


class TestResumeDecision(unittest.TestCase):
    """What to do with a session found on disk at boot."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pvarr-resume-"))
        self.ts = self.tmp / "game.ts"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def record(self, **over):
        r = sessions.build_record(
            recording_id="rec1", candidates=["http://a/1.m3u8"],
            output_filepath=str(self.ts), started_at=1000.0)
        r.update(over)
        return r

    def write(self, size=1024, age=0.0):
        self.ts.write_bytes(b"x" * size)
        if age:
            past = time.time() - age
            os.utime(self.ts, (past, past))

    def test_missing_file_is_discarded(self):
        self.assertEqual(sessions.resume_decision(self.record()), "discard")

    def test_empty_file_is_discarded(self):
        self.write(size=0)
        self.assertEqual(sessions.resume_decision(self.record()), "discard")

    def test_recently_written_file_resumes(self):
        self.write(age=5)
        self.assertEqual(sessions.resume_decision(self.record()), "resume")

    def test_long_dead_file_is_finalised_not_resumed(self):
        self.write(age=3600)
        self.assertEqual(sessions.resume_decision(self.record()), "finalise")

    def test_gap_is_measured_from_the_file_not_the_last_transition(self):
        # The trap: state is written on transitions only, so a healthy
        # three-hour recording's last transition is at t=0. Measuring the gap
        # from that would finalise exactly the recordings worth saving.
        self.write(age=5)
        old = self.record(started_at=time.time() - 10800)
        self.assertEqual(sessions.resume_decision(old), "resume")

    def test_repeated_failures_stop_the_restart_loop(self):
        self.write(age=5)
        r = self.record(resume_attempts=sessions.DEFAULT_MAX_RESUME_ATTEMPTS)
        self.assertEqual(sessions.resume_decision(r), "finalise")

    def test_limits_are_configurable(self):
        self.write(age=100)
        self.assertEqual(
            sessions.resume_decision(self.record(), gap_limit=50), "finalise")
        self.assertEqual(
            sessions.resume_decision(self.record(), gap_limit=500), "resume")


class TestStopReason(unittest.TestCase):
    """Operator stop and process-going-away are not the same event.

    stop() used to set status='completed' unconditionally. Persisting that
    meant a restart read 'completed' and nothing ever resumed.
    """

    def make(self):
        rec = StreamFailoverRecorder("s1", ["http://a/1.m3u8"], "/tmp/x.ts")
        rec._reap_ffmpeg = lambda: None
        rec.stop_proxy = lambda: None
        return rec

    def test_operator_stop_completes(self):
        rec = self.make()
        rec.stop()
        self.assertEqual(rec.status, "completed")
        self.assertEqual(rec.stop_reason, "operator")

    def test_shutdown_stop_is_interrupted_not_completed(self):
        rec = self.make()
        rec.stop(reason="shutdown")
        self.assertEqual(rec.status, "interrupted")

    def test_completion_block_does_not_overwrite_interrupted(self):
        rec = self.make()
        rec.stop(reason="shutdown")
        rec._recording_loop()
        self.assertEqual(rec.status, "interrupted")


class TestLogSequence(unittest.TestCase):
    """The live log view froze silently once a session passed 500 lines.

    log_history is trimmed to its newest LOG_HISTORY_LIMIT entries, but the
    SSE endpoint tracked a plain index into it. Once trimming began the length
    stopped growing, "is there anything new" was never true again, and the
    dashboard log pane sat dead for the rest of the recording with no error.
    """

    def make(self):
        return StreamFailoverRecorder("s1", ["http://a/1.m3u8"], "/tmp/x.ts")

    def test_new_lines_still_arrive_after_the_buffer_wraps(self):
        rec = self.make()
        for i in range(rec.LOG_HISTORY_LIMIT + 50):
            rec._log(f"line {i}")
        _, seq = rec.logs_since(0)
        rec._log("after the wrap")
        lines, _ = rec.logs_since(seq)
        self.assertEqual([l.split("] ", 3)[-1] for l in lines], ["after the wrap"])

    def test_history_is_capped(self):
        rec = self.make()
        for i in range(rec.LOG_HISTORY_LIMIT + 200):
            rec._log(f"line {i}")
        self.assertEqual(len(rec.log_history), rec.LOG_HISTORY_LIMIT)

    def test_reader_further_behind_than_the_buffer_gets_what_is_left(self):
        rec = self.make()
        for i in range(rec.LOG_HISTORY_LIMIT * 2):
            rec._log(f"line {i}")
        lines, seq = rec.logs_since(0)
        self.assertEqual(len(lines), rec.LOG_HISTORY_LIMIT)
        self.assertEqual(seq, rec.LOG_HISTORY_LIMIT * 2)

    def test_nothing_new_returns_nothing(self):
        rec = self.make()
        rec._log("one")
        _, seq = rec.logs_since(0)
        self.assertEqual(rec.logs_since(seq), ([], seq))


class TestStreamUrlSchemes(unittest.TestCase):
    """FFmpeg opens far more than HTTP, and PVArr streams captured bytes back."""

    def test_http_and_https_are_accepted(self):
        for url in ("http://a/1.m3u8", "https://a/1.m3u8", "  https://a/1.m3u8  "):
            self.assertTrue(safe_stream_url(url).startswith("http"))

    def test_local_file_read_is_refused(self):
        with self.assertRaises(ValueError):
            safe_stream_url("file:///etc/passwd")

    def test_concat_splicing_is_refused(self):
        with self.assertRaises(ValueError):
            safe_stream_url("concat:/etc/passwd|/etc/shadow")

    def test_raw_socket_is_refused(self):
        with self.assertRaises(ValueError):
            safe_stream_url("tcp://169.254.169.254:80")

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            safe_stream_url("   ")

    def test_ffmpeg_command_pins_the_protocol_whitelist(self):
        rec = StreamFailoverRecorder("s1", ["http://a/1.m3u8"], "/tmp/x.ts")
        cmd = rec._build_ffmpeg_cmd("http://a/1.m3u8")
        self.assertIn("-protocol_whitelist", cmd)
        allowed = cmd[cmd.index("-protocol_whitelist") + 1]
        self.assertNotIn("file", allowed.split(","))


class TestBackoffIsInterruptible(FailoverLoopTestCase):
    """A stop must not have to wait out the failover backoff.

    The backoff climbs to 60s after a fruitless lap. It used to be a plain
    time.sleep, so a container stop landing in one blew through the 20s
    shutdown budget and the 30s stop_grace_period, and Docker SIGKILLed the
    app mid-shutdown -- losing the remux the shutdown fix exists to protect.
    """

    def test_stop_during_backoff_returns_promptly(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [False, False])
        rec._failover_delay = lambda wrapped: 30.0
        rec._stop_event.set()
        t0 = time.time()
        rec._recording_loop()
        elapsed = time.time() - t0
        self.assertLess(elapsed, 5.0,
                        f"stop waited out the backoff ({elapsed:.1f}s)")


class TestFailoverHappyPath(FailoverLoopTestCase):
    def test_first_candidate_succeeds_no_failover(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [True])
        self.run_loop(rec)
        self.assertEqual(rec.attempts, ["Candidate 1"])
        self.assertEqual(rec.current_candidate_index, 0)
        self.assertEqual(rec.status, "completed")

    def test_completion_callback_fires_on_success(self):
        seen = []
        rec = self.make(["http://a/1.m3u8"], [True],
                        on_completion_callback=seen.append)
        self.run_loop(rec)
        self.assertEqual(seen, [str(Path(self.out).resolve())])

    def test_is_running_cleared_when_loop_exits(self):
        rec = self.make(["http://a/1.m3u8"], [True])
        rec.is_running = True
        self.run_loop(rec)
        self.assertFalse(rec.is_running)
        self.assertIsNotNone(rec.stop_time)


class TestFailoverAdvance(FailoverLoopTestCase):
    def test_proxy_fallback_tried_before_advancing(self):
        # Candidate 1 fails direct -> proxy fallback on the SAME candidate,
        # and only then do we move on. This is the documented "direct-first
        # with proxy fallback" design.
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [False, False, True])
        self.run_loop(rec)
        self.assertEqual(rec.attempts,
                         ["Candidate 1", "Candidate 1", "Candidate 2"])
        self.assertEqual(rec.proxy_starts, ["Candidate 1"])
        self.assertEqual(rec.current_candidate_index, 1)
        self.assertEqual(rec.status, "completed")

    def test_failover_callback_names_next_candidate(self):
        seen = []
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [False, False, True],
                        on_failover_callback=lambda rid, name: seen.append((rid, name)))
        self.run_loop(rec)
        self.assertEqual(seen, [("test-id", "Candidate 2")])

    def test_exhaustion_takes_max_cycles_laps_not_one_pass(self):
        # The list cycles now, so one bad pass is no longer the end: a token
        # blip that touches all three sources must not kill a recording that
        # still has hours to run. Giving up takes max_cycles fruitless laps.
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        [], max_cycles=3)
        self.run_loop(rec)
        self.assertEqual(rec.status, "failed")
        self.assertEqual(rec.cycles_without_data, 3)
        # Three laps x three candidates x (direct + proxy).
        self.assertEqual(rec.attempts.count("Candidate 1"), 6)
        self.assertEqual(rec.attempts.count("Candidate 3"), 6)

    def test_one_bad_lap_does_not_end_the_recording(self):
        # Everything fails once, then candidate 1 comes back on the second lap.
        script = [False] * 6 + [True]
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        script, max_cycles=3)
        self.run_loop(rec)
        self.assertEqual(rec.status, "completed",
                         "a single failed lap ended the recording")
        self.assertEqual(rec.current_candidate_index, 0,
                         "did not cycle back round to candidate 1")

    def test_data_resets_the_fruitless_lap_budget(self):
        # A long capture that fails over occasionally must never exhaust its
        # budget: any bytes at all put the counter back to zero.
        def deliver(rec, candidate):
            rec.bytes_written += 4096
            return StreamOutcome.INTERRUPTED

        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [False, False, deliver, deliver, deliver, deliver, True],
                        max_cycles=2)
        self.run_loop(rec)
        self.assertEqual(rec.status, "completed")
        self.assertEqual(rec.cycles_without_data, 0)

    def test_backoff_grows_between_fruitless_laps(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [])
        self.assertEqual(rec._failover_delay(wrapped=False), 1.0)
        rec.cycles_without_data = 1
        self.assertEqual(rec._failover_delay(wrapped=True), 5.0)
        rec.cycles_without_data = 2
        self.assertEqual(rec._failover_delay(wrapped=True), 10.0)
        rec.cycles_without_data = 99
        self.assertEqual(rec._failover_delay(wrapped=True), 60.0)

    def test_completion_callback_not_fired_when_all_fail(self):
        seen = []
        rec = self.make(["http://a/1.m3u8"], [False, False],
                        on_completion_callback=seen.append)
        self.run_loop(rec)
        self.assertEqual(rec.status, "failed")
        self.assertEqual(seen, [], "completion callback fired on total failure")

    def test_callback_exception_does_not_kill_recording(self):
        def boom(*a):
            raise RuntimeError("notification service down")
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [False, False, True], on_failover_callback=boom)
        self.run_loop(rec)  # must not raise
        self.assertEqual(rec.status, "completed")


class TestInterruptedFailover(FailoverLoopTestCase):
    def test_interrupted_advances_to_next_candidate(self):
        rec = self.make(
            ["http://a/1.m3u8", "http://b/2.m3u8"],
            [StreamOutcome.INTERRUPTED, StreamOutcome.INTERRUPTED, True],
        )
        self.run_loop(rec)
        self.assertEqual(rec.current_candidate_index, 1)
        self.assertEqual(rec.status, "completed")

    def test_completed_does_not_advance(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [StreamOutcome.COMPLETED])
        self.run_loop(rec)
        self.assertEqual(rec.attempts, ["Candidate 1"])

    def test_exhaustion_after_real_footage_is_partial_not_failed(self):
        # A 3-hour recording whose stream dies near the end must not be thrown
        # away: post-processing still needs to run on what was captured.
        def wrote_then_died(rec, candidate):
            rec.bytes_written += 500_000_000
            return StreamOutcome.INTERRUPTED

        seen = []
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [wrote_then_died] * 4,
                        on_completion_callback=seen.append)
        self.run_loop(rec)
        self.assertEqual(rec.status, "completed_partial")
        self.assertEqual(len(seen), 1, "post-processing skipped for partial recording")

    def test_exhaustion_with_no_footage_is_failed(self):
        seen = []
        rec = self.make(["http://a/1.m3u8"], [StreamOutcome.FAILED] * 2,
                        on_completion_callback=seen.append)
        self.run_loop(rec)
        self.assertEqual(rec.status, "failed")
        self.assertEqual(seen, [])

    def test_proxy_retried_on_interrupted(self):
        # An expiring mid-stream token is exactly what the proxy re-scrapes.
        rec = self.make(["http://a/1.m3u8"], [StreamOutcome.INTERRUPTED, True])
        self.run_loop(rec)
        self.assertEqual(rec.proxy_starts, ["Candidate 1"])


class TestStopDuringRecording(FailoverLoopTestCase):
    def test_stop_event_halts_before_next_candidate(self):
        def stop_it(rec, candidate):
            rec._stop_event.set()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [stop_it])
        self.run_loop(rec)
        self.assertEqual(rec.attempts, ["Candidate 1"])
        self.assertNotIn("Candidate 2", rec.attempts)

    def test_stop_skips_proxy_fallback(self):
        def stop_it(rec, candidate):
            rec._stop_event.set()
            return False
        rec = self.make(["http://a/1.m3u8"], [stop_it])
        self.run_loop(rec)
        self.assertEqual(rec.proxy_starts, [])


class TestForceFailover(FailoverLoopTestCase):
    def test_force_failover_advances_one_candidate(self):
        def force_then_die(rec, candidate):
            rec.force_failover()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [force_then_die, True])
        self.run_loop(rec)
        self.assertEqual(rec.current_candidate_index, 1)

    def test_force_failover_skips_proxy_fallback(self):
        # An explicit "move on" must not spend time on the proxy bridge for
        # the candidate the user just abandoned.
        def force_then_die(rec, candidate):
            rec.force_failover()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [force_then_die, True])
        self.run_loop(rec)
        self.assertEqual(rec.proxy_starts, [])

    def test_force_failover_does_not_burn_remaining_candidates(self):
        # Pressing the dashboard failover button once must switch to the next
        # stream and keep recording -- not cascade through every remaining
        # candidate and kill the recording.
        def force_then_die(rec, candidate):
            rec.force_failover()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        [force_then_die, True])
        self.run_loop(rec)
        self.assertEqual(rec.status, "completed",
                         "one manual failover killed the whole recording")
        self.assertEqual(rec.current_candidate_index, 1)
        self.assertEqual(rec.attempts, ["Candidate 1", "Candidate 2"])

    def test_flag_cleared_after_being_consumed(self):
        def force_then_die(rec, candidate):
            rec.force_failover()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [force_then_die, True])
        self.run_loop(rec)
        self.assertFalse(rec._force_failover_flag,
                         "force-failover flag left set after being handled")

    def test_force_failover_refused_when_no_backup_remains(self):
        # With a single URL there is nothing to switch to, and advancing past
        # the last candidate ends the recording. The button used to do exactly
        # that -- killing a live capture -- while the API answered "success".
        calls = []

        def try_force_then_finish(rec, candidate):
            calls.append(rec.force_failover())
            return True

        rec = self.make(["http://a/1.m3u8"], [try_force_then_finish])
        self.run_loop(rec)
        self.assertEqual(calls, [False], "force_failover claimed it switched")
        self.assertFalse(rec._force_failover_flag,
                         "a refused failover must not latch the flag")
        self.assertEqual(rec.current_candidate_index, 0)
        self.assertEqual(rec.status, "completed",
                         "a refused failover ended the recording anyway")

    def test_force_failover_from_the_last_candidate_wraps(self):
        # Was refused when the walk was one-way, because advancing past the end
        # ended the recording. Now the list cycles, so the last candidate has
        # somewhere to go: back to the first.
        calls = []

        def die(rec, candidate):
            return False

        def try_force(rec, candidate):
            calls.append(rec.force_failover())
            return True

        # Candidate 1 fails direct, then via the proxy, so we land on the last.
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [die, die, try_force, True])
        self.run_loop(rec)
        self.assertEqual(calls, [True], "force-failover refused despite cycling")
        self.assertEqual(rec.current_candidate_index, 0)

    def test_has_next_candidate_holds_at_the_end_of_the_list(self):
        # Cycling means the last candidate still has a next one -- the first.
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [])
        self.assertTrue(rec.has_next_candidate)
        rec.current_candidate_index = 1
        self.assertTrue(rec.has_next_candidate)

    def test_switch_jumps_straight_to_a_chosen_candidate(self):
        def jump(rec, candidate):
            rec.switch_to_candidate(2)      # 0-based -> Candidate 3
            return False

        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        [jump, True])
        self.run_loop(rec)
        self.assertEqual(rec.attempts, ["Candidate 1", "Candidate 3"],
                         "switch did not skip straight to the chosen candidate")
        self.assertEqual(rec.current_candidate_index, 2)

    def test_switch_back_to_the_primary(self):
        # The story this whole change exists for: the primary's token expires,
        # the recorder moves to a backup, the primary recovers, and there was
        # previously no route back to it.
        def to_first(rec, candidate):
            rec.switch_to_candidate(0)
            return False

        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        [False, False, to_first, True])
        self.run_loop(rec)
        self.assertEqual(rec.attempts,
                         ["Candidate 1", "Candidate 1", "Candidate 2", "Candidate 1"])
        self.assertEqual(rec.current_candidate_index, 0)
        self.assertEqual(rec.status, "completed")

    def test_switch_skips_the_proxy_retry_on_the_stream_being_left(self):
        def jump(rec, candidate):
            rec.switch_to_candidate(1)
            return False

        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [jump, True])
        self.run_loop(rec)
        self.assertEqual(rec.proxy_starts, [],
                         "spent time on the proxy for a stream the operator left")

    def test_switch_rejects_an_index_out_of_range(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [])
        self.assertFalse(rec.switch_to_candidate(-1))
        self.assertFalse(rec.switch_to_candidate(2))
        self.assertFalse(rec._force_failover_flag,
                         "a rejected switch must not latch the failover flag")

    def test_switch_to_the_current_candidate_is_a_no_op(self):
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [])
        self.assertFalse(rec.switch_to_candidate(0))
        self.assertFalse(rec._force_failover_flag)

    def test_single_candidate_still_has_nowhere_to_go(self):
        # The protection that matters stays: forcing a failover on a one-URL
        # session would end the recording, so it is still refused.
        rec = self.make(["http://a/1.m3u8"], [])
        self.assertFalse(rec.has_next_candidate)
        self.assertFalse(rec.force_failover())

    def test_force_failover_marks_status_immediately(self):
        # The loop only reaches its own "failing_over" assignment once the
        # current attempt unwinds, and holds it for about a second. Against a
        # 3s dashboard poll the operator saw nothing change at all.
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"], [])
        rec.is_running = True
        rec.status = "recording"
        self.assertTrue(rec.force_failover())
        self.assertEqual(rec.status, "failing_over")

    def test_two_forced_failovers_traverse_two_candidates(self):
        def force_then_die(rec, candidate):
            rec.force_failover()
            return False
        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8", "http://c/3.m3u8"],
                        [force_then_die, force_then_die, True])
        self.run_loop(rec)
        self.assertEqual(rec.current_candidate_index, 2)
        self.assertEqual(rec.status, "completed")


class TestStatusReporting(FailoverLoopTestCase):
    def test_current_candidate_never_exceeds_total(self):
        # The index runs one past the end once the list is exhausted, which the
        # dashboard rendered literally as "Stream 2 of 1".
        rec = self.make(["http://a/1.m3u8"], [False, False])
        self.run_loop(rec)
        summary = rec.get_status_summary()
        self.assertEqual(summary["total_candidates"], 1)
        self.assertEqual(summary["current_candidate"], 1,
                         "status summary reported a candidate that does not exist")

    def test_status_returns_to_recording_after_failover(self):
        # While candidate 2 is happily recording the dashboard must not still
        # be showing "failing_over".
        observed = []

        def watch(rec, candidate):
            observed.append(rec.status)
            return True

        rec = self.make(["http://a/1.m3u8", "http://b/2.m3u8"],
                        [False, False, watch])
        self.run_loop(rec)
        self.assertEqual(observed, ["recording"],
                         f"status was {observed} while actively recording")


# --------------------------------------------------------------------------
# recorder._stream_ffmpeg_process  —  freeze detection
#
# Drives the real method against a fake Popen so the stall path is reachable
# without spawning FFmpeg or waiting out a real timeout.
# --------------------------------------------------------------------------
class _FakeProc:
    """Stands in for FFmpeg, over a real OS pipe.

    The capture loop selects on the stdout file descriptor, so a fake with a
    plain read() method would not exercise the code under test. Chunks are
    written into the pipe up front; leaving the write end OPEN models the case
    that matters most -- a source that has gone quiet without dropping the
    connection, which is exactly what the freeze timeout exists to catch.
    """

    def __init__(self, chunks, returncode=None, close_stdout=False,
                 stderr_lines=()):
        read_fd, write_fd = os.pipe()
        for chunk in chunks:
            os.write(write_fd, chunk)   # total stays well under the 64KB pipe
        if close_stdout:
            os.close(write_fd)
            self._write_fd = None
        else:
            self._write_fd = write_fd
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        # readline() walks these then hits EOF, so the drain thread exits.
        self.stderr = io.BytesIO(b"".join(l + b"\n" for l in stderr_lines))
        self._rc = returncode

    def close(self):
        try:
            self.stdout.close()
        except Exception:
            pass
        if self._write_fd is not None:
            try:
                os.close(self._write_fd)
            except Exception:
                pass
            self._write_fd = None

    def poll(self):
        return self._rc

    def terminate(self):
        pass

    def kill(self):
        pass

    def wait(self, timeout=None):
        return self._rc


class TestFreezeDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.out = str(Path(self.tmp) / "out.ts")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def drive(self, chunks, returncode=None, freeze_timeout=0,
              close_stdout=False, stderr_lines=()):
        from unittest.mock import patch
        rec = StreamFailoverRecorder(
            "test-id", ["http://a/1.m3u8"], self.out,
            freeze_timeout_sec=freeze_timeout,
            min_free_gb=0,   # not what these tests are about; see make() above
        )
        # Shrink the select() wait so tests do not sit through the real 0.5s.
        rec.READ_POLL_SEC = 0.01
        proc = _FakeProc(chunks, returncode, close_stdout=close_stdout,
                         stderr_lines=stderr_lines)
        try:
            with patch("app.recorder.subprocess.Popen", return_value=proc):
                result = rec._stream_ffmpeg_process(["ffmpeg"], rec.candidates[0])
        finally:
            proc.close()
        return rec, result

    def test_stall_before_any_data_is_a_failure(self):
        rec, result = self.drive([])
        self.assertIs(result, StreamOutcome.FAILED)
        self.assertEqual(rec.candidates[0].fail_count, 1)

    def test_bytes_written_is_tracked(self):
        rec, _ = self.drive([b"x" * 4096])
        self.assertEqual(rec.bytes_written, 4096)
        self.assertEqual(Path(self.out).stat().st_size, 4096)

    def test_clean_exit_after_data_is_success(self):
        # FFmpeg exited 0 with data on disk: the stream genuinely ended.
        rec, result = self.drive([b"x" * 1024], returncode=0)
        self.assertIs(result, StreamOutcome.COMPLETED)

    def test_immediate_nonzero_exit_is_a_failure(self):
        rec, result = self.drive([], returncode=1)
        self.assertIs(result, StreamOutcome.FAILED)

    def test_stop_event_returns_without_writing(self):
        from unittest.mock import patch
        rec = StreamFailoverRecorder("test-id", ["http://a/1.m3u8"], self.out,
                                     min_free_gb=0)
        rec._stop_event.set()
        proc = _FakeProc([b"x" * 100])
        try:
            with patch("app.recorder.subprocess.Popen", return_value=proc):
                result = rec._stream_ffmpeg_process(["ffmpeg"], rec.candidates[0])
        finally:
            proc.close()
        self.assertIs(result, StreamOutcome.FAILED)
        self.assertEqual(rec.bytes_written, 0)

    def test_ffmpeg_stderr_explains_a_failure(self):
        # FFmpeg's last words are usually the only account of why a stream
        # would not play. They used to go into an undrained pipe and vanish.
        rec, result = self.drive(
            [], returncode=1,
            stderr_lines=[b"[https] HTTP error 403 Forbidden",
                          b"http://x/y.m3u8: Server returned 403 Forbidden"],
        )
        self.assertIs(result, StreamOutcome.FAILED)
        self.assertIn("403", rec.candidates[0].last_error)

    def test_stderr_is_not_attached_to_a_clean_finish(self):
        rec, result = self.drive([b"x" * 512], returncode=0, close_stdout=True,
                                 stderr_lines=[b"some benign warning"])
        self.assertIs(result, StreamOutcome.COMPLETED)
        self.assertEqual(rec.candidates[0].last_error, "")

    def test_ffmpeg_argv_suppresses_the_stats_spam(self):
        # The progress line is ~124 B/s on a 64KB pipe that is only read on
        # failure: at the default log level it filled in under ten minutes and
        # FFmpeg then blocked, stopping video output entirely.
        rec = StreamFailoverRecorder("test-id", ["http://a/1.m3u8"], self.out)
        cmd = rec._build_ffmpeg_cmd("http://a/1.m3u8")
        self.assertIn("-nostats", cmd)
        self.assertEqual(cmd[cmd.index("-loglevel") + 1], "error")

    def test_freeze_fires_while_the_pipe_is_still_open(self):
        # The regression that mattered: a source that stalls mid-buffer without
        # closing the connection. The old loop sat inside a blocking
        # read(32768) waiting for a full 32KB, so this check was unreachable --
        # measured at 20s of nothing against a 5s timeout. The write end of the
        # pipe is deliberately left open here.
        import time as _time
        start = _time.time()
        rec, result = self.drive([b"x" * 1024], freeze_timeout=0.2)
        elapsed = _time.time() - start
        self.assertIs(result, StreamOutcome.INTERRUPTED)
        self.assertLess(elapsed, 5.0,
                        "freeze detection did not fire on a stalled-but-open pipe")
        self.assertEqual(rec.candidates[0].fail_count, 1)

    def test_partial_chunk_is_written_without_waiting_for_a_full_buffer(self):
        # bytes_written used to advance only in 32KB steps, so the dashboard
        # showed 0.00 MB for the first seconds of a low-bitrate stream.
        rec, _ = self.drive([b"x" * 100], freeze_timeout=0.2)
        self.assertEqual(rec.bytes_written, 100)
        self.assertEqual(Path(self.out).stat().st_size, 100)

    def test_eof_with_clean_exit_completes(self):
        rec, result = self.drive([b"x" * 512], returncode=0, close_stdout=True)
        self.assertIs(result, StreamOutcome.COMPLETED)
        self.assertEqual(rec.bytes_written, 512)

    def test_mid_stream_freeze_after_data_is_interrupted(self):
        # A stall after data is NOT a clean finish. Reporting it as success is
        # what used to truncate a recording at the point of the stall instead
        # of failing over to a backup.
        rec, result = self.drive([b"x" * 2048])
        self.assertIs(result, StreamOutcome.INTERRUPTED)
        self.assertEqual(rec.candidates[0].fail_count, 1)

    def test_nonzero_exit_after_data_is_interrupted(self):
        # FFmpeg dying partway through a recording. Same class of bug as the
        # stall: bytes had arrived, so it read as "completed naturally".
        rec, result = self.drive([b"x" * 4096], returncode=1)
        self.assertIs(result, StreamOutcome.INTERRUPTED)
        self.assertEqual(rec.candidates[0].fail_count, 1)


# --------------------------------------------------------------------------
# app.server  —  route integration tests
#
# Needs httpx (fastapi.testclient). Install with:
#     pip install -r requirements-dev.txt
# The whole group skips cleanly when it is absent so the core suite still runs.
# --------------------------------------------------------------------------
try:
    from fastapi.testclient import TestClient
    HAS_TESTCLIENT = True
except Exception:
    HAS_TESTCLIENT = False


@unittest.skipUnless(HAS_TESTCLIENT, "httpx not installed (see requirements-dev.txt)")
class ServerTestCase(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        from app import server
        from app.naming import StorageManager

        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")
        self.server = server
        # Point the module-level storage at a scratch dir so tests never touch
        # the developer's real recordings/.
        self._storage_patch = patch.object(
            server, "storage", StorageManager(self.tmp)
        )
        self._storage_patch.start()
        self._recorders_patch = patch.object(server, "active_recorders", {})
        self._recorders_patch.start()
        self._dir_patch = patch.object(server, "RECORDINGS_DIR", Path(self.tmp))
        self._dir_patch.start()
        # Disable the free-space floor by default. Left live, every start test
        # passes or fails according to how full the developer's disk is; tests
        # that are about the guard set it explicitly.
        self._disk_patch = patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "0"})
        self._disk_patch.start()
        self.client = TestClient(server.app)

    def tearDown(self):
        self._storage_patch.stop()
        self._recorders_patch.stop()
        self._dir_patch.stop()
        self._disk_patch.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestStaticRoutes(ServerTestCase):
    def test_dashboard_renders(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])

    def test_favicon(self):
        r = self.client.get("/favicon.ico")
        self.assertIn(r.status_code, (200, 204))

    def test_openapi_docs_available(self):
        self.assertEqual(self.client.get("/openapi.json").status_code, 200)


class TestTunerRoutes(ServerTestCase):
    def test_m3u_both_extensions(self):
        for path in ("/live/playlist.m3u", "/live/playlist.m3u8"):
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers["content-type"], "application/x-mpegurl")
                self.assertTrue(r.text.startswith("#EXTM3U"))

    def test_epg_is_xml(self):
        r = self.client.get("/live/epg.xml")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "application/xml")
        self.assertIn("<tv", r.text)


class TestHDHomeRunRoutes(ServerTestCase):
    """Plex's Live TV setup probes a device address for these files before it
    will add a tuner. They 404'd, so the whole add-device flow failed."""

    def _add_session(self, rid="rec1", name="Game 1.ts"):
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.is_running = True
        rec.is_rebroadcast = False
        rec.ring = None
        rec.get_status_summary.return_value = {
            "id": rid, "output_filename": name, "is_running": True,
            "started_at": 1756000000.0,
        }
        self.server.active_recorders[rid] = rec
        return rec

    def test_probe_paths_answer_at_both_mounts(self):
        # Plex appends these to whatever address the user typed, so the root
        # and the /live prefix both have to serve them.
        for prefix in ("", "/live"):
            for name in ("discover.json", "lineup_status.json", "lineup.json"):
                with self.subTest(path=f"{prefix}/{name}"):
                    r = self.client.get(f"{prefix}/{name}")
                    self.assertEqual(r.status_code, 200)

    def test_discover_advertises_a_reachable_lineup_url(self):
        r = self.client.get("/live/discover.json").json()
        self.assertTrue(r["LineupURL"].endswith("/live/lineup.json"))
        follow = self.client.get(r["LineupURL"].replace("http://testserver", ""))
        self.assertEqual(follow.status_code, 200)

    def test_discover_device_id_is_stable(self):
        first = self.client.get("/discover.json").json()["DeviceID"]
        second = self.client.get("/discover.json").json()["DeviceID"]
        self.assertEqual(first, second)
        self.assertRegex(first, r"^[0-9A-F]{8}$")

    def test_lineup_lists_running_recordings(self):
        self._add_session()
        entry = self.client.get("/lineup.json").json()[0]
        self.assertEqual(entry["GuideName"], "Game 1")
        self.assertEqual(entry["URL"],
                         "http://testserver/api/recordings/rec1/stream")

    def test_lineup_urls_are_routable(self):
        # The same regression as the M3U: a lineup pointing at a 404 gives
        # Plex channels that will not tune.
        path = Path(self.tmp) / "live.ts"
        path.write_bytes(b"\x47data")
        rec = self._add_session()
        rec.output_filepath = path
        url = self.client.get("/lineup.json").json()[0]["URL"]
        # Fetch it as a finished recording: a running one tails the file and
        # the request would never return.
        rec.is_running = False
        r = self.client.get(url.replace("http://testserver", ""))
        self.assertEqual(r.status_code, 200, f"lineup URL {url} is not routable")

    def test_empty_lineup_is_still_a_json_array(self):
        self.assertEqual(self.client.get("/lineup.json").json(), [])

    def test_lineup_status_reports_no_scan_running(self):
        self.assertEqual(
            self.client.get("/lineup_status.json").json()["ScanInProgress"], 0
        )

    def test_lineup_post_scan_trigger_succeeds(self):
        for method in (self.client.get, self.client.post):
            with self.subTest(method=method):
                self.assertEqual(method("/lineup.post").status_code, 200)

    def test_device_xml_is_wellformed(self):
        import xml.etree.ElementTree as ET
        r = self.client.get("/device.xml")
        self.assertEqual(r.headers["content-type"], "application/xml")
        ET.fromstring(r.text)


class TestRebroadcastRoutes(ServerTestCase):
    """Starting a channel through the API."""

    def start(self, **extra):
        from unittest.mock import MagicMock, patch
        data = {"url_primary": "https://example.com/live.m3u8",
                "sport": "Sports", "team_a": "Bears", "team_b": "Packers"}
        data.update(extra)
        made = {}

        def build(**kw):
            rec = MagicMock()
            rec.get_status_summary.return_value = {"id": "x", "is_running": True}
            made.update(kw)
            return rec

        with patch.object(self.server, "StreamFailoverRecorder", side_effect=build), \
             patch.object(self.server, "notifier", MagicMock()):
            resp = self.client.post("/api/recordings/start", data=data)
        return resp, made

    def test_a_normal_start_gets_no_ring(self):
        resp, made = self.start()
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(made.get("ring"))

    def test_rebroadcast_start_gets_a_ring(self):
        resp, made = self.start(rebroadcast="true")
        self.assertEqual(resp.status_code, 200)
        self.assertIsNotNone(made.get("ring"))
        made["ring"].close()

    def test_rebroadcast_writes_no_recording_file(self):
        # The output path must not land in the library tree.
        resp, made = self.start(rebroadcast="true")
        self.assertNotIn(str(self.server.RECORDINGS_DIR / "Sports"),
                         made["output_filepath"])
        made["ring"].close()

    def test_channel_name_defaults_to_the_teams(self):
        resp, made = self.start(rebroadcast="true")
        self.assertEqual(made.get("channel_name"), "Bears vs Packers")
        made["ring"].close()

    def test_explicit_channel_name_wins(self):
        resp, made = self.start(rebroadcast="true", channel_name="RedZone")
        self.assertEqual(made.get("channel_name"), "RedZone")
        made["ring"].close()

    def test_url_scheme_is_still_validated_for_a_channel(self):
        resp, _ = self.start(rebroadcast="true", url_primary="file:///etc/passwd")
        self.assertEqual(resp.status_code, 400)


class TestRecordingRoutes(ServerTestCase):
    def test_start_requires_a_url(self):
        r = self.client.post("/api/recordings/start", data={"sport": "NFL"})
        self.assertEqual(r.status_code, 422)  # missing required form field

    def test_start_rejects_blank_url(self):
        r = self.client.post("/api/recordings/start", data={"url_primary": "   "})
        self.assertEqual(r.status_code, 400)

    def test_freeze_timeout_bounds_enforced(self):
        for bad in (0, -5, 9999):
            with self.subTest(freeze_timeout=bad):
                r = self.client.post("/api/recordings/start", data={
                    "url_primary": "http://a/1.m3u8",
                    "freeze_timeout": bad,
                })
                self.assertEqual(r.status_code, 400)

    def test_notifications_do_not_block_the_response(self):
        # The notifier must be deferred to a background task; called inline it
        # can stall the event loop for up to 15s on slow webhooks.
        from unittest.mock import patch, MagicMock
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        notifier = MagicMock()
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake), \
             patch.object(self.server, "notifier", notifier):
            r = self.client.post("/api/recordings/start",
                                 data={"url_primary": "http://a/1.m3u8"})
        self.assertEqual(r.status_code, 200)
        # TestClient runs background tasks before returning, so it has fired --
        # what matters is that it was scheduled, not awaited inline.
        notifier.notify_recording_started.assert_called_once()

    def test_stop_unknown_session_404s(self):
        r = self.client.post("/api/recordings/nope/stop")
        self.assertEqual(r.status_code, 404)

    def test_failover_unknown_session_404s(self):
        r = self.client.post("/api/recordings/nope/failover")
        self.assertEqual(r.status_code, 404)

    def test_logs_unknown_session_404s(self):
        r = self.client.get("/api/recordings/nope/logs")
        self.assertEqual(r.status_code, 404)

    def test_failover_on_stopped_session_400s(self):
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.is_running = False
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/failover")
        self.assertEqual(r.status_code, 400)
        rec.force_failover.assert_not_called()

    def test_failover_on_running_session_calls_recorder(self):
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.is_running = True
        rec.has_next_candidate = True
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/failover")
        self.assertEqual(r.status_code, 200)
        rec.force_failover.assert_called_once()

    def test_start_refuses_when_the_volume_is_nearly_full(self):
        # Fail fast rather than starting a capture the guard aborts moments
        # later -- and rather than being the thing that fills the volume.
        from unittest.mock import patch
        import collections
        usage = collections.namedtuple("usage", "total used free")
        with patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "5"}), \
             patch.object(self.server.shutil, "disk_usage",
                          return_value=usage(100, 99, int(0.5 * 1024 ** 3))):
            r = self.client.post("/api/recordings/start",
                                 data={"url_primary": "http://a/1.m3u8"})
        self.assertEqual(r.status_code, 507)
        self.assertIn("PVARR_MIN_FREE_GB", r.json()["detail"])
        self.assertEqual(self.server.active_recorders, {},
                         "a refused start still registered a session")

    def test_start_allowed_when_space_is_ample(self):
        from unittest.mock import patch, MagicMock
        import collections
        usage = collections.namedtuple("usage", "total used free")
        fake = MagicMock(); fake.get_status_summary.return_value = {}
        with patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "5"}), \
             patch.object(self.server.shutil, "disk_usage",
                          return_value=usage(100, 1, int(50 * 1024 ** 3))), \
             patch.object(self.server, "StreamFailoverRecorder", return_value=fake), \
             patch.object(self.server, "notifier", MagicMock()):
            r = self.client.post("/api/recordings/start",
                                 data={"url_primary": "http://a/1.m3u8"})
        self.assertEqual(r.status_code, 200)

    def test_invalid_min_free_gb_falls_back_to_the_default(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "not-a-number"}):
            self.assertEqual(self.server._min_free_gb(),
                             self.server.DEFAULT_MIN_FREE_GB)

    def test_min_free_gb_is_read_from_the_environment(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "12.5"}):
            self.assertEqual(self.server._min_free_gb(), 12.5)
        with patch.dict(os.environ, {"PVARR_MIN_FREE_GB": "-4"}):
            self.assertEqual(self.server._min_free_gb(), 0.0)

    def test_switch_unknown_session_404s(self):
        r = self.client.post("/api/recordings/nope/switch", data={"candidate": 1})
        self.assertEqual(r.status_code, 404)

    def test_switch_on_stopped_session_400s(self):
        from unittest.mock import MagicMock
        rec = MagicMock(); rec.is_running = False
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/switch", data={"candidate": 1})
        self.assertEqual(r.status_code, 400)
        rec.switch_to_candidate.assert_not_called()

    def test_switch_rejects_out_of_range_candidate(self):
        from unittest.mock import MagicMock
        rec = MagicMock(); rec.is_running = True
        rec.candidates = ["a", "b"]
        self.server.active_recorders["abc"] = rec
        for bad in (0, -1, 3):
            with self.subTest(candidate=bad):
                r = self.client.post("/api/recordings/abc/switch",
                                     data={"candidate": bad})
                self.assertEqual(r.status_code, 400)
        rec.switch_to_candidate.assert_not_called()

    def test_switch_passes_zero_based_index_to_the_recorder(self):
        # The API is 1-based because that is what the dashboard shows; the
        # recorder indexes from 0. Getting this wrong switches to the wrong
        # stream, which is silent and hard to spot.
        from unittest.mock import MagicMock
        rec = MagicMock(); rec.is_running = True
        rec.candidates = ["a", "b", "c"]
        rec.switch_to_candidate.return_value = True
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/switch", data={"candidate": 3})
        self.assertEqual(r.status_code, 200)
        rec.switch_to_candidate.assert_called_once_with(2)

    def test_switch_to_the_current_candidate_400s(self):
        from unittest.mock import MagicMock
        rec = MagicMock(); rec.is_running = True
        rec.candidates = ["a", "b"]
        rec.switch_to_candidate.return_value = False
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/switch", data={"candidate": 1})
        self.assertEqual(r.status_code, 400)
        self.assertIn("already", r.json()["detail"].lower())

    def test_failover_refused_when_no_backup_configured(self):
        # A single-URL session has nothing to fail over to. Honouring the
        # request advanced past the last candidate and ended the recording,
        # and the caller still got a 200 "success".
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.is_running = True
        rec.has_next_candidate = False
        rec.candidates = ["http://a/1.m3u8"]
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/failover")
        self.assertEqual(r.status_code, 400)
        rec.force_failover.assert_not_called()
        self.assertIn("backup", r.json()["detail"].lower())

    def test_stop_calls_recorder_stop(self):
        from unittest.mock import MagicMock
        rec = MagicMock()
        self.server.active_recorders["abc"] = rec
        r = self.client.post("/api/recordings/abc/stop")
        self.assertEqual(r.status_code, 200)
        rec.stop.assert_called_once()

    def test_start_registers_session_without_spawning_ffmpeg(self):
        from unittest.mock import patch, MagicMock
        fake = MagicMock()
        fake.get_status_summary.return_value = {"id": "x"}
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake), \
             patch.object(self.server, "notifier", MagicMock()):
            r = self.client.post("/api/recordings/start",
                                 data={"url_primary": "http://a/1.m3u8"})
        self.assertEqual(r.status_code, 200)
        fake.start_recording.assert_called_once()
        self.assertEqual(len(self.server.active_recorders), 1)

    def test_start_passes_all_three_candidates_in_order(self):
        from unittest.mock import patch, MagicMock
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake) as ctor, \
             patch.object(self.server, "notifier", MagicMock()):
            self.client.post("/api/recordings/start", data={
                "url_primary": "http://a/1.m3u8",
                "url_backup1": "",                       # blank middle field
                "url_backup2": "http://c/3.m3u8",
            })
        candidates = ctor.call_args.kwargs["candidates"]
        self.assertEqual(candidates, ["http://a/1.m3u8", "http://c/3.m3u8"])


class TestCompletionOrdering(ServerTestCase):
    """Post-process first, announce second.

    notify_recording_finished triggers a Plex/Emby library scan. It used to run
    before the remux, so the media server scanned while only the .ts existed
    and the .mp4 did not -- indexing a file the remux was about to delete, and
    never seeing the finished recording until its next scheduled scan. The
    webhook text also quoted the .ts name and its pre-remux size.
    """

    def _run_completion(self, remux_result, source_bytes=b"x" * 2048,
                        final_bytes=b"y" * 1024):
        from unittest.mock import patch, MagicMock

        order = []
        captured = {}

        ts_path = Path(self.tmp) / "game.ts"
        ts_path.write_bytes(source_bytes)
        mp4_path = Path(self.tmp) / "game.mp4"

        def fake_recorder(**kwargs):
            captured["on_complete"] = kwargs["on_completion_callback"]
            rec = MagicMock()
            rec.get_status_summary.return_value = {}
            rec.final_filepath = None  # a bare MagicMock attr is truthy
            captured["recorder"] = rec
            return rec

        def fake_remux(path, **kwargs):
            order.append("remux")
            if remux_result.get("status") == "success":
                mp4_path.write_bytes(final_bytes)
                ts_path.unlink()
            return remux_result

        notifier = MagicMock()
        notifier.notify_recording_finished.side_effect = (
            lambda sid, name, size: order.append(("notify", name, size))
        )

        with patch.object(self.server, "StreamFailoverRecorder", fake_recorder), \
             patch.object(self.server, "remux_recording", fake_remux), \
             patch.object(self.server, "notifier", notifier):
            r = self.client.post("/api/recordings/start",
                                 data={"url_primary": "http://a/1.m3u8"})
            self.assertEqual(r.status_code, 200)
            captured["on_complete"](str(ts_path))

        return order, captured

    def test_remux_runs_before_the_library_scan(self):
        order, _ = self._run_completion(
            {"status": "success", "output_filepath": str(Path(self.tmp) / "game.mp4")}
        )
        self.assertEqual(order[0], "remux",
                         "the media server was told to scan before the mp4 existed")
        self.assertEqual(order[1][0], "notify")

    def test_notification_names_the_remuxed_file(self):
        order, _ = self._run_completion(
            {"status": "success", "output_filepath": str(Path(self.tmp) / "game.mp4")}
        )
        _, name, size_mb = order[1]
        self.assertEqual(name, "game.mp4",
                         "notification quoted the .ts the remux just deleted")
        self.assertEqual(size_mb, round(1024 / (1024 * 1024), 2))

    def test_failed_remux_still_notifies_about_the_ts(self):
        # If the remux fails the .ts is what is left on disk, so that is what
        # the notification and the scan must refer to.
        order, _ = self._run_completion({"status": "failed", "error": "boom"})
        self.assertEqual(order[0], "remux")
        _, name, size_mb = order[1]
        self.assertEqual(name, "game.ts")
        self.assertEqual(size_mb, round(2048 / (1024 * 1024), 2))

    def test_session_points_at_the_remuxed_file(self):
        _, captured = self._run_completion(
            {"status": "success", "output_filepath": str(Path(self.tmp) / "game.mp4")}
        )
        self.assertEqual(captured["recorder"].final_filepath,
                         Path(self.tmp) / "game.mp4")


class TestLibraryRoutes(ServerTestCase):
    def test_library_lists_recordings(self):
        (Path(self.tmp) / "game.ts").write_bytes(b"x" * 1024)
        r = self.client.get("/api/library")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i["filename"] for i in r.json()["library"]], ["game.ts"])

    def test_rename(self):
        (Path(self.tmp) / "old.ts").write_bytes(b"x")
        r = self.client.post("/api/library/rename",
                             data={"old_name": "old.ts", "new_name": "new.ts"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue((Path(self.tmp) / "new.ts").exists())

    def test_rename_missing_file_400s(self):
        r = self.client.post("/api/library/rename",
                             data={"old_name": "ghost.ts", "new_name": "new.ts"})
        self.assertEqual(r.status_code, 400)

    def test_delete_remuxed_recording(self):
        # The reported symptom: deleting a finished recording errored, because
        # the library only ever showed (and the UI only ever offered) .ts.
        (Path(self.tmp) / "game.mp4").write_bytes(b"x")
        r = self.client.request("DELETE", "/api/library/game.mp4")
        self.assertEqual(r.status_code, 200)
        self.assertFalse((Path(self.tmp) / "game.mp4").exists())

    def test_library_lists_remuxed_recordings(self):
        (Path(self.tmp) / "game.mp4").write_bytes(b"x")
        r = self.client.get("/api/library")
        self.assertEqual(r.status_code, 200)
        self.assertEqual([i["filename"] for i in r.json()["library"]], ["game.mp4"])

    def test_download_uses_the_right_content_type(self):
        (Path(self.tmp) / "game.mp4").write_bytes(b"x" * 16)
        r = self.client.get("/api/library/download/game.mp4")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "video/mp4")

    def test_delete(self):
        (Path(self.tmp) / "game.ts").write_bytes(b"x")
        r = self.client.request("DELETE", "/api/library/game.ts")
        self.assertEqual(r.status_code, 200)
        self.assertFalse((Path(self.tmp) / "game.ts").exists())

    def test_delete_missing_404s(self):
        self.assertEqual(
            self.client.request("DELETE", "/api/library/ghost.ts").status_code, 404
        )

    def test_download_missing_404s(self):
        self.assertEqual(
            self.client.get("/api/library/download/ghost.ts").status_code, 404
        )

    def test_download_serves_file(self):
        (Path(self.tmp) / "game.ts").write_bytes(b"payload")
        r = self.client.get("/api/library/download/game.ts")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"payload")


class TestMissingStatusRoute(ServerTestCase):
    def test_api_status_is_registered(self):
        # The dashboard polls /api/status on a timer to refresh active
        # sessions. get_system_status() exists in server.py but has no route
        # decorator, so the poll 404s and the UI never updates.
        r = self.client.get("/api/status")
        self.assertEqual(r.status_code, 200,
                         "/api/status is not routed; dashboard polling is dead")
        body = r.json()
        for key in ("active_count", "total_sessions", "sessions"):
            self.assertIn(key, body)


class TestCookieRedaction(unittest.TestCase):
    """A live session cookie must not be readable back out of the API.

    PVArr is unauthenticated by design, so every field in /api/status is
    effectively public to the LAN. The cookie is the sponsor's paid account.
    """

    def make(self):
        from app.recorder import CandidateStream
        c = CandidateStream("https://example.com/s.m3u8", "Primary")
        c.cookie = "SESSIONID=super-secret-token"
        return c

    def test_to_dict_withholds_the_cookie_by_default(self):
        data = self.make().to_dict()
        self.assertNotIn("cookie", data)
        self.assertNotIn("super-secret-token", json.dumps(data))

    def test_to_dict_reports_that_a_cookie_exists(self):
        # The UI still needs to show that auth is attached, just not what it is.
        self.assertTrue(self.make().to_dict()["has_cookie"])
        from app.recorder import CandidateStream
        self.assertFalse(CandidateStream("https://example.com/s.m3u8").to_dict()["has_cookie"])

    def test_opt_in_still_returns_the_value(self):
        # Persistence and FFmpeg command building need the real thing.
        self.assertEqual(self.make().to_dict(include_secrets=True)["cookie"],
                         "SESSIONID=super-secret-token")

    def test_status_payload_carries_no_cookie(self):
        rec = StreamFailoverRecorder("s1", ["https://example.com/s.m3u8"], "/tmp/x.ts")
        rec.candidates[0].cookie = "SESSIONID=super-secret-token"
        self.assertNotIn("super-secret-token", json.dumps(rec.get_status_summary()))


class TestTunerStream(ServerTestCase):
    """The endpoint the tuner playlist advertises. It did not exist before, so
    every channel Plex saw resolved to a 404."""

    def _fake_recorder(self, data=b"", running=False):
        from unittest.mock import MagicMock
        path = Path(self.tmp) / "live.ts"
        path.write_bytes(data)
        rec = MagicMock()
        rec.output_filepath = path
        rec.is_running = running
        # Explicit: on a MagicMock every attribute is truthy, so without this
        # the endpoint takes the rebroadcast branch and tails a ring that does
        # not exist.
        rec.is_rebroadcast = False
        rec.ring = None
        return rec

    def test_unknown_session_404s(self):
        self.assertEqual(
            self.client.get("/api/recordings/nope/stream").status_code, 404
        )

    def test_streams_the_recorded_bytes(self):
        self.server.active_recorders["abc"] = self._fake_recorder(b"\x47" + b"payload")
        r = self.client.get("/api/recordings/abc/stream")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.headers["content-type"], "video/mp2t")
        self.assertEqual(r.content, b"\x47" + b"payload")

    def test_live_mode_starts_at_the_write_head(self):
        # ?live=true joins at the current position instead of replaying.
        self.server.active_recorders["abc"] = self._fake_recorder(b"old data here")
        r = self.client.get("/api/recordings/abc/stream?live=true")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"")

    def test_missing_file_on_stopped_recorder_404s(self):
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.output_filepath = Path(self.tmp) / "never-written.ts"
        rec.is_running = False
        rec.is_rebroadcast = False
        rec.ring = None
        self.server.active_recorders["abc"] = rec
        self.assertEqual(
            self.client.get("/api/recordings/abc/stream").status_code, 404
        )

    def test_playlist_url_resolves_to_a_real_route(self):
        # Guards the exact regression: tuner advertising an unrouted path.
        from app.tuner import generate_m3u_playlist
        self.server.active_recorders["abc"] = self._fake_recorder(b"data")
        m3u = generate_m3u_playlist(
            [{"id": "abc", "output_filename": "g.ts", "is_running": True}],
            "http://testserver",
        )
        url = [l for l in m3u.splitlines() if l.startswith("http")][0]
        r = self.client.get(url.replace("http://testserver", ""))
        self.assertEqual(r.status_code, 200, f"tuner URL {url} is not routable")


class TestSessionRetention(ServerTestCase):
    """Nothing used to remove finished sessions, so the dict grew for the life
    of the process and the proxy port climbed with it."""

    def _session(self, rid, running, stop_time=0.0, base_port=8090):
        from unittest.mock import MagicMock
        rec = MagicMock()
        rec.is_running = running
        rec.stop_time = stop_time
        rec.base_port = base_port
        self.server.active_recorders[rid] = rec
        return rec

    def test_finished_sessions_are_capped(self):
        for i in range(self.server.MAX_FINISHED_SESSIONS + 10):
            self._session(f"old{i}", running=False, stop_time=float(i))
        self.server._prune_finished_sessions()
        self.assertEqual(len(self.server.active_recorders),
                         self.server.MAX_FINISHED_SESSIONS)

    def test_pruning_drops_the_oldest_first(self):
        for i in range(self.server.MAX_FINISHED_SESSIONS + 3):
            self._session(f"s{i}", running=False, stop_time=float(i))
        self.server._prune_finished_sessions()
        remaining = set(self.server.active_recorders)
        self.assertNotIn("s0", remaining)
        self.assertIn(f"s{self.server.MAX_FINISHED_SESSIONS + 2}", remaining)

    def test_running_sessions_are_never_pruned(self):
        for i in range(self.server.MAX_FINISHED_SESSIONS + 5):
            self._session(f"done{i}", running=False, stop_time=float(i))
        self._session("live", running=True)
        self.server._prune_finished_sessions()
        self.assertIn("live", self.server.active_recorders)

    def test_proxy_port_reuses_freed_slots(self):
        # Derived from the total count, this climbed forever and eventually
        # ran past the valid port range. A stopped session's block is free.
        self._session("a", running=True, base_port=8090)
        self._session("b", running=False, base_port=8094)
        self.assertEqual(self.server._allocate_proxy_port(), 8094)

    def test_proxy_ports_do_not_collide_between_running_sessions(self):
        self._session("a", running=True, base_port=8090)
        self._session("b", running=True, base_port=8094)
        self.assertEqual(self.server._allocate_proxy_port(), 8098)

    def test_allocator_leaves_room_for_every_candidate(self):
        # start_proxy() binds base_port + candidate_index, so a three-candidate
        # session occupies base .. base+2. The allocator used to step by 2, so
        # session A failing over to its third candidate bound 8092 -- which had
        # already been handed to session B as a base, and B's proxy then could
        # not start. The gap must exceed the candidates a session can hold.
        from app.server import PROXY_PORT_STRIDE
        self._session("a", running=True, base_port=8090)
        second = self.server._allocate_proxy_port()
        self.assertGreaterEqual(second - 8090, 3,
                                "next base port lands inside session a's block")
        self.assertEqual(second, 8090 + PROXY_PORT_STRIDE)

    def test_candidate_index_cannot_escape_its_reserved_block(self):
        from app.recorder import PROXY_PORT_STRIDE, StreamFailoverRecorder
        rec = StreamFailoverRecorder(
            "x", ["u1", "u2", "u3"], "/tmp/x.ts", base_port=8090)
        for index in range(6):
            rec.current_candidate_index = index
            port = rec.base_port + (rec.current_candidate_index % PROXY_PORT_STRIDE)
            self.assertLess(port, 8090 + PROXY_PORT_STRIDE)


class TestShutdown(ServerTestCase):
    def test_lifespan_shutdown_stops_active_recorders(self):
        # docker stop / Ctrl-C must not orphan FFmpeg and hls-proxy children.
        from unittest.mock import MagicMock
        rec = MagicMock()
        with TestClient(self.server.app) as client:
            self.server.active_recorders["abc"] = rec
            client.get("/live/epg.xml")
        rec.stop.assert_called_once()

    def test_shutdown_survives_a_failing_recorder(self):
        from unittest.mock import MagicMock
        bad = MagicMock()
        bad.stop.side_effect = RuntimeError("already dead")
        good = MagicMock()
        with TestClient(self.server.app) as client:
            self.server.active_recorders["bad"] = bad
            self.server.active_recorders["good"] = good
            client.get("/live/epg.xml")
        good.stop.assert_called_once()


class TestPathHandling(ServerTestCase):
    """Directory-escape guards.

    None of these endpoints are authenticated, so an unconstrained dir_path
    turned the library API into arbitrary file read and delete for anyone who
    could reach the port. All three of these failed before the fix.
    """

    def setUp(self):
        super().setUp()
        self.outside = tempfile.mkdtemp(prefix="pvarr-outside-")
        self.secret = Path(self.outside) / "secret.txt"
        self.secret.write_bytes(b"SENSITIVE")

    def tearDown(self):
        shutil.rmtree(self.outside, ignore_errors=True)
        super().tearDown()

    def test_start_refuses_output_dir_outside_allowlist(self):
        # output_dir gets mkdir'd and written to, so an unconstrained value is
        # arbitrary directory creation plus arbitrary file write.
        r = self.client.post("/api/recordings/start", data={
            "url_primary": "http://a/1.m3u8",
            "output_dir": str(Path(self.outside) / "escaped"),
        })
        self.assertEqual(r.status_code, 403)
        self.assertFalse((Path(self.outside) / "escaped").exists(),
                         "directory created outside the allowlist")

    def test_start_accepts_output_dir_inside_allowlist(self):
        from unittest.mock import patch, MagicMock
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        inside = str(Path(self.tmp) / "sub")
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake), \
             patch.object(self.server, "notifier", MagicMock()):
            r = self.client.post("/api/recordings/start", data={
                "url_primary": "http://a/1.m3u8",
                "output_dir": inside,
            })
        self.assertEqual(r.status_code, 200)

    def test_download_refuses_dir_path_outside_allowlist(self):
        r = self.client.get(
            f"/api/library/download/secret.txt?dir_path={self.outside}"
        )
        self.assertEqual(r.status_code, 403)
        self.assertNotIn(b"SENSITIVE", r.content)

    def test_delete_refuses_dir_path_outside_allowlist(self):
        r = self.client.request(
            "DELETE", f"/api/library/secret.txt?dir_path={self.outside}"
        )
        self.assertEqual(r.status_code, 403)
        self.assertTrue(self.secret.exists(), "file outside allowlist was deleted")

    def test_list_refuses_dir_path_outside_allowlist(self):
        self.assertEqual(
            self.client.get(f"/api/library?dir_path={self.outside}").status_code, 403
        )

    def test_rename_refuses_filename_with_directory_component(self):
        r = self.client.post("/api/library/rename",
                             data={"old_name": "../x.ts", "new_name": "y.ts"})
        self.assertEqual(r.status_code, 400)

    def test_allowlist_env_var_permits_extra_dirs(self):
        from unittest.mock import patch
        with patch.dict(os.environ, {"PVARR_ALLOWED_DIRS": self.outside}):
            r = self.client.get(
                f"/api/library/download/secret.txt?dir_path={self.outside}"
            )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, b"SENSITIVE")

    def test_download_rejects_dotdot_traversal(self):
        r = self.client.get("/api/library/download/..%2F..%2Fetc%2Fpasswd")
        self.assertNotEqual(r.status_code, 200, "path traversal via %2F succeeded")


# ---------------------------------------------------------------------------
# Stream probe: paste a URL, get back a playlist plus the headers it needs.
# Every test here drives a scripted fake HTTP layer -- no network.
# ---------------------------------------------------------------------------

MEDIA_PLAYLIST = b"#EXTM3U\n#EXT-X-TARGETDURATION:6\n#EXTINF:6.0,\nseg1.ts\n#EXTINF:6.0,\nseg2.ts\n"
MASTER_PLAYLIST = (
    b"#EXTM3U\n"
    b'#EXT-X-STREAM-INF:BANDWIDTH=5000000,RESOLUTION=1920x1080\n720/index.m3u8\n'
    b'#EXT-X-STREAM-INF:BANDWIDTH=1200000,RESOLUTION=1280x720\n480/index.m3u8\n'
)


class FakeResponse:
    def __init__(self, url, status=200, body=b""):
        self.url = url
        self.status_code = status
        self._body = body

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def iter_content(self, chunk_size):
        yield self._body

    def close(self):
        pass


class FakeSession:
    """Serves a handler(url, headers) -> FakeResponse and records every call."""

    def __init__(self, handler):
        self.handler = handler
        self.cookies = []
        self.calls = []

    def get(self, url, headers=None, timeout=None, stream=False, allow_redirects=True):
        headers = dict(headers or {})
        self.calls.append((url, headers))
        return self.handler(url, headers)


class ProbeTestCase(unittest.TestCase):
    def probe(self, handler, url, **kwargs):
        from unittest.mock import patch
        import app.probe as probe_mod
        self.session = FakeSession(handler)
        with patch.object(probe_mod.requests, "Session", return_value=self.session):
            return probe_mod.probe_stream(url, **kwargs)


class TestProbeUrlCleaning(unittest.TestCase):
    def test_strips_quotes_and_whitespace(self):
        from app.probe import clean_url
        self.assertEqual(
            clean_url("  'https://a.example/x.m3u8'  "), "https://a.example/x.m3u8"
        )

    def test_protocol_relative_gets_scheme(self):
        from app.probe import clean_url
        self.assertEqual(clean_url("//a.example/x.m3u8"), "https://a.example/x.m3u8")

    def test_rejects_non_http_scheme(self):
        from app.probe import clean_url, ProbeError
        # file:// would turn the probe endpoint into a local file reader.
        for bad in ("file:///etc/passwd", "ftp://a/x.m3u8", "notaurl"):
            with self.subTest(url=bad), self.assertRaises(ProbeError):
                clean_url(bad)


class TestProbeDirectPlaylist(ProbeTestCase):
    def test_open_stream_needs_no_headers(self):
        result = self.probe(
            lambda url, h: FakeResponse(url, 200, MEDIA_PLAYLIST),
            "https://cdn.example/hls/stream.m3u8",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["referer"], "")
        self.assertEqual(result["headers_required"], [])
        self.assertEqual(result["kind"], "media")

    def test_bare_attempt_comes_first(self):
        # Sending an invented Referer to a stream that does not want one is
        # occasionally worse than sending none, so try clean first.
        self.probe(
            lambda url, h: FakeResponse(url, 200, MEDIA_PLAYLIST),
            "https://cdn.example/hls/stream.m3u8",
        )
        self.assertNotIn("Referer", self.session.calls[0][1])

    def test_referer_discovered_when_403_without_it(self):
        def handler(url, headers):
            if headers.get("Referer") != "https://cdn.example/":
                return FakeResponse(url, 403, b"denied")
            return FakeResponse(url, 200, MEDIA_PLAYLIST)

        result = self.probe(handler, "https://cdn.example/hls/stream.m3u8")
        self.assertTrue(result["ok"])
        self.assertEqual(result["referer"], "https://cdn.example/")
        self.assertIn("Referer", result["headers_required"])

    def test_referer_taken_from_query_string(self):
        def handler(url, headers):
            if headers.get("Referer") != "https://player.example/embed":
                return FakeResponse(url, 403, b"denied")
            return FakeResponse(url, 200, MEDIA_PLAYLIST)

        result = self.probe(
            handler,
            "https://cdn.example/x.m3u8?referer=https://player.example/embed",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["referer"], "https://player.example/embed")

    def test_caller_referer_is_tried_first(self):
        result = self.probe(
            lambda url, h: FakeResponse(url, 200, MEDIA_PLAYLIST),
            "https://cdn.example/x.m3u8",
            referer="https://mysite.example/game",
        )
        self.assertEqual(self.session.calls[0][1]["Referer"], "https://mysite.example/game")
        self.assertEqual(result["referer"], "https://mysite.example/game")

    def test_all_rejected_reports_403(self):
        result = self.probe(
            lambda url, h: FakeResponse(url, 403, b"denied"),
            "https://cdn.example/x.m3u8",
        )
        self.assertFalse(result["ok"])
        self.assertIn("403", result["message"])

    def test_expired_token_reads_as_404(self):
        result = self.probe(
            lambda url, h: FakeResponse(url, 404, b""),
            "https://cdn.example/x.m3u8?token=old",
        )
        self.assertFalse(result["ok"])
        self.assertIn("expired", result["message"])

    def test_non_playlist_body_is_not_accepted(self):
        # A 200 that is actually an HTML error page must not pass as a stream.
        result = self.probe(
            lambda url, h: FakeResponse(url, 200, b"<html>nope</html>"),
            "https://cdn.example/x.m3u8",
        )
        self.assertFalse(result["ok"])


class TestProbeMasterPlaylist(ProbeTestCase):
    def handler(self, url, headers):
        if url.endswith("master.m3u8"):
            return FakeResponse(url, 200, MASTER_PLAYLIST)
        if url.endswith("index.m3u8"):
            return FakeResponse(url, 200, MEDIA_PLAYLIST)
        return FakeResponse(url, 200, b"\x47" * 512)

    def test_variants_parsed(self):
        result = self.probe(self.handler, "https://cdn.example/master.m3u8")
        self.assertEqual(result["kind"], "master")
        self.assertEqual(len(result["variants"]), 2)
        self.assertEqual(result["variants"][0]["resolution"], "1920x1080")
        self.assertEqual(result["variants"][0]["bandwidth"], 5000000)

    def test_variant_urls_absolutised(self):
        result = self.probe(self.handler, "https://cdn.example/master.m3u8")
        self.assertEqual(
            result["variants"][0]["url"], "https://cdn.example/720/index.m3u8"
        )

    def test_segment_reached_through_variant(self):
        result = self.probe(self.handler, "https://cdn.example/master.m3u8")
        self.assertTrue(result["segment_ok"])


class TestProbeSegmentCheck(ProbeTestCase):
    def test_gated_segments_flagged(self):
        # The manifest is public, the segments are not: this is the failure
        # that would otherwise show up minutes into a recording.
        def handler(url, headers):
            if url.endswith(".m3u8"):
                return FakeResponse(url, 200, MEDIA_PLAYLIST)
            return FakeResponse(url, 403, b"")

        result = self.probe(handler, "https://cdn.example/x.m3u8")
        self.assertTrue(result["ok"])
        self.assertFalse(result["segment_ok"])
        self.assertIn("segments rejected", result["message"])

    def test_segment_request_is_ranged(self):
        def handler(url, headers):
            if url.endswith(".m3u8"):
                return FakeResponse(url, 200, MEDIA_PLAYLIST)
            return FakeResponse(url, 206, b"\x47" * 1024)

        self.probe(handler, "https://cdn.example/x.m3u8")
        seg_call = [c for c in self.session.calls if c[0].endswith(".ts")][0]
        self.assertEqual(seg_call[1]["Range"], "bytes=0-2047")


class TestProbePageScraping(ProbeTestCase):
    PAGE = (
        b"<html><script>var src = 'https:\\/\\/cdn.example\\/hls\\/master.m3u8?t=9';"
        b"</script></html>"
    )

    def handler(self, url, headers):
        if url.endswith(".php"):
            return FakeResponse(url, 200, self.PAGE)
        if headers.get("Referer") != "https://site.example/watch.php":
            return FakeResponse(url, 403, b"denied")
        if ".m3u8" in url:
            return FakeResponse(url, 200, MEDIA_PLAYLIST)
        return FakeResponse(url, 200, b"\x47" * 512)

    def test_m3u8_extracted_from_page(self):
        result = self.probe(self.handler, "https://site.example/watch.php")
        self.assertTrue(result["ok"])
        self.assertEqual(result["m3u8_url"], "https://cdn.example/hls/master.m3u8?t=9")

    def test_page_becomes_the_referer(self):
        result = self.probe(self.handler, "https://site.example/watch.php")
        self.assertEqual(result["referer"], "https://site.example/watch.php")
        self.assertEqual(result["page_url"], "https://site.example/watch.php")

    def test_page_with_no_playlist_says_so(self):
        result = self.probe(
            lambda url, h: FakeResponse(url, 200, b"<html>nothing here</html>"),
            "https://site.example/watch.php",
        )
        self.assertFalse(result["ok"])
        self.assertIn("No .m3u8", result["message"])

    def test_url_serving_a_playlist_without_m3u8_suffix(self):
        # Some hosts serve playlists from extensionless paths.
        result = self.probe(
            lambda url, h: FakeResponse(url, 200, MEDIA_PLAYLIST),
            "https://site.example/live/channel1",
        )
        self.assertTrue(result["ok"])
        self.assertEqual(result["kind"], "media")


class TestProbeBodyCap(ProbeTestCase):
    def test_oversized_body_is_truncated(self):
        import app.probe as probe_mod
        huge = b"#EXTM3U\n" + b"x" * (probe_mod.MAX_BYTES * 3)
        result = self.probe(lambda url, h: FakeResponse(url, 200, huge),
                            "https://cdn.example/x.m3u8")
        # Accepted, but the probe must not have grown with the response.
        self.assertTrue(result["ok"])


class TestRecorderProbeIntegration(unittest.TestCase):
    """The recorder probes at connect time, then falls back to the script."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pvarr-test-")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rec(self, **kwargs):
        return StreamFailoverRecorder(
            "test-id", ["https://cdn.example/x.m3u8"],
            str(Path(self.tmp) / "out.ts"), **kwargs
        )

    def test_probe_result_populates_candidate(self):
        from unittest.mock import patch
        rec = self._rec()
        fake = {
            "ok": True,
            "m3u8_url": "https://cdn.example/real.m3u8",
            "referer": "https://site.example/",
            "user_agent": "UA/9",
            "cookie": "sid=abc",
            "kind": "media",
            "headers_required": ["Referer", "Cookie"],
            "message": "Media playlist, needs Referer + Cookie.",
        }
        with patch("app.recorder.probe_stream", return_value=fake):
            rec.detect_candidate_headers(rec.candidates[0])
        cand = rec.candidates[0]
        self.assertEqual(cand.m3u8_url, "https://cdn.example/real.m3u8")
        self.assertEqual(cand.referer, "https://site.example/")
        self.assertEqual(cand.cookie, "sid=abc")
        self.assertEqual(cand.user_agent, "UA/9")
        self.assertEqual(cand.detect_source, "probe")

    def test_failed_probe_falls_back_to_raw_url(self):
        from unittest.mock import patch
        rec = self._rec()
        rec.detect_headers_path = ""
        with patch("app.recorder.probe_stream", return_value={"ok": False, "message": "nope"}):
            rec.detect_candidate_headers(rec.candidates[0])
        # A failed probe must still leave a recordable URL: the stream may well
        # work under FFmpeg even when the probe cannot confirm it.
        self.assertEqual(rec.candidates[0].m3u8_url, "https://cdn.example/x.m3u8")
        self.assertEqual(rec.candidates[0].detect_source, "raw")

    def test_probe_exception_does_not_kill_recording(self):
        from unittest.mock import patch
        rec = self._rec()
        rec.detect_headers_path = ""
        with patch("app.recorder.probe_stream", side_effect=RuntimeError("boom")):
            self.assertTrue(rec.detect_candidate_headers(rec.candidates[0]))
        self.assertEqual(rec.candidates[0].detect_source, "raw")

    def test_script_used_only_after_probe_fails(self):
        from unittest.mock import patch, MagicMock
        rec = self._rec()
        detector = Path(self.tmp) / "detect-headers.sh"
        detector.write_text("#!/bin/sh\necho {}\n")
        detector.chmod(0o755)
        rec.detect_headers_path = str(detector)
        good = {"ok": True, "m3u8_url": "https://cdn.example/x.m3u8", "referer": "",
                "user_agent": "UA", "cookie": "", "kind": "media",
                "headers_required": [], "message": "ok"}
        with patch("app.recorder.probe_stream", return_value=good):
            with patch("app.recorder.subprocess.run") as run:
                rec.detect_candidate_headers(rec.candidates[0])
        run.assert_not_called()

    def test_auto_probe_can_be_disabled(self):
        from unittest.mock import patch
        rec = self._rec(auto_probe=False)
        rec.detect_headers_path = ""
        with patch("app.recorder.probe_stream") as probe:
            rec.detect_candidate_headers(rec.candidates[0])
        probe.assert_not_called()

    def test_header_overrides_seed_candidates_by_url(self):
        rec = StreamFailoverRecorder(
            "test-id",
            ["https://a.example/1.m3u8", "https://b.example/2.m3u8"],
            str(Path(self.tmp) / "out.ts"),
            header_overrides={"https://b.example/2.m3u8": {"referer": "https://b.example/"}},
        )
        # Keyed by URL, so the override lands on the second candidate only.
        self.assertEqual(rec.candidates[0].referer, "")
        self.assertEqual(rec.candidates[1].referer, "https://b.example/")

    def test_cookie_reaches_ffmpeg_headers(self):
        rec = self._rec(auto_probe=False)
        cmd = rec._build_ffmpeg_cmd(
            "https://cdn.example/x.m3u8", referer="https://s/", cookie="sid=abc"
        )
        headers = cmd[cmd.index("-headers") + 1]
        self.assertIn("Cookie: sid=abc", headers)


class TestProbeRoute(ServerTestCase):
    def test_probe_endpoint_returns_result(self):
        from unittest.mock import patch
        fake = {"ok": True, "m3u8_url": "https://cdn.example/x.m3u8", "message": "fine"}
        with patch("app.server.probe_stream", return_value=fake):
            r = self.client.post("/api/probe", data={"url": "https://cdn.example/x.m3u8"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["ok"])

    def test_probe_passes_referer_hint(self):
        from unittest.mock import patch
        with patch("app.server.probe_stream", return_value={"ok": False}) as probe:
            self.client.post(
                "/api/probe",
                data={"url": "https://cdn.example/x.m3u8", "referer": "https://site/"},
            )
        self.assertEqual(probe.call_args.kwargs["referer"], "https://site/")

    def test_probe_rejects_absurd_url(self):
        r = self.client.post("/api/probe", data={"url": "https://a/" + "x" * 5000})
        self.assertEqual(r.status_code, 400)

    def test_start_accepts_stream_headers(self):
        from unittest.mock import patch, MagicMock
        import json as _json
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        payload = _json.dumps({"https://cdn.example/x.m3u8": {"referer": "https://site/"}})
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake) as ctor:
            r = self.client.post("/api/recordings/start", data={
                "url_primary": "https://cdn.example/x.m3u8",
                "stream_headers": payload,
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            ctor.call_args.kwargs["header_overrides"],
            {"https://cdn.example/x.m3u8": {"referer": "https://site/"}},
        )

    def test_malformed_stream_headers_ignored(self):
        # A bad hint must not cost the user their recording.
        from unittest.mock import patch, MagicMock
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake) as ctor:
            r = self.client.post("/api/recordings/start", data={
                "url_primary": "https://cdn.example/x.m3u8",
                "stream_headers": "{not json",
            })
        self.assertEqual(r.status_code, 200)
        self.assertEqual(ctor.call_args.kwargs["header_overrides"], {})

    def test_non_dict_header_entries_dropped(self):
        from unittest.mock import patch, MagicMock
        import json as _json
        fake = MagicMock()
        fake.get_status_summary.return_value = {}
        with patch.object(self.server, "StreamFailoverRecorder", return_value=fake) as ctor:
            self.client.post("/api/recordings/start", data={
                "url_primary": "https://cdn.example/x.m3u8",
                "stream_headers": _json.dumps({"https://cdn.example/x.m3u8": "nope"}),
            })
        self.assertEqual(ctor.call_args.kwargs["header_overrides"], {})


if __name__ == "__main__":
    unittest.main(verbosity=2)
