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
        "/ws.html": "<html><body><script>new WebSocket('ws://10.255.255.1:9/live');</script></body></html>",
        # Freezes its own main thread: every call into the page would wait forever.
        "/hang.html": "<html><body><script>setTimeout(() => { for (;;) {} }, 50);</script></body></html>",
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
    def test_a_host_name_that_resolves_to_a_private_address_is_blocked(self):
        # A page reaching a container by its Docker name (PVArr shares a
        # network with the sidecar), not just by literal IP.
        from unittest.mock import patch

        async def fake_getaddrinfo(host, port):
            ips = {"pvarr": "172.20.0.3", "cdn.example": "93.184.216.34"}
            if host not in ips:
                raise OSError("no such host")
            return [(2, 1, 6, "", (ips[host], 0))]

        async def check():
            loop = asyncio.get_running_loop()
            cache = {}
            with patch.object(loop, "getaddrinfo", fake_getaddrinfo):
                self.assertTrue(await netguard.request_is_private_resolved(
                    "http://pvarr:8999/api/recordings/start", cache))
                self.assertTrue(await netguard.request_is_private_resolved("ws://pvarr:8999/x", cache))
                self.assertFalse(await netguard.request_is_private_resolved(
                    "https://cdn.example/live.m3u8", cache))
                self.assertFalse(await netguard.request_is_private_resolved(
                    "https://nowhere.example/", cache))
                self.assertFalse(await netguard.request_is_private_resolved("data:text/plain,x", cache))
            self.assertEqual(cache, {"pvarr": True, "cdn.example": False, "nowhere.example": False})
        asyncio.run(check())

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
        self.assertTrue(netguard.request_is_private("http://[64:ff9b::a00:5]/x"))   # NAT64 of 10.0.0.5
        self.assertFalse(netguard.request_is_private("https://cdn.example/a.m3u8"))
        self.assertFalse(netguard.request_is_private("data:text/plain,hi"))
        self.assertFalse(netguard.request_is_private("http://127.0.0.1:9/x"))    # loopback allowed in tests

    def test_trailing_dot_names(self):
        os.environ["PVARR_BROWSER_ALLOW_LOOPBACK"] = "0"
        try:
            self.assertTrue(netguard.request_is_private("http://localhost.:8998/health"))
            self.assertTrue(netguard.request_is_private("http://app.localhost./"))
            self.assertIsNotNone(netguard.page_url_problem("http://LOCALHOST./"))
        finally:
            os.environ["PVARR_BROWSER_ALLOW_LOOPBACK"] = "1"


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

    def test_lan_websocket_is_blocked(self):
        out = self.run_page("/ws.html", timeout=6)
        self.assertFalse(out["ok"])
        self.assertIn("private addresses blocked", out["note"])

    def test_frozen_page_still_returns_in_time(self):
        t0 = time.monotonic()
        out = self.run_page("/hang.html", timeout=8)
        self.assertFalse(out["ok"])
        self.assertLess(time.monotonic() - t0, 8 + resolver_mod.CALL_LIMIT + 3)
        # ...and the shared browser still works afterwards.
        self.assertTrue(self.run_page("/xhr.html")["ok"])

    def test_user_agent_names_the_running_version(self):
        out = self.run_page("/xhr.html")
        self.assertNotIn("Headless", out["user_agent"])
        self.assertNotIn("Chrome/120.", out["user_agent"])


class TestApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from fastapi.testclient import TestClient
        import app as app_mod
        cls.app_mod = app_mod
        cls.client = TestClient(app_mod.app)

    def setUp(self):
        self.app_mod._busy = 0

    def fake_resolve(self, result=None, sleep=0.0):
        async def fake(url, timeout):
            await asyncio.sleep(sleep)
            return dict(result or {})
        real = self.app_mod.resolver.resolve
        self.app_mod.resolver.resolve = fake
        self.addCleanup(setattr, self.app_mod.resolver, "resolve", real)

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

    def test_private_playlist_is_never_returned(self):
        self.fake_resolve({"ok": True, "m3u8_url": "http://10.1.2.3/live.m3u8", "cookie": "c=1"})
        out = self.client.post("/resolve", json={"url": "http://127.0.0.1:9/p"}).json()
        self.assertFalse(out["ok"])
        self.assertEqual((out["m3u8_url"], out["cookie"]), ("", ""))
        self.assertIn("private", out["note"])

    def test_hard_limit_frees_the_slot(self):
        self.fake_resolve({"ok": True}, sleep=30)
        self.app_mod.GRACE_SEC, grace = 0.5, self.app_mod.GRACE_SEC
        self.addCleanup(setattr, self.app_mod, "GRACE_SEC", grace)
        t0 = time.monotonic()
        out = self.client.post("/resolve", json={"url": "http://127.0.0.1:9/p", "timeout": 5}).json()
        self.assertLess(time.monotonic() - t0, 8)
        self.assertFalse(out["ok"])
        self.assertEqual(self.app_mod._busy, 0)

    def test_health(self):
        self.assertEqual(self.client.get("/health").json()["ok"], True)


if __name__ == "__main__":
    unittest.main()
