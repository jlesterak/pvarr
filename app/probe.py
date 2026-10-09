#!/usr/bin/env python3
"""
Stream Probe - PVArr

Turns a pasted URL into something FFmpeg can actually record. Given either an
m3u8 or the page that embeds one, it resolves the playlist and works out which
request headers the origin insists on, by trying the plausible combinations and
keeping the first that returns a real playlist.

This exists so the normal path is "paste the URL and press record". The
DevTools ritual (open Network, filter m3u8, copy Referer by hand) is still the
fallback for pages that only build their URL from JavaScript, but it should no
longer be the first thing anyone has to do.
"""

import base64
import binascii
import ipaddress
import logging
import re
import socket
import time
from typing import Any, Dict, List, Optional
from urllib.parse import parse_qs, urljoin, urlparse, urlsplit

import requests

logger = logging.getLogger("PVArrProbe")

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# A playlist is a few KB of text; a page is a few hundred. Anything past this
# is not what we are looking for, and reading it all would let a hostile or
# merely enormous URL balloon the server's memory.
MAX_BYTES = 512 * 1024
# Pages and embeds get more room: a player page measured 2026-10-07 padded
# itself with ~640 KB of inline script ahead of its stream URL. The embed
# walk reads at most MAX_EMBED_FETCHES documents, so the worst case per probe
# stays in the tens of MB, transiently.
PAGE_MAX_BYTES = 2 * 1024 * 1024
MAX_PAGE_CANDIDATES = 6
DEFAULT_TIMEOUT = 8

# Matches an m3u8 reference anywhere in HTML or inline JS: absolute, protocol
# relative, or root relative. Trailing punctuation is excluded so a URL sitting
# inside quotes or parentheses comes out clean.
#
# Found by searching for ".m3u8" and scanning out from each hit, not by one
# regex: `[^sep]+\.m3u8` is quadratic on a long separator-free run (13.75s on a
# 60 KB base64 data URI), and the embed walk reads a dozen documents.
_URL_SEPARATORS = frozenset(" \t\r\n\f\v\"'`\\<>()[]{},")
_M3U8_HIT_RE = re.compile(r"\.m3u8", re.I)
_M3U8_QUERY_RE = re.compile(r"""\?[^\s"'`\\<>()\[\]{},]{0,4096}""")
MAX_URL_LENGTH = 4096


def _find_m3u8(text: str) -> List[str]:
    found, last_end = [], 0
    for hit in _M3U8_HIT_RE.finditer(text):
        if hit.start() < last_end:
            continue
        start = hit.start()
        floor = max(last_end, start - MAX_URL_LENGTH)
        while start > floor and text[start - 1] not in _URL_SEPARATORS:
            start -= 1
        if start == hit.start():
            continue
        query = _M3U8_QUERY_RE.match(text, hit.end())
        end = query.end() if query else hit.end()
        found.append(text[start:end])
        last_end = end
    return found

# Embed-chain patterns, all generic. Chains seen in the field are three or four
# documents deep; the caps bound what a hostile page can make the probe fetch.
MAX_EMBED_FETCHES = 12
MAX_SCRIPTS_PER_DOCUMENT = 4
_IFRAME_RE = re.compile(r"""<iframe\b[^>]*?\bsrc\s*=\s*["']([^"'<>\s]+)["']""", re.I)
_SCRIPT_SRC_RE = re.compile(r"""<script\b[^>]*?\bsrc\s*=\s*["']([^"'<>\s]+)["']""", re.I)
# window.fid = "x"; var fid = 'x'; let/const likewise.
_JS_VAR_RE = re.compile(
    r"""(?:\bwindow\.|\bvar\s+|\blet\s+|\bconst\s+)([A-Za-z_$][\w$]*)\s*=\s*["']([^"'\\]*)["']""")
# '...' + name + '...' with the same quote on both sides.
_CONCAT_RE = re.compile(r"""(["'])\s*\+\s*(?:window\.)?([A-Za-z_$][\w$]*)\s*\+\s*\1""")
_QUOTED = r"""(?:"[^"\\]*"|'[^'\\]*')"""
_QUOTED_RE = re.compile(r""""([^"\\]*)"|'([^'\\]*)'""")
# A quoted base64 string that decodes to an http(s) URL: "aHR0c" is base64 for
# "http". Anchoring on it keeps the search linear on pages carrying large
# base64 blobs, which never start that way.
_B64_URL_RE = re.compile(r"""(["'])(aHR0c[A-Za-z0-9+/]{10,4000}={0,2})\1""")
_JOINED_ARRAY_RE = re.compile(
    r"\[\s*((?:" + _QUOTED + r"\s*,\s*)*" + _QUOTED + r")\s*,?\s*\]\s*\.join\(\s*(?:\"\"|'')\s*\)")

