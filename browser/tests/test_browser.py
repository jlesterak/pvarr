"""
Browser sidecar tests - PVArr

Real Chromium against fixture pages served on 127.0.0.1 (two ports, so a
nested player is cross-origin). No internet. Run from the repo root:

    PVARR_BROWSER_ALLOW_LOOPBACK=1 python -m unittest discover -s browser/tests
"""

import asyncio
import json
import os
import sys
import threading
import time
import unittest
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

os.environ["PVARR_BROWSER_ALLOW_LOOPBACK"] = "1"
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import netguard  # noqa: E402
import resolver as resolver_mod  # noqa: E402

PLAYLIST = b"#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXTINF:4.0,\nseg0.ts\n"
SEGMENT_LIST = json.dumps({"sequence": 7, "segments": [
    {"url": "https://cdn.example/s7.ts", "duration": 4.0}]}).encode()


def pages(other: str) -> dict:
    """Fixture pages; `other` is the second (cross-origin) server's base URL."""
    fetch = lambda path: f"<script>fetch('{path}', {{credentials: 'include'}});</script>"
    return {
        "/xhr.html": "<html><body><video></video>" + fetch("/live/a.m3u8") + "</body></html>",
        "/outer.html": f"<html><body><iframe src='{other}/inner.html' width=640 height=360></iframe></body></html>",
        "/inner.html": "<html><body>" + fetch("/live/inner.m3u8") + "</body></html>",
        "/click.html": ("<html><body><div class='play-overlay' style='width:200px;height:100px'"
                        " onclick=\"fetch('/live/click.m3u8')\">Play</div></body></html>"),
        "/popup.html": ("<html><body><div id='player' style='width:640px;height:360px;background:#000'></div>"
                        "<script>document.getElementById('player').addEventListener('click', e => {"
                        " if (e.isTrusted) { window.open('/ad.html'); fetch('/live/popup.m3u8'); } });"
                        "</script></body></html>"),
        "/ad.html": "<html><body>ad</body></html>",
        "/json.html": "<html><body>" + fetch("/live/list.json") + "</body></html>",
        "/none.html": "<html><body><p>No stream here.</p></body></html>",
        "/lan.html": "<html><body>" + fetch("http://10.255.255.1/live/x.m3u8") + "</body></html>",
    }


class _Handler(SimpleHTTPRequestHandler):
    routes: dict = {}

    def log_message(self, *args):
        pass

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith(".m3u8"):
            body, ctype = PLAYLIST, "application/vnd.apple.mpegurl"
        elif path == "/live/list.json":
            body, ctype = SEGMENT_LIST, "application/json"
        elif path in self.routes:
            body, ctype = self.routes[path].encode(), "text/html"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)


def _serve():
    server = ThreadingHTTPServer(("127.0.0.1", 0), type("H", (_Handler,), {"routes": {}}))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


class TestNetguard(unittest.TestCase):
    def test_page_urls(self):
        self.assertIsNotNone(netguard.page_url_problem("file:///etc/passwd"))
        self.assertIsNotNone(netguard.page_url_problem("javascript:alert(1)"))
        self.assertIsNotNone(netguard.page_url_problem("http://192.168.1.1/"))
        self.assertIsNotNone(netguard.page_url_problem("http://169.254.169.254/latest/"))
        self.assertIsNotNone(netguard.page_url_problem("http://[fd00::1]/"))
        self.assertIsNotNone(netguard.page_url_problem("http://a.example/x y"))
        self.assertIsNone(netguard.page_url_problem("http://127.0.0.1:8000/"))   # loopback allowed in tests
        self.assertIsNone(netguard.page_url_problem("https://8.8.8.8/watch"))   # a public literal IP

    def test_browser_requests(self):
        self.assertTrue(netguard.request_is_private("http://10.0.0.5/a.m3u8"))
        self.assertTrue(netguard.request_is_private("https://[fe80::1]/x"))
        self.assertTrue(netguard.request_is_private("ws://192.168.0.2:9000/"))
        self.assertFalse(netguard.request_is_private("https://cdn.example/a.m3u8"))
        self.assertFalse(netguard.request_is_private("data:text/plain,hi"))
        self.assertFalse(netguard.request_is_private("http://127.0.0.1:9/x"))    # loopback allowed in tests


class TestResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.a, cls.base = _serve()
        cls.b, cls.other = _serve()
        cls.a.RequestHandlerClass.routes.update(pages(cls.other))
        cls.b.RequestHandlerClass.routes.update(pages(cls.base))
        cls.loop = asyncio.new_event_loop()
        cls.r = resolver_mod.Resolver()

    @classmethod
    def tearDownClass(cls):
        cls.loop.run_until_complete(cls.r.close())
        cls.loop.close()
        for server in (cls.a, cls.b):
            server.shutdown()
            server.server_close()

    def run_page(self, path: str, timeout: float = 20.0) -> dict:
        return self.loop.run_until_complete(self.r.resolve(self.base + path, timeout))

    def test_playlist_fetched_by_script(self):
        out = self.run_page("/xhr.html")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["kind"], "m3u8")
        self.assertTrue(out["m3u8_url"].endswith("/live/a.m3u8"))
        self.assertEqual(out["nudges"], 0)
        self.assertTrue(out["referer"].endswith("/xhr.html"))

    def test_cross_origin_nested_player(self):
        out = self.run_page("/outer.html")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["m3u8_url"].startswith(self.other))
        self.assertTrue(out["referer"].startswith(self.other))     # the frame's own Referer

    def test_click_to_play_overlay(self):
        out = self.run_page("/click.html")
        self.assertTrue(out["ok"], out)
        self.assertGreaterEqual(out["nudges"], 1)

    def test_popup_on_click_is_closed_and_stream_found(self):
        out = self.run_page("/popup.html")
        self.assertTrue(out["ok"], out)
        self.assertTrue(out["m3u8_url"].endswith("/live/popup.m3u8"))

    def test_json_segment_list(self):
        out = self.run_page("/json.html")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["kind"], "json_list")

    def test_no_stream_gives_up_within_timeout(self):
        t0 = time.monotonic()
        out = self.run_page("/none.html", timeout=6)
        self.assertFalse(out["ok"])
        self.assertLess(time.monotonic() - t0, 12)
        self.assertIn("no playlist", out["note"])

    def test_lan_playlist_is_blocked(self):
        out = self.run_page("/lan.html", timeout=6)
        self.assertFalse(out["ok"])
        self.assertIn("private addresses blocked", out["note"])


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_mod
        cls.app_mod = app_mod
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        self.app_mod._cache.clear()
        self.app_mod._busy = 0

    def test_refuses_bad_scheme_and_private_host(self):
        self.assertEqual(self.client.post("/resolve", json={"url": "file:///etc/passwd"}).status_code, 400)
        self.assertEqual(self.client.post("/resolve", json={"url": "http://192.168.1.1/"}).status_code, 400)

    def test_busy_beyond_max_pages(self):
        self.app_mod._busy = self.app_mod.MAX_PAGES
        self.assertEqual(self.client.post("/resolve", json={"url": "http://127.0.0.1:9/"}).status_code, 503)

    def test_timeout_is_clamped(self):
        self.assertEqual(self.app_mod.clamp_timeout(999), 60.0)
        self.assertEqual(self.app_mod.clamp_timeout(0.1), 5.0)
        self.assertEqual(self.app_mod.clamp_timeout(None), 45.0)

    def test_resolved_page_is_cached(self):
        calls = []

        async def fake(url, timeout):
            calls.append(url)
            return {"ok": True, "m3u8_url": "https://cdn.example/a.m3u8"}
        real = self.app_mod.resolver.resolve
        self.app_mod.resolver.resolve = fake
        try:
            first = self.client.post("/resolve", json={"url": "http://127.0.0.1:9/p"}).json()
            second = self.client.post("/resolve", json={"url": "http://127.0.0.1:9/p"}).json()
        finally:
            self.app_mod.resolver.resolve = real
        self.assertEqual(len(calls), 1)
        self.assertFalse(first["cached"])
        self.assertTrue(second["cached"])

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)


if __name__ == "__main__":
    unittest.main()
