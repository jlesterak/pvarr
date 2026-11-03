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

Segments packaged inside PNG images (`app/pngwrap.py`) are the exception: with
`unwrap_segments` the relay also serves the segments, fetching each image and
handing FFmpeg the MPEG-TS inside it. Then all of the video passes through
here (a few Mbps on loopback, roughly 0.5 s of CPU per segment).
"""

import re
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

from app import pngwrap
from app.probe import NoPrivateRedirects, as_playlist, is_private_url, private_playlist_uri

# A master playlist lists a handful of variants and renditions; this only
# bounds memory if an upstream lists something absurd.
MAX_PLAYLISTS = 64
FETCH_TIMEOUT = 10
# Higher than the probe's 512 KB on purpose: an event playlist that keeps every
# segment with long tokenised URLs passes 512 KB in a few hours, and truncating
# it would cost video. This only stops an upstream from sending unbounded data.
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024

# Segment URLs the relay will fetch: the newest ones each playlist listed. A
# live window holds a few dozen; older ids 404, as FFmpeg has moved past them.
MAX_SEGMENTS = 256
SEGMENT_TIMEOUT = 20
# Wrapped segments are fetched as soon as a playlist lists them, not when
# FFmpeg asks: FFmpeg reads one segment at a time, and a 3-6 MB image that
# takes a few seconds per fetch would otherwise stack those seconds up into a
# freeze. The newest few listed are fetched, two at a time; a fetched segment
# is held until FFmpeg takes it or it falls out of the cache (about 5 MB each).
PREFETCH = 4
PREFETCH_WORKERS = 2
MAX_CACHED = 8

_URI_ATTR = re.compile(r'URI="([^"]+)"')

# (url, headers, timeout[, max_bytes]) -> (status, body, final URL)
Fetch = Callable[..., Tuple[int, bytes, str]]


def browser_tls_fetch(url: str, headers: Dict[str, str], timeout: int,
                      max_bytes: int = MAX_PLAYLIST_BYTES) -> Tuple[int, bytes, str]:
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
                if len(body) >= max_bytes:
                    break
            return resp.status_code, body, str(resp.url)
        finally:
            resp.close()
    finally:
        session.close()


class PlaylistRelay:
    def __init__(self, url: str, headers: Dict[str, str], fetch: Optional[Fetch] = None,
                 unwrap_segments: bool = False):
        self._playlists: List[str] = [url]
        self._unwrap = unwrap_segments
        self._segments: Dict[int, str] = {}
        self._segment_ids: Dict[str, int] = {}
        self._next_segment = 0
        self._loads: Dict[int, "Future[Tuple[int, bytes]]"] = {}
        self._pool: Optional[ThreadPoolExecutor] = None
        self._stopped = False
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

    def _route_segment(self, absolute: str, listed: Optional[List[int]] = None) -> str:
        with self._lock:
            sid = self._segment_ids.get(absolute)
            if sid is None:
                sid = self._next_segment
                self._next_segment += 1
                self._segments[sid] = absolute
                self._segment_ids[absolute] = sid
                while len(self._segments) > MAX_SEGMENTS:
                    oldest = min(self._segments)
                    self._segment_ids.pop(self._segments.pop(oldest), None)
            if listed is not None:
                listed.append(sid)
            return f"http://127.0.0.1:{self._port}/s/{sid}.ts"

    def rewrite(self, text: str, base: str, listed: Optional[List[int]] = None) -> str:
        """Absolutise every URI; send the nested playlists through the relay.

        Decided by the tag, not the extension: a variant can be `/live/720p`
        or `.m3u`, and one that slipped past the relay would 403 in FFmpeg.
        Variants follow #EXT-X-STREAM-INF; renditions and I-frame playlists
        are the URI= of #EXT-X-MEDIA / #EXT-X-I-FRAME-STREAM-INF. Segments and
        #EXT-X-KEY / #EXT-X-MAP URIs go direct.
        """
        out, variant_next = [], False
        # A byte range is a slice of a file, not a whole PNG to unwrap.
        unwrap = self._unwrap and "#EXT-X-BYTERANGE" not in text
        for line in text.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                absolute = urljoin(base, stripped)
                if variant_next:
                    out.append(self._route(absolute))
                else:
                    out.append(self._route_segment(absolute, listed) if unwrap else absolute)
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
        # A JSON segment list (`probe.as_playlist`) is handed over as m3u8.
        playlist = as_playlist(body)
        if playlist is None:
            return 502, b""
        text = playlist.decode("utf-8", errors="replace")
        base = final_url or upstream
        # Segments and keys go to FFmpeg direct: a public playlist naming a LAN
        # address would have FFmpeg fetch it. Refused whole, as FFmpeg cannot
        # skip one segment cleanly; it retries and then fails over.
        if not self._allow_private and private_playlist_uri(text, base):
            return 403, b""
        listed: List[int] = []
        rewritten = self.rewrite(text, base, listed).encode("utf-8")
        for sid in listed[-PREFETCH:]:
            self._load_async(sid)
        return 200, rewritten

    def _load_async(self, sid: int) -> "Future[Tuple[int, bytes]]":
        """The (possibly still running) fetch of one listed segment."""
        with self._lock:
            load = self._loads.get(sid)
            if load is None and self._stopped:
                load = Future()
                load.set_result((503, b""))
            elif load is None:
                if self._pool is None:
                    self._pool = ThreadPoolExecutor(PREFETCH_WORKERS,
                                                    thread_name_prefix="pvarr-segment")
                load = self._pool.submit(self._load, sid)
                self._loads[sid] = load
                while len(self._loads) > MAX_CACHED:
                    self._loads.pop(min(self._loads)).cancel()
            return load

    def segment(self, sid: int) -> Tuple[int, bytes]:
        """One listed segment for FFmpeg: the MPEG-TS inside it.

        A segment that is not a wrapped one is passed through as served:
        FFmpeg then decides, and drops it like any bad segment, so the
        recording's segment-loss count still sees it.
        """
        try:
            return self._load_async(sid).result(timeout=SEGMENT_TIMEOUT * 2)
        except Exception:   # cancelled (evicted), timed out, or the pool is gone
            return 502, b""

    def _load(self, sid: int) -> Tuple[int, bytes]:
        with self._lock:
            upstream = self._segments.get(sid)
        if upstream is None:
            return 404, b""
        if not self._allow_private and is_private_url(upstream):
            return 403, b""
        try:
            status, body, _ = self._fetch(upstream, self._headers, SEGMENT_TIMEOUT,
                                          pngwrap.MAX_WRAPPED_BYTES)
        except Exception:
            return 502, b""
        if status not in (200, 206) or not body:
            return status if status >= 400 else 502, b""
        return 200, (pngwrap.unwrap(body) or body) if pngwrap.is_png(body) else body

    def start(self) -> str:
        """Bind an ephemeral loopback port and return the playlist's local URL."""
        relay = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                path = self.path.split("?", 1)[0]
                content_type = "application/vnd.apple.mpegurl"
                match = re.fullmatch(r"/(\d+)\.m3u8", path)
                seg = re.fullmatch(r"/s/(\d+)\.ts", path)
                if match:
                    status, body = relay.serve(int(match.group(1)))
                elif seg and relay._unwrap:
                    status, body = relay.segment(int(seg.group(1)))
                    content_type = "video/mp2t"
                else:
                    status, body = 404, b""
                self.send_response(status)
                self.send_header("Content-Type", content_type)
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
        with self._lock:
            self._stopped = True
            pool, self._pool = self._pool, None
            self._loads.clear()
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)
        server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()
