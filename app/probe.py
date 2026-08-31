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

import logging
import re
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
MAX_PAGE_CANDIDATES = 6
DEFAULT_TIMEOUT = 8

# Matches an m3u8 reference anywhere in HTML or inline JS: absolute, protocol
# relative, or root relative. Trailing punctuation is excluded so a URL sitting
# inside quotes or parentheses comes out clean.
_M3U8_RE = re.compile(r"""[^\s"'`\\<>()\[\]{},]+\.m3u8(?:\?[^\s"'`\\<>()\[\]{},]*)?""", re.I)

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
    """GET with a hard ceiling on how much of the body is read."""
    resp = session.get(url, headers=headers, timeout=timeout, stream=True, allow_redirects=True)
    try:
        body = b""
        for chunk in resp.iter_content(8192):
            body += chunk
            if len(body) >= max_bytes:
                break
        return resp, body
    finally:
        resp.close()


def _extract_playlists(page_body: bytes, page_url: str) -> List[str]:
    """Pull m3u8 references out of a page body, absolutised against the page."""
    try:
        text = page_body.decode("utf-8", errors="replace")
    except Exception:
        return []

    # Unescape before matching: inline JS and JSON write the URL as
    # https:\/\/host\/path.m3u8, and a regex run over that would capture only
    # the fragment after the last backslash.
    text = text.replace("\\/", "/").replace("&amp;", "&").replace("&#47;", "/")

    found: List[str] = []
    for match in _M3U8_RE.findall(text):
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
    session = requests.Session()

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
            resp, body = _fetch(session, target, page_headers, timeout)
        except requests.RequestException as exc:
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
            page_url = resp.url
            playlists = _extract_playlists(body, resp.url)
            if not playlists:
                result["page_url"] = resp.url
                result["message"] = (
                    f"No .m3u8 found on that page (HTTP {resp.status_code}). If the player "
                    "builds its URL in JavaScript, grab the m3u8 from DevTools and paste that."
                )
                return result

    last_status = None
    for playlist_url in playlists:
        for headers in _header_attempts(playlist_url, page_url, referer, ua, cookie):
            try:
                resp, body = _fetch(session, playlist_url, headers, timeout)
            except requests.RequestException as exc:
                result["attempts"].append({
                    "stage": "playlist",
                    "url": playlist_url,
                    "referer": headers.get("Referer", ""),
                    "error": str(exc),
                })
                continue

            last_status = resp.status_code
            result["attempts"].append({
                "stage": "playlist",
                "url": playlist_url,
                "referer": headers.get("Referer", ""),
                "status": resp.status_code,
                # A 2xx that is not a playlist is its own failure mode -- an
                # HTML error page or an anti-bot interstitial answering 200 --
                # and reads as an inexplicable success without this. Scoped to
                # a successful status: on a 403 the body is obviously not a
                # playlist, and saying so is noise that reads like a second,
                # unrelated problem.
                "note": ("" if (not resp.ok or _is_playlist_body(body))
                         else "2xx but not a playlist (HTML or interstitial?)"),
            })

            if not (resp.ok and _is_playlist_body(body)):
                continue

            parsed = _parse_playlist(body, resp.url)
            result.update(
                {
                    "ok": True,
                    "m3u8_url": resp.url,
                    "page_url": page_url or "",
                    "referer": headers.get("Referer", ""),
                    "user_agent": ua,
                    "cookie": cookie or "",
                    "kind": parsed["kind"],
                    "variants": parsed["variants"],
                    "headers_required": [k for k in ("Referer", "Cookie") if headers.get(k)],
                }
            )

            # Cookies picked up on the way (page redirect, playlist itself) are
            # part of the answer: a stream that gates its segments on a session
            # will not replay without them.
            jar = "; ".join(f"{c.name}={c.value}" for c in session.cookies)
            if jar and not cookie:
                result["cookie"] = jar
                result["headers_required"].append("Cookie")

            if check_segment:
                result["segment_ok"] = _check_segment(
                    session, parsed, headers, result["cookie"], timeout,
                    result["attempts"],
                )

            result["message"] = _describe(result)
            return result

    failed_url = playlists[0] if playlists else target
    origin_status = _check_origin(session, failed_url, ua, timeout, result["attempts"])
    result["message"] = _failure_message(last_status, failed_url, origin_status)
    return result


def segment_extension(url: str) -> str:
    """The extension the segment is *served as*, ignoring the query string.

    Worth surfacing on its own. FFmpeg's HLS demuxer refuses segments by
    extension, so an anti-leech origin serving MPEG-TS as ".image" or ".png"
    fails for a reason that has nothing to do with headers -- and reads as an
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
            resp, body = _fetch(session, variant_url, headers, timeout)
            record({
                "url": variant_url,
                "status": resp.status_code,
                "note": "variant playlist",
            })
            if not (resp.ok and _is_playlist_body(body)):
                return False
            target = _parse_playlist(body, resp.url).get("first_segment")
        except (requests.RequestException, KeyError, IndexError) as exc:
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
            note = (f"served as {ext} -- disguised segment, FFmpeg refuses "
                    "these by extension")
        record({"url": target, "status": resp.status_code, "note": note})
        return ok
    except requests.RequestException as exc:
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
    if result["segment_ok"] is False:
        parts.append("segments rejected — stream may be session gated")
    return ", ".join(parts) + "."


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
    except requests.RequestException as exc:
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
            f"The access token in this URL {expired} ({status}). These are usually "
            "tied to one browser session and last minutes, so an m3u8 copied from "
            "DevTools is often dead by the time it is pasted. Paste the **page URL** "
            "you watch the stream on instead — PVArr re-resolves it every time it "
            "connects, including on failover, so it gets a fresh token each time."
        )
    if status == 403:
        return (
            "Every header combination was rejected (403), but the host does answer "
            "other requests — so it is gating this stream specifically. It likely "
            "needs a cookie or a referer PVArr cannot guess — copy them from DevTools."
        )
    if status == 404:
        return "Playlist not found (404). If the URL carries a token, it has probably expired."
    if status is None:
        return f"Could not reach {url}."
    return f"No playlist returned (last status {status})."