# requests raises requests.RequestException; curl_cffi raises its own, which
# is an OSError (as is requests'). Both mean "this fetch failed".
_FETCH_ERRORS = (requests.RequestException, OSError)

# Query parameters some CDNs use to carry the referer they expect back.
_REFERER_PARAMS = ("referer", "referrer", "origin", "ref")


class ProbeError(ValueError):
    """The input could not be used as a stream URL at all."""


def clean_url(raw: str) -> str:
    """Normalise a pasted URL, rejecting anything that is not http(s).

    Paste sources add noise: wrapping quotes, a `curl '<url>'` prefix, stray
    whitespace from a wrapped terminal line.
    """
    url = (raw or "").strip()
    url = url.strip("\"'`")
    url = re.sub(r"\s+", "", url)
    if url.lower().startswith("//"):
        url = "https:" + url
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ProbeError("URL must start with http:// or https://")
    return url


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _looks_like_playlist_url(url: str) -> bool:
    return ".m3u8" in urlparse(url).path.lower()


def _is_playlist_body(body: bytes) -> bool:
    return body.lstrip()[:7].upper().startswith(b"#EXTM3U")


def _dedupe(items: List[str]) -> List[str]:
    seen, out = set(), []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _referer_candidates(m3u8_url: str, page_url: Optional[str], hint: Optional[str]) -> List[str]:
    """Referers worth trying, best guess first.

    The embed page is the usual answer; failing that, the site root of the page
    or of the playlist host. Some CDNs also echo the referer they want back in
    the query string, which is a free correct answer when present.
    """
    candidates: List[str] = []
    if hint:
        candidates.append(hint)
    if page_url:
        candidates.append(page_url)
        candidates.append(_origin(page_url) + "/")
    for key, values in parse_qs(urlparse(m3u8_url).query).items():
        if key.lower() in _REFERER_PARAMS:
            for value in values:
                if value.startswith(("http://", "https://")):
                    candidates.append(value)
    candidates.append(_origin(m3u8_url) + "/")
    return _dedupe(candidates)


def _header_attempts(
    m3u8_url: str,
    page_url: Optional[str],
    referer: Optional[str],
    user_agent: str,
    cookie: Optional[str],
) -> List[Dict[str, str]]:
    """Header sets to try in order, cheapest and least invented first.

    An explicit referer, when the caller gave one, outranks everything. After
    that a bare request comes first: plenty of streams need no referer at all,
    and sending one they do not expect is occasionally worse than sending none.
    """
    attempts: List[Dict[str, str]] = []
    referers = _referer_candidates(m3u8_url, page_url, referer)

    if referer:
        attempts.append({"Referer": referer, "Origin": _origin(referer)})
    attempts.append({})
    for candidate in referers:
        if referer and candidate == referer:
            continue
        attempts.append({"Referer": candidate, "Origin": _origin(candidate)})

    base = {"User-Agent": user_agent, "Accept": "*/*"}
    if cookie:
        base["Cookie"] = cookie
    return [dict(base, **extra) for extra in attempts]


def _fetch(
    session: requests.Session,
    url: str,
    headers: Dict[str, str],
    timeout: int,
    max_bytes: int = MAX_BYTES,
):
    """GET with a hard ceiling on how much of the body is read, and for how long.

    `timeout` is per socket operation, so on its own a server that trickles a
    byte every few seconds could hold the caller for minutes. The body read
    also stops at a wall-clock deadline; what arrived by then is returned and
    judged like any other body (a truncated playlist simply is not one).
    """
    deadline = time.monotonic() + timeout * 2
    resp = session.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    try:
        body = b""
        for chunk in resp.iter_content(8192):
            body += chunk
            if len(body) >= max_bytes or time.monotonic() > deadline:
                break
        return resp, body
    finally:
        resp.close()


def _extract_playlists(text: str, page_url: str) -> List[str]:
    """Pull m3u8 references out of normalised page text, absolutised against the page."""
    found: List[str] = []
    for match in _find_m3u8(text):
        candidate = match.lstrip("=(,:")
        if candidate.startswith("//"):
            candidate = urlparse(page_url).scheme + ":" + candidate
        elif not candidate.startswith(("http://", "https://")):
            candidate = urljoin(page_url, candidate)
        found.append(candidate)

    # A master playlist is the better recording target than a single variant,
    # and is conventionally named. Otherwise keep page order.
    def rank(url: str) -> int:
        name = urlparse(url).path.lower()
        if "master" in name or "index" in name or "playlist" in name:
            return 0
        return 1

    return sorted(_dedupe(found), key=rank)[:MAX_PAGE_CANDIDATES]


