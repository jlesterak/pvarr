"""
Playlist relay - PVArr

Some origins serve their HLS playlist only to a browser-compatible TLS
client, and refuse FFmpeg (OpenSSL presents one TLS profile and cannot present
another) however correct its headers. The video segments behind the
same playlist are usually gated on Referer/Origin alone, which FFmpeg can send.

So only the playlist needs carrying. This serves it on 127.0.0.1: each time
FFmpeg reloads, the relay fetches the upstream playlist with curl_cffi's
browser TLS profile, makes every URI in it absolute, and hands it back.
Segments and keys keep their real URLs, so FFmpeg still pulls the video straight
from the CDN and the relay moves a few KB every few seconds.

It is not an open proxy: it answers only for the playlist it was started with
and for the variant/rendition playlists that playlist itself lists.
"""

import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from app.probe import NoPrivateRedirects, is_private_url, private_playlist_uri

# A master playlist lists a handful of variants and renditions; this only
# bounds memory if an upstream lists something absurd.
MAX_PLAYLISTS = 64
FETCH_TIMEOUT = 10
# Higher than the probe's 512 KB on purpose: an event playlist that keeps every
# segment with long tokenised URLs passes 512 KB in a few hours, and truncating
# it would cost video. This only stops an upstream from sending unbounded data.
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024

_URI_ATTR = re.compile(r'URI="([^"]+)"')

Fetch = Callable[[str, Dict[str, str], int], Tuple[int, bytes, str]]


def browser_tls_fetch(url: str, headers: Dict[str, str], timeout: int) -> Tuple[int, bytes, str]:
    """GET with a browser TLS profile. Returns (status, body, final URL).

    Redirects are followed hop by hop, refusing any from a public URL onto a
    private address (see `probe.NoPrivateRedirects`).
    """
    from curl_cffi import requests as curl_requests

    session = curl_requests.Session(impersonate="chrome")
    try:
        resp = NoPrivateRedirects(session).get(url, headers=headers, timeout=timeout,
                                               stream=True)
        try:
            body = b""
            for chunk in resp.iter_content():
                body += chunk
                if len(body) >= MAX_PLAYLIST_BYTES:
                    break
            return resp.status_code, body, str(resp.url)
        finally:
            resp.close()
    finally:
        session.close()


class PlaylistRelay:
    def __init__(self, url: str, headers: Dict[str, str], fetch: Optional[Fetch] = None):
        self._playlists: List[str] = [url]
        self._headers = dict(headers)
        self._fetch = fetch or browser_tls_fetch
        self._lock = threading.Lock()
        self._server: Optional[ThreadingHTTPServer] = None
        self._port = 0
        # A nested playlist on a private address is refused unless the stream
        # itself lives on one: the relay sends the stream's cookie with it.
        self._allow_private = is_private_url(url)

    def _local(self, index: int) -> str:
        # The port is fixed at start(); reading it from the server raced stop(),
        # which clears `_server` while a handler may still be rewriting.
        return f"http://127.0.0.1:{self._port}/{index}.m3u8"

    def _route(self, absolute: str) -> str:
        """Hand back a relay URL for a playlist the upstream listed."""
        with self._lock:
            if absolute not in self._playlists:
                if len(self._playlists) >= MAX_PLAYLISTS:
                    return absolute
                self._playlists.append(absolute)
            return self._local(self._playlists.index(absolute))

    def rewrite(self, text: str, base: str) -> str:
        """Absolutise every URI; send the nested playlists through the relay.

        Decided by the tag, not the extension: a variant can be `/live/720p`
        or `.m3u`, and one that slipped past the relay would 403 in FFmpeg.
        Variants follow #EXT-X-STREAM-INF; renditions and I-frame playlists
        are the URI= of #EXT-X-MEDIA / #EXT-X-I-FRAME-STREAM-INF. Segments and
        #EXT-X-KEY / #EXT-X-MAP URIs go direct.
        """
        out, variant_next = [], False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                absolute = urljoin(base, stripped)
                out.append(self._route(absolute) if variant_next else absolute)
                variant_next = False
                continue
            if stripped.startswith("#EXT-X-STREAM-INF"):
                variant_next = True
            if "URI=" in stripped:
                nested = stripped.startswith(("#EXT-X-MEDIA:", "#EXT-X-I-FRAME-STREAM-INF:"))
                line = _URI_ATTR.sub(
                    lambda m: 'URI="{}"'.format(
                        (self._route if nested else str)(urljoin(base, m.group(1)))),
                    line)
            out.append(line)
        return "\n".join(out) + "\n"

    def serve(self, index: int) -> Tuple[int, bytes]:
        with self._lock:
            if not 0 <= index < len(self._playlists):
                return 404, b""
            upstream = self._playlists[index]
        if index and not self._allow_private and is_private_url(upstream):
            return 403, b""
        try:
            status, body, final_url = self._fetch(upstream, self._headers, FETCH_TIMEOUT)
        except Exception:  # any upstream failure is FFmpeg's to retry
            return 502, b""
        if status != 200:
            return status, b""
        if not body.lstrip()[:7].upper().startswith(b"#EXTM3U"):
            return 502, b""
        text = body.decode("utf-8", errors="replace")
        base = final_url or upstream
        # Segments and keys go to FFmpeg direct: a public playlist naming a LAN
        # address would have FFmpeg fetch it. Refused whole, as FFmpeg cannot
        # skip one segment cleanly; it retries and then fails over.
        if not self._allow_private and private_playlist_uri(text, base):
            return 403, b""
        return 200, self.rewrite(text, base).encode("utf-8")

    def start(self) -> str:
        """Bind an ephemeral loopback port and return the playlist's local URL."""
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                match = re.fullmatch(r"/(\d+)\.m3u8", self.path.split("?", 1)[0])
                status, body = relay.serve(int(match.group(1))) if match else (404, b"")
                self.send_response(status)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):  # tokenised URLs stay out of the logs
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._port = self._server.server_address[1]
        threading.Thread(target=self._server.serve_forever, daemon=True,
                         name="pvarr-playlist-relay").start()
        return self._local(0)

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