def _normalise(raw: str, variables: Dict[str, str]) -> str:
    """Undo the generic ways a page hides a URL from a plain text search.

    None of these is site-specific and none needs JavaScript to be run:
    - escaped slashes and HTML entities (inline JS and JSON write
      https:\\/\\/host\\/x.m3u8, and a regex would capture only the tail),
      and JSON's \\u0026 / \\u002F, which would cut a token's query short;
    - a URL built as an array of pieces, ``["h","t","t","p",...].join("")``;
    - a URL concatenated with a variable the page set earlier,
      ``'...live=' + fid + '...'`` with ``window.fid = "abc"``;
    - a URL stored base64-encoded and passed to ``atob()`` by the player.
    """
    text = raw.replace("\\/", "/").replace("&amp;", "&").replace("&#47;", "/")
    text = text.replace("\\u0026", "&").replace("\\u002F", "/").replace("\\u002f", "/")
    if ".join(" in text:
        text = _JOINED_ARRAY_RE.sub(
            lambda m: '"' + "".join(a or b for a, b in _QUOTED_RE.findall(m.group(1))) + '"',
            text,
        )
    if variables:
        text = _CONCAT_RE.sub(lambda m: variables.get(m.group(2), m.group(0)), text)
    if "aHR0c" in text:
        text = _B64_URL_RE.sub(_decode_b64_url, text)
    return text


def _decode_b64_url(match: "re.Match") -> str:
    try:
        url = base64.b64decode(match.group(2), validate=True).decode("ascii")
    except (binascii.Error, ValueError):
        return match.group(0)
    if not url.isprintable() or any(c.isspace() for c in url):
        return match.group(0)
    return match.group(1) + url + match.group(1)


def _embedded(text: str, pattern: "re.Pattern", base_url: str) -> List[str]:
    """Absolute http(s) URLs of the iframes or scripts a document references."""
    urls = []
    for src in pattern.findall(text):
        url = urljoin(base_url, src.strip())
        if url.startswith(("http://", "https://")):
            urls.append(url)
    return _dedupe(urls)


def is_private_url(url: str) -> bool:
    """True when the URL's host is loopback, private, link-local or otherwise
    not a public internet address.

    A page PVArr scrapes, or a playlist it relays, can name any URL it likes,
    and following it blindly would let a stranger's page make PVArr fetch
    from inside the operator's LAN -- with the stream's cookie attached, in
    the relay's case. Hostnames are resolved; one that does not resolve is
    not treated as private (the fetch will simply fail).
    """
    host = urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return False
        addresses = [ipaddress.ip_address(info[4][0].split("%")[0]) for info in infos]
    return any(not a.is_global or a.is_multicast for a in addresses)


# Browsers stop at 20; a real CDN hand-off is one or two hops.
MAX_REDIRECTS = 10
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_PLAYLIST_URI_ATTR = re.compile(r'URI="([^"]+)"')


class PrivateAddressError(requests.RequestException):
    """A fetch was refused because it would reach a private or loopback address."""


class NoPrivateRedirects:
    """A requests or curl_cffi session whose redirects are followed by hand.

    Checking a URL before fetching it is worthless if the HTTP client then
    follows a 302 wherever it points: a public page could bounce PVArr onto
    the LAN, the container's own API, or a cloud metadata address. So every
    hop is checked with `is_private_url` before it is requested. The rule is
    per request: a redirect may only land on a private address if the request
    *started* on one -- which keeps an operator's own LAN source (and its
    in-LAN redirects) working, and is the only case where that is wanted.

    Also on each hop: only http(s) is followed (curl would otherwise open a
    `file://` Location), and an explicit Cookie header is dropped when the
    redirect changes host, so the stream's cookie never goes to a stranger.
    Everything else (`cookies`, `close`, ...) passes through to the session.
    """

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def get(self, url, headers=None, allow_redirects=True, **kwargs):
        headers = dict(headers or {})
        allow_private = is_private_url(url)
        first_host = urlsplit(url).hostname
        for _ in range(MAX_REDIRECTS + 1):
            resp = self._session.get(url, headers=headers, allow_redirects=False, **kwargs)
            location = resp.headers.get("Location") if allow_redirects else None
            if resp.status_code not in _REDIRECT_STATUSES or not location:
                return resp
            resp.close()
            url = urljoin(str(resp.url), location)
            if urlsplit(url).scheme not in ("http", "https"):
                raise PrivateAddressError(f"Refused a redirect to a non-HTTP URL: {url}")
            if not allow_private and is_private_url(url):
                raise PrivateAddressError(
                    f"Refused a redirect to a private or loopback address: {url}")
            if urlsplit(url).hostname != first_host:
                headers.pop("Cookie", None)
        raise requests.TooManyRedirects(f"More than {MAX_REDIRECTS} redirects")


def private_playlist_uri(text: str, base: str) -> Optional[str]:
    """The first URI in a playlist that is on a private address, else None.

    Covers everything the playlist makes a client fetch: segments, variants,
    renditions, keys and init maps. Each host is resolved once, so a playlist
    of a thousand segments on one CDN costs one lookup.
    """
    verdicts: Dict[str, bool] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        for raw in (_PLAYLIST_URI_ATTR.findall(line) if line.startswith("#") else [line]):
            url = urljoin(base, raw)
            host = urlsplit(url).hostname
            if not host:  # data: URIs and the like fetch nothing
                continue
            if host not in verdicts:
                verdicts[host] = is_private_url(url)
            if verdicts[host]:
                return url
    return None


def _embed_referer(parent_url: str, child_url: str) -> str:
    """The Referer a browser sends when a document loads an iframe or script.

    The default policy (strict-origin-when-cross-origin) sends the full URL to
    the same origin and only the origin to anyone else.
    """
    if _origin(parent_url) == _origin(child_url):
        return parent_url
    return _origin(parent_url) + "/"


def _follow_embeds(
    session,
    page_url: str,
    body: bytes,
    headers: Dict[str, str],
    timeout: int,
    attempts: List[Dict[str, Any]],
    allow_private: bool = False,
):
    """Find the playlist in a page or in the documents it embeds.

    Players usually live in an iframe, often several deep, and sometimes in an
    iframe that an external script ``document.write``s -- so it appears in no
    HTML at all. This walks that chain breadth-first, without running any
    JavaScript.

    Returns ``(document_url, playlists)``. The document is the one whose content
    held the playlist URL, and its origin is the Referer the CDN wants: measured
    on 2026-09-01, the innermost document's origin got a 200 while the
    intermediate iframe and the pasted page both got 403. Discovered per stream,
    so it survives the front-end domains rotating. ``(page_url, [])`` when
    nothing was found.

    URLs on private or loopback addresses are skipped unless the pasted page
    was itself on one (`allow_private`): see `is_private_url`.
    """
    queue = [(page_url, body)]
    seen = {page_url}
    budget = MAX_EMBED_FETCHES

    def public(urls: List[str]) -> List[str]:
        if allow_private:
            return urls
        kept = [u for u in urls if not is_private_url(u)]
        for url in urls:
            if url not in kept:
                attempts.append({"stage": "embed", "url": url,
                                 "note": "skipped: private or loopback address"})
        return kept

    def fetch(url: str, parent: str, stage: str):
        nonlocal budget
        if budget <= 0:
            return None
        budget -= 1
        embed_headers = dict(headers, Referer=_embed_referer(parent, url))
        try:
            resp, data = _fetch(session, url, embed_headers, timeout,
                                max_bytes=PAGE_MAX_BYTES)
        except _FETCH_ERRORS as exc:
            attempts.append({"stage": stage, "url": url, "error": str(exc)})
            return None
        attempts.append({"stage": stage, "url": url, "status": resp.status_code})
        return (resp.url, data) if resp.ok else None

    while queue:
        doc_url, doc_body = queue.pop(0)
        raw = doc_body.decode("utf-8", errors="replace")
        variables = dict(_JS_VAR_RE.findall(raw))
        text = _normalise(raw, variables)
        playlists = public(_extract_playlists(text, doc_url))
        if playlists:
            return doc_url, playlists

        frames = public(_embedded(text, _IFRAME_RE, doc_url))
        if not frames:
            # No iframe in the markup: look for one written by a script. The
            # script runs in this document, so its URLs resolve against this
            # document and a playlist found in it is still this document's.
            for script_url in public(_embedded(text, _SCRIPT_SRC_RE, doc_url))[:MAX_SCRIPTS_PER_DOCUMENT]:
                got = fetch(script_url, doc_url, "script")
                if not got:
                    continue
                script_raw = got[1].decode("utf-8", errors="replace")
                script_vars = dict(variables, **dict(_JS_VAR_RE.findall(script_raw)))
                script_text = _normalise(script_raw, script_vars)
                playlists = public(_extract_playlists(script_text, doc_url))
                if playlists:
                    return doc_url, playlists
                frames += public(_embedded(script_text, _IFRAME_RE, doc_url))

        for frame in _dedupe(frames):
            if frame in seen:
                continue
            seen.add(frame)
            got = fetch(frame, doc_url, "iframe")
            if got:
                queue.append(got)
    return page_url, []


def _parse_playlist(body: bytes, playlist_url: str) -> Dict[str, Any]:
    """Classify a playlist and pull out variants or the first segment."""
    text = body.decode("utf-8", errors="replace")
    lines = [line.strip() for line in text.splitlines()]

    variants: List[Dict[str, Any]] = []
    first_segment: Optional[str] = None
    pending: Optional[Dict[str, Any]] = None

    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
            resolution = re.search(r"RESOLUTION=([0-9x]+)", attrs, re.I)
            bandwidth = re.search(r"[^-]BANDWIDTH=(\d+)", "," + attrs, re.I)
            pending = {
                "resolution": resolution.group(1) if resolution else None,
                "bandwidth": int(bandwidth.group(1)) if bandwidth else None,
            }
        elif line and not line.startswith("#"):
            absolute = urljoin(playlist_url, line)
            if pending is not None:
                pending["url"] = absolute
                variants.append(pending)
                pending = None
            elif first_segment is None:
                first_segment = absolute

    return {
        "kind": "master" if variants else "media",
        "variants": variants,
        "first_segment": first_segment,
    }


def probe_stream(
    url: str,
    referer: Optional[str] = None,
    user_agent: Optional[str] = None,
    cookie: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check_segment: bool = True,
    use_ytdlp: bool = True,
) -> Dict[str, Any]:
    """Resolve a pasted URL to a playlist plus the headers needed to fetch it.

    Never raises for a stream that simply refuses us: a failed probe comes back
    as ``ok: False`` with the status codes it saw, because the caller (the
    dashboard, or the recorder mid-failover) wants to report that, not crash.
    """
    result: Dict[str, Any] = {
        "ok": False,
        "input_url": url,
        "m3u8_url": "",
        "page_url": "",
        "referer": "",
        "user_agent": user_agent or DEFAULT_USER_AGENT,
        "cookie": "",
        "kind": "",
        "variants": [],
        "headers_required": [],
        "segment_ok": None,
        "impersonate": False,
        "attempts": [],
        "message": "",
    }

    try:
        target = clean_url(url)
    except ProbeError as exc:
        result["message"] = str(exc)
        return result

    result["input_url"] = target
    ua = user_agent or DEFAULT_USER_AGENT
    session = NoPrivateRedirects(requests.Session())

    page_url: Optional[str] = None
    playlists: List[str] = [target]

    # A URL with no .m3u8 in its path is a page until proven otherwise. Fetch
    # it, and if it turns out to serve a playlist directly, carry on with it.
    if not _looks_like_playlist_url(target):
        page_headers = {"User-Agent": ua, "Accept": "text/html,*/*"}
        if referer:
            page_headers["Referer"] = referer
        if cookie:
            page_headers["Cookie"] = cookie
        try:
            resp, body = _fetch(session, target, page_headers, timeout,
                                max_bytes=PAGE_MAX_BYTES)
        except _FETCH_ERRORS as exc:
            result["attempts"].append(
                {"stage": "page", "url": target, "error": str(exc)})
            result["message"] = f"Could not open {target}: {exc}"
            return result

        result["attempts"].append({
            "stage": "page",
            "url": target,
            "status": resp.status_code,
            "note": "served a playlist directly" if _is_playlist_body(body) else "scraped for an m3u8",
        })

        if _is_playlist_body(body):
            playlists = [resp.url]
        else:
            page_url, playlists = _follow_embeds(
                session, resp.url, body, page_headers, timeout, result["attempts"],
                allow_private=is_private_url(target))
            if not playlists:
                result["page_url"] = resp.url
                # Nothing in the HTML. That is the XHR case: the player fetches
                # its manifest at runtime, so it was never in the document to
                # find. yt-dlp is the one route left that can resolve it, and
                # it must be tried here as well as in the recorder -- an
                # operator testing from the dashboard has to see the same
                # answer the recording will get.
                if use_ytdlp:
                    resolved = _try_ytdlp(target, result)
                    if resolved:
                        return resolved
                result["message"] = (
                    f"No .m3u8 found on that page (HTTP {resp.status_code})"
                    + (", and yt-dlp could not resolve it either" if use_ytdlp else "")
                    + ". The player probably builds "
                    "its URL in JavaScript. Take the m3u8 from DevTools (Network "
                    "tab, filter m3u8) and paste that -- note its token is usually "
                    "good for a few hours, so start the recording promptly."
                )
                return result

    last_status = None
    # Second pass, only when every plain attempt was refused: some origins
    # answer only a browser-compatible TLS client (measured 2026-09-01: one
    # browser profile 200, every other client 403 with identical headers).
    # FFmpeg's OpenSSL cannot present one, so a playlist that needs it is
    # recorded through app.relay; the result says so with `impersonate` (the
    # name of curl_cffi's option for this).
    for impersonate in (False, True):
        fetch_session = session
        if impersonate:
            if last_status not in (401, 403):
                break
            fetch_session = _browser_tls_session()
            if fetch_session is None:
                break
        for playlist_url in playlists:
            for headers in _header_attempts(playlist_url, page_url, referer, ua, cookie):
                try:
                    found = _try_playlist(
                        fetch_session, session, playlist_url, headers, impersonate,
                        page_url, cookie, timeout, check_segment, result)
                except PrivateAddressError as exc:
                    # Not a header problem, so trying more headers is pointless.
                    result["message"] = str(exc)
                    return result
                if found is True:
                    return result
                if found is not None:
                    last_status = found

    failed_url = playlists[0] if playlists else target
    origin_status = _check_origin(session, failed_url, ua, timeout, result["attempts"])
    result["message"] = _failure_message(last_status, failed_url, origin_status)
    return result


def _browser_tls_session():
    """A curl_cffi session with a browser-compatible TLS profile, or None."""
    try:
        from curl_cffi import requests as curl_requests
    except ImportError:
        return None
    return NoPrivateRedirects(curl_requests.Session(impersonate="chrome"))


def _cookie_header(session) -> str:
    # requests' jar yields Cookie objects; curl_cffi keeps them on `.jar`.
    jar = getattr(session.cookies, "jar", session.cookies)
    return "; ".join(f"{c.name}={c.value}" for c in jar)


def _try_playlist(fetch_session, plain_session, playlist_url, headers, impersonate,
                  page_url, cookie, timeout, check_segment, result):
    """One playlist attempt. True on success (result filled in), else the
    status seen, or None when the request itself failed."""
    note_tls = "with a browser TLS profile" if impersonate else ""
    allow_private = is_private_url(result["input_url"])
    try:
        resp, body = _fetch(fetch_session, playlist_url, headers, timeout)
    except _FETCH_ERRORS as exc:
        result["attempts"].append({
            "stage": "playlist",
            "url": playlist_url,
            "referer": headers.get("Referer", ""),
            "error": str(exc),
            "note": note_tls,
        })
        if isinstance(exc, PrivateAddressError):
            raise
        return None

    result["attempts"].append({
        "stage": "playlist",
        "url": playlist_url,
        "referer": headers.get("Referer", ""),
        "status": resp.status_code,
        # A 2xx that is not a playlist is its own failure mode -- an HTML
        # error page or an anti-bot interstitial answering 200 -- and reads as
        # an inexplicable success without this. Scoped to a successful status:
        # on a 403 the body is obviously not a playlist, and saying so is noise
        # that reads like a second, unrelated problem.
        "note": ("2xx but not a playlist (HTML or interstitial?)"
                 if resp.ok and not _is_playlist_body(body) else note_tls),
    })

    if not (resp.ok and _is_playlist_body(body)):
        return resp.status_code

    # A public playlist naming a LAN segment, key or variant would have FFmpeg
    # (or the relay, cookie attached) fetch from inside the network.
    private = None if allow_private else private_playlist_uri(
        body.decode("utf-8", errors="replace"), resp.url)
    if private:
        raise PrivateAddressError(
            f"Refused: the playlist points at a private or loopback address ({private}).")

    parsed = _parse_playlist(body, resp.url)
    result.update(
        {
            "ok": True,
            "m3u8_url": resp.url,
            "page_url": page_url or "",
            "referer": headers.get("Referer", ""),
            "user_agent": headers.get("User-Agent", result["user_agent"]),
            "cookie": cookie or "",
            "kind": parsed["kind"],
            "variants": parsed["variants"],
            "headers_required": [k for k in ("Referer", "Cookie") if headers.get(k)],
            "impersonate": impersonate,
        }
    )

    # Cookies picked up on the way (page redirect, playlist itself) are part of
    # the answer: a stream that gates its segments on a session will not replay
    # without them.
    jar = "; ".join(filter(None, {_cookie_header(plain_session), _cookie_header(fetch_session)}))
    if jar and not cookie:
        result["cookie"] = jar
        result["headers_required"].append("Cookie")

    if check_segment:
        # Segments are checked with the plain client even when the playlist
        # needed the browser TLS profile: FFmpeg fetches them itself, so this is
        # the honest test of whether the recording will work.
        result["segment_ok"] = _check_segment(
            plain_session, parsed, headers, result["cookie"], timeout,
            result["attempts"], playlist_session=fetch_session,
            allow_private=allow_private,
        )

    result["message"] = _describe(result)
    return True


def segment_extension(url: str) -> str:
    """The extension the segment is *served as*, ignoring the query string.

    Worth surfacing on its own. FFmpeg's HLS demuxer refuses segments by
    extension, so an origin serving MPEG-TS as ".image" or ".png" fails for
    a reason that has nothing to do with headers -- and reads as an
    inexplicable failure unless the trace says what the extension was.
    """
    path = urlsplit(url or "").path
    _, _, ext = path.rpartition(".")
    return f".{ext.lower()}" if ext and ext != path else ""


def _check_segment(
    session: requests.Session,
    parsed: Dict[str, Any],
    headers: Dict[str, str],
    cookie: str,
    timeout: int,
    attempts: Optional[List[Dict[str, Any]]] = None,
    playlist_session=None,
    allow_private: bool = True,
) -> Optional[bool]:
    """Confirm the media the playlist points at is fetchable with the same headers.

    A playlist that loads is not proof of a recordable stream: origins
    routinely serve the manifest to anyone and gate the segments. Checking one
    segment here turns a recording that fails minutes later into a red field in
    the browser now.

    Every step is recorded into `attempts`. "Segments rejected" without a
    status code tells an operator that something is wrong but nothing about
    what, which is the difference between a diagnosis and a shrug.
    """
    def record(entry: Dict[str, Any]) -> None:
        if attempts is not None:
            attempts.append(dict(entry, stage="segment"))

    target = parsed.get("first_segment")
    if not target and parsed.get("variants"):
        # Master playlist: descend one level to reach real segments.
        try:
            variant_url = parsed["variants"][0]["url"]
            resp, body = _fetch(playlist_session or session, variant_url, headers, timeout)
            record({
                "url": variant_url,
                "status": resp.status_code,
                "note": "variant playlist",
            })
            if not (resp.ok and _is_playlist_body(body)):
                return False
            private = None if allow_private else private_playlist_uri(
                body.decode("utf-8", errors="replace"), resp.url)
            if private:
                record({"url": private,
                        "note": "refused: private or loopback address in the variant"})
                return False
            target = _parse_playlist(body, resp.url).get("first_segment")
        except _FETCH_ERRORS + (KeyError, IndexError) as exc:
            record({"url": parsed.get("variants", [{}])[0].get("url", ""),
                    "error": str(exc), "note": "variant playlist"})
            return None
    if not target:
        record({"url": "", "note": "playlist listed no segments"})
        return None

    probe_headers = dict(headers, Range="bytes=0-2047")
    if cookie:
        probe_headers["Cookie"] = cookie
    ext = segment_extension(target)
    try:
        resp, body = _fetch(session, target, probe_headers, timeout, max_bytes=4096)
        ok = bool(resp.ok and body)
        note = f"served as {ext}" if ext else ""
        if ok and ext and ext not in (".ts", ".m4s", ".mp4", ".aac", ".m3u8"):
            # Not a failure here -- it fetched fine. But it is exactly what
            # makes FFmpeg refuse the stream later, so name it now.
            note = (f"served as {ext} -- not a video extension, FFmpeg refuses "
                    "these by extension")
        record({"url": target, "status": resp.status_code, "note": note})
        return ok
    except _FETCH_ERRORS as exc:
        record({"url": target, "error": str(exc),
                "note": f"served as {ext}" if ext else ""})
        return None


def _describe(result: Dict[str, Any]) -> str:
    parts = ["Master playlist" if result["kind"] == "master" else "Media playlist"]
    if result["variants"]:
        parts.append(f"{len(result['variants'])} variants")
    if result["headers_required"]:
        parts.append("needs " + " + ".join(result["headers_required"]))
    else:
        parts.append("no special headers")
    if result.get("impersonate"):
        parts.append("playlist only served to a browser TLS profile (relayed)")
    if result["segment_ok"] is False:
        parts.append("segments rejected — stream may be session gated")
    return ", ".join(parts) + "."


def _try_ytdlp(target: str, result: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Last resort for a page with no m3u8 in its HTML.

    Imported lazily: probe.py is imported by the recorder on every connect, and
    there is no reason to pull yt-dlp's module graph in for the common case
    where the page scrape already worked.
    """
    from app import ytdlp

    if not ytdlp.ytdlp_path():
        return None
    found = ytdlp.resolve(target)
    result["attempts"].append({
        "stage": "yt-dlp",
        "url": target,
        "status": 200 if found else None,
        "note": (f"resolved by the {found['extractor']} extractor" if found
                 else "no extractor could resolve this page"),
    })
    if not found:
        return None

    result.update({
        "ok": True,
        "m3u8_url": found["m3u8_url"],
        "page_url": target,
        "referer": found.get("referer", ""),
        "user_agent": found.get("user_agent") or result["user_agent"],
        "cookie": found.get("cookie", ""),
        "kind": "media",
        "headers_required": [
            name for name, key in (("Referer", "referer"), ("Cookie", "cookie"))
            if found.get(key)
        ],
        "message": f"Resolved by yt-dlp ({found.get('extractor') or 'generic'}).",
    })
    return result


def _check_origin(
    session: requests.Session,
    url: str,
    user_agent: str,
    timeout: int,
    attempts: List[Dict[str, Any]],
) -> Optional[int]:
    """Ask the origin for its own front page, after everything else failed.

    This is what separates "the stream needs a header we could not guess" from
    "this host is not talking to us at all". A host that refuses its own root
    with the same status it gave the playlist is not gating on a Referer -- it
    is refusing the client, and no header will change that.

    Worth the extra request because the alternative is what PVArr used to do:
    answer every 403 by sending the operator to DevTools to hunt for headers,
    including on a link that was simply dead. Runs only on total failure, so a
    probe that succeeds costs nothing extra.
    """
    origin = _origin(url)
    if not origin or "://" not in origin:
        return None
    root = origin + "/"
    try:
        resp, _ = _fetch(session, root, {"User-Agent": user_agent}, timeout, max_bytes=2048)
    except _FETCH_ERRORS as exc:
        attempts.append({
            "stage": "origin", "url": root, "error": str(exc),
            "note": "the origin's own front page",
        })
        return None
    attempts.append({
        "stage": "origin", "url": root, "status": resp.status_code,
        "note": "the origin's own front page",
    })
    return resp.status_code


# Query keys and path shapes that mean "this URL carries an access token".
# Not an exhaustive list and does not need to be: it only decides whether to
# offer one extra sentence of advice.
_TOKEN_QUERY_KEYS = (
    "token", "tok", "key", "sig", "signature", "hash", "md5", "expires",
    "expire", "exp", "auth", "hdnts", "wmsauthsign", "st", "e",
)
# A token in a path is long *and* looks random. Length alone is not enough:
# "2024-nfl-week-1-highlights" is 26 characters of perfectly ordinary slug.
# Randomness shows up as either long hex, or a mix of upper and lower case --
# neither of which happens in a human-written path segment.
_HEX_SEGMENT = re.compile(r"^[0-9a-f]{16,}$", re.I)
_MIXED_CASE_SEGMENT = re.compile(r"^(?=.*[a-z])(?=.*[A-Z])[A-Za-z0-9_\-]{16,}$")


def looks_tokenised(url: str) -> bool:
    """True when the URL carries what looks like a per-session access token.

    Two shapes, both common: a token in the query string, and a token baked
    into the path as a long opaque segment (nginx secure_link does this --
    /secure/<32 chars>/...). Used only to decide whether to suggest pasting the
    page URL instead, so a false positive costs one unnecessary sentence.
    """
    parts = urlsplit(url or "")
    keys = {k.lower() for k in parse_qs(parts.query)}
    if keys & set(_TOKEN_QUERY_KEYS):
        return True
    return any(
        _HEX_SEGMENT.match(seg) or _MIXED_CASE_SEGMENT.match(seg)
        for seg in parts.path.split("/")
    )


def _failure_message(
    status: Optional[int],
    url: str,
    origin_status: Optional[int] = None,
) -> str:
    # The origin refusing its own root the same way it refused the playlist is
    # decisive: the host is turning this client away before it ever looks at
    # the path, so headers are not the problem and DevTools will not help.
    if (status is not None and origin_status is not None
            and origin_status == status and status in (401, 403, 429)):
        return (
            f"This host refused every request, including its own front page "
            f"({status}) — so this is not a missing header, and copying one "
            "from DevTools will not help. Either the link has expired (these "
            "tokens are usually short-lived) or the host is blocking us. Open "
            "the URL in a browser: if it fails there too, get a fresh link."
        )
    # A tokenised URL that is refused, from a host that is otherwise talking to
    # us, is far more often an expired token than a missing header. These
    # tokens are minted for one browser session and commonly die in minutes, so
    # a link copied out of DevTools is often already dead when it is pasted --
    # and no header will revive it. Pasting the *page* instead lets PVArr mint
    # its own, and re-mint it on every failover.
    if status in (401, 403, 404) and looks_tokenised(url):
        expired = "has probably expired" if status == 404 else "was rejected"
        return (
            f"The access token in this URL {expired} ({status}), though the host "
            "itself is answering — so this is the stream being gated, not a "
            "missing header. Paste the **page URL** you watch the stream on "
            "instead: PVArr re-resolves it on every connect and failover, so it "
            "gets a fresh token each time. If the page URL also fails, the "
            "stream is session-gated — copy the **Cookie** header from the same "
            "DevTools request into the advanced fields."
        )
    if status == 403:
        return (
            "Every header combination was rejected (403), but the host does answer "
            "other requests — so it is gating this stream specifically. It likely "
            "needs a cookie or a referer PVArr cannot guess — copy them from DevTools."
        )
    if status == 404:
        return ("Not found (404): the page or playlist is gone, or not up yet. "
                "If the URL carries a token, it has probably expired.")
    if status is None:
        return f"Could not reach {url}."
    return f"No playlist returned (last status {status})."
