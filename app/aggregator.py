#!/usr/bin/env python3
"""
Event pages -> the first three streams that actually work.

An event page (one page linking to several sources for the same event; called
an "aggregator" page in this code) is often what an operator has in hand
before an event. This module fetches that page, pulls out every link that
could be a stream, probes them with the ordinary probe (embed walk +
browser-TLS retry), and keeps the first three that resolve to a recordable
playlist -- in page order, because such pages generally list their best links
first. Those become primary + two
backups for the normal failover.

Nothing here knows any site by name. Links are picked
out by shape: off-site links with a real path, iframes, m3u8 references, and
same-site links that go *deeper* than the event page (a per-stream sub-page).
Navigation, social and asset links fall away on the same rules.

Pages that answer with an anti-bot check (e.g. Cloudflare), in order of
cost:
  1. a plain request;
  2. the same request through a browser-compatible TLS client (curl_cffi,
     already a dependency for the probe);
  3. FlareSolverr, only if `PVARR_FLARESOLVERR_URL` is set -- a separate
     container that loads the page in a real browser.
Interactive captchas are never attempted or solved. For the event page itself
the operator can lend PVArr their own browser session (its Cookie and
User-Agent); a stream link behind such a check is simply skipped.

Everything is bounded: the page is read up to the probe's byte cap, at most
`MAX_LINKS` links are probed, `PROBE_WORKERS` at a time, and the whole probe
phase stops waiting after `PROBE_BUDGET_SEC`. A FlareSolverr solve is capped
at `PVARR_FLARESOLVERR_TIMEOUT` (default 120 s), so a page fetch that needs it
can take about two minutes before the minute of probing starts.
"""

import logging
import os
import re
import threading
import time
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from typing import Any, Callable, Dict, Iterable, List, Optional
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import requests

from app import eventinfo, probe, quality

logger = logging.getLogger("PVArrAggregator")

# Measured on a real event page: 35 links, 13 of them working, 7 of those
# past link 20. Early stopping (first PICK that work) keeps the usual cost
# where it was; the cap only matters on pages with few working links.
MAX_LINKS = 40
PICK = 3
# With ranking on, up to GATHER working links are collected and measured
# (app/quality.py), one at a time, and the best PICK of them kept.
# RANK_BUDGET_SEC is a hard wall clock for all the measuring together.
GATHER = 6
RANK_BUDGET_SEC = 20
# When all GATHER are one source (several sites relaying one origin: 6 of 7
# on a real page), probing goes on down the page for a different one, up to
# GATHER_MAX working links. Only links that add a source are measured on top.
GATHER_MAX = 10
RANK_SLOT_MIN_SEC = 8   # each stream's share of the budget, at least this
PROBE_WORKERS = 6
# Per-request timeout for each link's probe. Shorter than the probe's default:
# a dead link that blackholes would otherwise spend the whole budget on a few
# 8-second waits while working links further down are never reached.
PROBE_TIMEOUT_SEC = 5
PROBE_BUDGET_SEC = 60.0
# FlareSolverr usually loads a checked page in 10-20 s, but one took
# ~71 s: 60 s failed it every time. Overridable by PVARR_FLARESOLVERR_TIMEOUT.
DEFAULT_FLARESOLVERR_TIMEOUT_SEC = 120
FLARESOLVERR_TIMEOUT_BOUNDS = (10, 300)

# Links that are never a stream page, whatever site they are on.
_ASSET_EXTENSIONS = (
    ".css", ".js", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico", ".webp",
    ".woff", ".woff2", ".ttf", ".pdf", ".xml", ".json", ".txt", ".zip",
)
# Anchors, plus the data-* attributes and JS navigation that click-handler
# link tables use instead of a plain href.
_HREF_RE = re.compile(
    r"""(?:<a\b[^>]*?\bhref|\bdata-(?:href|url|link|src))\s*=\s*["']([^"'<>\s]+)["']""", re.I)
_BASE_RE = re.compile(r"""<base\s[^>]*href\s*=\s*["']([^"']+)""", re.I)
# An off-site host linked more often than this is a sister site's menu, not
# streams: each stream site appears once or twice on an event page.
MAX_LINKS_PER_HOST = 5
_JS_NAV_RE = re.compile(
    r"""(?:window\.open|location\.href\s*=|location\.assign)\s*\(?\s*["'](https?://[^"'<>\s]+)["']""",
    re.I)

# Generic marks of a Cloudflare challenge page. Cloudflare is the anti-bot
# vendor, not a provider: these strings are the same on every site it fronts.
_CHALLENGE_MARKERS = (
    b"just a moment", b"challenge-platform", b"cf-chl", b"cf_chl",
    b"challenges.cloudflare.com", b"attention required",
)


def is_challenge(status: int, headers: Dict[str, str], body: bytes) -> bool:
    """True when a response is an anti-bot challenge rather than the page."""
    lowered = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if lowered.get("cf-mitigated", "").lower() == "challenge":
        return True
    if status in (403, 429, 503):
        head = (body or b"")[:65536].lower()
        return any(marker in head for marker in _CHALLENGE_MARKERS)
    return False


def flaresolverr_url() -> str:
    """The FlareSolverr endpoint, or "" when the fallback is disabled."""
    raw = (os.environ.get("PVARR_FLARESOLVERR_URL") or "").strip().rstrip("/")
    if not raw:
        return ""
    return raw if raw.endswith("/v1") else raw + "/v1"


def flaresolverr_timeout() -> int:
    """Seconds FlareSolverr may spend on one page (PVARR_FLARESOLVERR_TIMEOUT)."""
    raw = os.environ.get("PVARR_FLARESOLVERR_TIMEOUT")
    try:
        value = int(float(raw)) if raw not in (None, "") else DEFAULT_FLARESOLVERR_TIMEOUT_SEC
    except (ValueError, OverflowError):
        logger.warning("Ignoring invalid PVARR_FLARESOLVERR_TIMEOUT=%r", raw)
        value = DEFAULT_FLARESOLVERR_TIMEOUT_SEC
    low, high = FLARESOLVERR_TIMEOUT_BOUNDS
    return max(low, min(high, value))


# Path segments that mark an ad or click-tracking hop rather than a stream.
# Generic web vocabulary, no site names. A link is dropped only when one of
# these is a whole path segment AND nothing in the URL looks stream-ish, so
# `/embed/ads-free/1` or `/live/ad/2` survive.
_AD_SEGMENTS = frozenset({
    "ad", "ads", "adv", "advert", "adverts", "advertisement", "banner", "banners",
    "click", "clicks", "clk", "track", "tracking", "redirect", "redir", "aff",
    "affiliate", "visit.php", "click.php", "adclick.php",
})
_STREAM_HINTS = ("m3u8", "live", "stream", "watch", "embed", "channel", "player", "hls")


def _looks_like_ad(url: str) -> bool:
    parts = urlsplit(url)
    segments = [seg for seg in parts.path.lower().split("/") if seg]
    if not any(seg in _AD_SEGMENTS for seg in segments):
        return False
    whole = (parts.path + "?" + parts.query).lower()
    return not any(hint in whole for hint in _STREAM_HINTS)


def _strip_fragment(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, ""))


def _site(url_or_host: str) -> str:
    """Host without a leading www., so www.x.com and x.com are one site."""
    host = urlsplit(url_or_host).hostname if "://" in url_or_host else url_or_host
    host = (host or "").lower()
    return host[4:] if host.startswith("www.") else host


def _unwrap_redirect(url: str, page_site: str) -> str:
    """A same-site `/go?url=https://elsewhere/...` link is the elsewhere link."""
    parts = urlsplit(url)
    if _site(url) != page_site:
        return url
    for _, value in parse_qsl(parts.query):
        if value.startswith(("http://", "https://")) and \
                _site(value) not in ("", page_site):
            return value
    return url


def extract_links(html: str, page_url: str) -> List[str]:
    """Candidate stream links on an event page, best first.

    Order: m3u8 references (already a playlist), then iframes (an embedded
    player), then anchors in page order. Dropped:
      - non-http(s), the page itself, and static assets;
      - off-site links to a bare homepage (nav, sponsors, social profiles);
      - off-site links that carry this page's path in the query (share
        buttons; a bare `?ref=<aggregator host>` is kept -- stream sites ask
        for that);
      - same-site links that neither sit below the page's path nor carry the
        event's slug (navigation to other events), unless they redirect
        off-site;
      - every link to an off-site host linked more than MAX_LINKS_PER_HOST
        times (a sister site's menu);
      - obvious ad / click-tracking hops (`/ad/visit.php` and the like), which
        otherwise use up a probe slot.
    Uncapped: find_streams() caps the list at MAX_LINKS after its own filters.
    """
    page = urlsplit(page_url)
    page_site = _site(page_url)
    page_path = page.path.rstrip("/")
    slug = page_path.rsplit("/", 1)[-1]
    text = probe._normalise(html, dict(probe._JS_VAR_RE.findall(html)))
    # Relative links resolve against <base href> when the page sets one, as
    # in a browser; otherwise a site menu of `watch-nfl-streams/` links looks
    # like sub-pages of the event.
    base_tag = _BASE_RE.search(text)
    base = urljoin(page_url, base_tag.group(1).strip()) if base_tag else page_url

    raw: List[str] = []
    raw += probe._extract_playlists(text, base)
    raw += probe._embedded(text, probe._IFRAME_RE, base)
    raw += [urljoin(base, href.strip()) for href in _HREF_RE.findall(text)]
    raw += _JS_NAV_RE.findall(text)

    kept: List[str] = []
    for url in raw:
        if not url.startswith(("http://", "https://")):
            continue
        url = _unwrap_redirect(_strip_fragment(url), page_site)
        parts = urlsplit(url)
        host = parts.hostname or ""
        path = parts.path.lower()
        if url == _strip_fragment(page_url) or not host:
            continue
        if path.endswith(_ASSET_EXTENSIONS):
            continue
        if _looks_like_ad(url):
            continue
        if _site(host) == page_site:
            # Compared without a trailing slash: `/event/x/` is the page
            # itself, not a stream below it.
            link_path = parts.path.rstrip("/")
            below = page_path and link_path.startswith(page_path + "/")
            same_event = len(slug) >= 4 and slug in link_path and link_path != page_path
            if not (below or same_event):
                continue
        else:
            if parts.path in ("", "/") and not parts.query:
                continue
            if page_path and page_path in unquote(parts.query):
                continue
        kept.append(url)
    kept = probe._dedupe(kept)
    per_host = Counter(urlsplit(u).hostname for u in kept)
    kept = [u for u in kept
            if _site(u) == page_site or per_host[urlsplit(u).hostname] <= MAX_LINKS_PER_HOST]
    return kept


def _plain_session():
    return probe.NoPrivateRedirects(requests.Session())


def _flaresolverr_fetch(target: str, endpoint: str) -> Dict[str, Any]:
    """Ask FlareSolverr to load the page in its browser. Raises on failure.

    The HTTP read timeout sits above FlareSolverr's own, so its answer
    (including its own timeout message) arrives instead of a bare read error.
    """
    limit = flaresolverr_timeout()
    resp = requests.post(
        endpoint,
        json={"cmd": "request.get", "url": target, "maxTimeout": limit * 1000},
        timeout=(10, limit + 15),
    )
    data = resp.json()
    if data.get("status") != "ok" or not isinstance(data.get("solution"), dict):
        raise ValueError(data.get("message") or f"FlareSolverr answered {resp.status_code}")
    return data["solution"]


# Shown with every challenge message: where the two values actually live.
COOKIE_HOWTO = (
    " How to copy them: open the page in your browser, press F12, go to the "
    "Network tab and reload. Click the first request in the list (the page "
    "itself, type \"document\"), and under Request Headers copy the whole "
    "\"cookie\" value and the \"user-agent\" value. Do it just before pressing "
    "Find: the cookie can expire within 30 minutes."
)


def clean_cookie(raw: Optional[str]) -> Optional[str]:
    """Turn the usual ways a cookie gets copied into a Cookie header value.

    Accepts the header line from DevTools' Network tab (with or without the
    leading ``Cookie:``), a row copied from the Application > Cookies table
    (tab separated: name, value, domain, ...), or the bare cf_clearance value.
    """
    s = (raw or "").strip()
    if s.lower().startswith("cookie:"):
        s = s[7:].strip()
    if "\t" in s:
        parts = [p.strip() for p in s.split("\t")]
        s = f"{parts[0]}={parts[1]}" if len(parts) > 1 and parts[1] else parts[0]
    elif s and "=" not in s:
        s = f"cf_clearance={s}"
    return s or None


def clean_user_agent(raw: Optional[str]) -> Optional[str]:
    s = (raw or "").strip()
    if s.lower().startswith("user-agent:"):
        s = s[11:].strip()
    return s or None


def fetch_page(
    url: str,
    cookie: Optional[str] = None,
    user_agent: Optional[str] = None,
    timeout: int = probe.DEFAULT_TIMEOUT,
    abort: Optional[threading.Event] = None,
    fs_retry: bool = False,
) -> Dict[str, Any]:
    """Fetch the event page: plain, then with a browser-compatible TLS client,
    then through FlareSolverr if configured and the page is an anti-bot check.
    `fs_retry` asks FlareSolverr a second time at once if the first solve
    timed out or came back still challenged (a manual Find, where nobody
    retries for you; scheduled scans have their own retry loop).

    Returns ``{ok, url, html, via, status, challenged, attempts, message}``;
    ``via`` is ``plain``, ``chrome`` or ``flaresolverr``.
    """
    out: Dict[str, Any] = {"ok": False, "url": url, "html": "", "via": "",
                           "status": None, "challenged": False,
                           "attempts": [], "message": ""}
    headers = {"Accept": "text/html,*/*"}
    if cookie:
        headers["Cookie"] = cookie

    tries = [("plain", _plain_session)]
    tries.append(("chrome", probe._browser_tls_session))
    for via, make in tries:
        session = make()
        if session is None:
            continue
        sent = dict(headers)
        # curl_cffi brings a User-Agent that matches its TLS profile; only an
        # operator-supplied one (paired with their cf_clearance) overrides it.
        if user_agent or via == "plain":
            sent["User-Agent"] = user_agent or probe.DEFAULT_USER_AGENT
        try:
            resp, body = probe._fetch(session, url, sent, timeout)
        except probe._FETCH_ERRORS as exc:
            out["attempts"].append({"stage": "aggregator", "via": via, "error": str(exc)})
            continue
        challenged = is_challenge(resp.status_code, getattr(resp, "headers", {}), body)
        out["attempts"].append({"stage": "aggregator", "via": via,
                                "status": resp.status_code,
                                "note": "Cloudflare challenge" if challenged else ""})
        out["status"] = resp.status_code
        out["challenged"] = challenged
        if resp.ok and not challenged:
            out.update(ok=True, url=resp.url, via=via,
                       html=body.decode("utf-8", errors="replace"))
            return out
        if not challenged and resp.status_code not in (401, 403, 429, 503):
            break  # a 404 or 500 will not change with a different TLS stack

    endpoint = flaresolverr_url()
    fs_failure = ""
    for attempt in range(2 if fs_retry else 1):
        if not (out["challenged"] and endpoint):
            break
        fs_failure = ""
        try:
            solution = _flaresolverr_fetch(url, endpoint)
            html = str(solution.get("response") or "")
            status = int(solution.get("status") or 0)
            out["attempts"].append({"stage": "aggregator", "via": "flaresolverr",
                                    "status": status})
            if 200 <= status < 400 and html and not is_challenge(status, {}, html.encode()[:65536]):
                out.update(ok=True, url=solution.get("url") or url, via="flaresolverr",
                           html=html, status=status, challenged=False)
                return out
            if not 200 <= status < 400:
                fs_failure = f"it returned HTTP {status}"
                break  # a real HTTP answer will not change on a second ask
            fs_failure = "its page was still a challenge"
        except (requests.RequestException, ValueError, TypeError) as exc:
            out["attempts"].append({"stage": "aggregator", "via": "flaresolverr",
                                    "error": str(exc)})
            fs_failure = str(exc)[:300]
        if abort is not None and abort.is_set():
            break
    # Kept for callers that report the outcome elsewhere (the scheduler's
    # notification), which must say FlareSolverr was tried.
    out["flaresolverr_failed"] = bool(fs_failure)
    fs_line = (f"FlareSolverr did not solve the challenge in {flaresolverr_timeout()} s: "
               f"{fs_failure}. " if fs_failure else "")

    if out["challenged"] and cookie:
        has_clearance = "cf_clearance=" in cookie
        out["message"] = fs_line + (
            "Cloudflare still challenged PVArr with your cookie"
            + (" (it does contain cf_clearance)" if has_clearance
               else " -- but it has no cf_clearance in it, so it was probably copied "
                    "from the wrong request; copy the Cookie header of the page itself")
            + ("" if user_agent else ", and no User-Agent was pasted (Cloudflare ties "
                                     "the cookie to the browser's User-Agent)")
            + ". If both are right, the cookie is tied to an IP PVArr does not share: "
            "the browser may have reached Cloudflare over IPv6 while the container uses "
            "IPv4, or it expired (many sites keep it only 30 minutes)."
            + COOKIE_HOWTO
        )
    elif out["challenged"]:
        out["message"] = fs_line + (
            "The event page answered with a Cloudflare check that PVArr does not complete itself"
            + ("" if endpoint else " (FlareSolverr is not configured)")
            + ". Open it in your browser, then paste that browser's Cookie "
            "(it holds cf_clearance) and User-Agent into the fields below and try again."
            + COOKIE_HOWTO
        )
    elif out["status"] is None:
        out["message"] = f"Could not reach {url}."
    else:
        out["message"] = f"The event page answered HTTP {out['status']}."
    return out


def _probe_one(url: str) -> Dict[str, Any]:
    # yt-dlp off: it is the slow last resort and would eat the whole budget on
    # links that are simply dead. The recorder still tries it on connect.
    return probe.probe_stream(url, use_ytdlp=False, timeout=PROBE_TIMEOUT_SEC)


def _works(res: Dict[str, Any]) -> bool:
    # A playlist whose segments were refused would fail the moment FFmpeg
    # starts; it does not count as working.
    return bool(res.get("ok")) and res.get("segment_ok") is not False


def pick_first_working(
    links: List[str],
    probe_fn: Callable[[str], Dict[str, Any]] = _probe_one,
    pick: int = PICK,
    workers: int = PROBE_WORKERS,
    budget: float = PROBE_BUDGET_SEC,
    abort: Optional[threading.Event] = None,
    gather: Optional[int] = None,
    measure_fn: Optional[Callable[[Dict[str, Any], float], Dict[str, Any]]] = None,
    gather_max: int = GATHER_MAX,
) -> Dict[str, Any]:
    """Probe links concurrently; keep the first `pick` that work, in link order.

    With `measure_fn`, up to `gather` working links are collected the same
    way, measured one at a time within RANK_BUDGET_SEC, and the best `pick`
    kept by quality.score (backups from other sources where possible), best
    first; ties keep page order. If those `gather` are all one source,
    probing goes on (up to `gather_max` working links) for a different one,
    and only links adding a source are measured on top.

    "First" means page order, not finishing order: a fast link further down
    must not displace a slower one above it. So the search ends only once the
    first `pick` successes all have every earlier link decided, or the budget
    runs out (then whatever is decided is used). Probes still running at the
    deadline are abandoned, not killed -- each is bounded by its own timeouts
    and fetch caps, and finishes in the background.

    `abort` (set at shutdown) ends the search early and cancels every probe
    not yet started. Without it the pool's queued probes all still ran at
    interpreter exit -- the executor's workers are joined then -- which held
    a container stop for close to a minute.
    """
    want = max(pick, gather or pick) if measure_fn else pick
    want_max = max(want, gather_max) if measure_fn else want
    results: Dict[int, Dict[str, Any]] = {}

    def key(i: int) -> str:
        return quality.source_key(results[i].get("m3u8_url", "")) or f"?{i}"

    def enough(found: List[int]) -> bool:
        return len(found) >= want and (len(found) >= want_max or len({key(j) for j in found}) > 1)
    deadline = time.monotonic() + budget
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(probe_fn, url): i for i, url in enumerate(links)}

    def settled() -> bool:
        found: List[int] = []
        for i in range(len(links)):
            if i not in results:
                return False
            if _works(results[i]):
                found.append(i)
                if enough(found):
                    return True
        return True

    timed_out = False
    try:
        pending = set(futures)
        while pending and not settled():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if abort is not None and abort.is_set():
                break
            # Short waits only so an abort is noticed; finishing order is
            # unaffected.
            done, pending = wait(pending, timeout=min(remaining, 0.5),
                                 return_when=FIRST_COMPLETED)
            for fut in done:
                try:
                    results[futures[fut]] = fut.result()
                except Exception as exc:  # a probe bug must not sink the batch
                    results[futures[fut]] = {"ok": False, "message": f"probe error: {exc}"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    # Cut where the search may stop, not at whatever else finished probing
    # meanwhile: past `want`, that is at most the one link adding a source.
    found: List[int] = []
    for i in range(len(links)):
        if i in results and _works(results[i]):
            found.append(i)
            if enough(found):
                break
    working, seen = found[:want], {key(i) for i in found[:want]}
    for i in found[want:]:      # past `gather`: only a source not yet seen
        if key(i) not in seen:
            working.append(i)
            seen.add(key(i))
    measured: Dict[int, Dict[str, Any]] = {}
    if measure_fn and len(working) > 1 and not (abort is not None and abort.is_set()):
        measured = _measure_all({i: results[i] for i in working}, measure_fn, abort)
    # Every sampled segment was something other than video (a real image, an
    # HTML page): the playlist works but FFmpeg cannot record what it lists,
    # so it is no candidate at all, let alone pick 1.
    not_video = {i for i in working if measured.get(i, {}).get("not_video")}
    working = [i for i in working if i not in not_video]
    if measured:
        # Stable sort: equal scores keep page order.
        working.sort(key=lambda i: quality.score(measured.get(i, {})))
    keys = {i: quality.source_key(results[i].get("m3u8_url", "")) for i in working}
    if measured:
        # Backups from different real sources where possible (several sites
        # often relay one origin; they die together).
        slow = {i: quality.score(measured.get(i, {}))[0] == 2 for i in working}
        chosen = quality.diversify(working, keys, slow, pick)
    else:
        chosen = working[:pick]

    picked, tried = [], []
    for i in chosen:
        res = results[i]
        q = measured.get(i)
        message = res.get("message", "")
        if q:
            message = f"{quality.summary(q)} -- {message}" if quality.summary(q) else message
        picked.append({
            "url": links[i],
            "m3u8_url": res.get("m3u8_url", ""),
            "referer": res.get("referer", ""),
            "user_agent": res.get("user_agent", ""),
            "impersonate": bool(res.get("impersonate")),
            "message": message,
            **({"quality": q} if q else {}),
        })
    for i, url in enumerate(links):
        res = results.get(i)
        if res is None:
            tried.append({"url": url, "ok": False,
                          "note": "not checked (time budget)" if timed_out else "not needed"})
            continue
        ok = _works(res) and i not in not_video
        note = res.get("message", "") if not ok else "works"
        if i in not_video:
            note = "segment is not video -- FFmpeg cannot record it"
        if ok and i in measured:
            rank = chosen.index(i) + 1 if i in chosen else None
            same = next((n + 1 for n, j in enumerate(chosen)
                         if j != i and keys.get(j) and keys.get(j) == keys.get(i)), None)
            note = ("works" + (f", pick {rank}" if rank else ", ranked lower")
                    + (f" (same source as pick {same})" if same else "")
                    + (f": {quality.summary(measured[i])}" if quality.summary(measured[i]) else ""))
        tried.append({"url": url, "ok": ok, "note": note})
    sources = len({keys.get(i) or f"?{i}" for i in chosen})
    summary = _source_summary(links, results, chosen, working, not_video, tried)
    return {"picked": picked, "tried": tried, "timed_out": timed_out, "sources": sources,
            "summary": summary}


def _source_summary(links: List[str], results: Dict[int, Dict[str, Any]], chosen: List[int],
                    working: List[int], not_video: set,
                    tried: List[Dict[str, Any]]) -> Dict[str, Any]:
    """How many real feeds the page's links come down to.

    Each link gets a status and, when a playlist was found, the letter of its
    real feed (quality.source_letters: mirrors of one origin on numbered edge
    servers share a letter). Letters follow the picks, so the primary is A.
    Fills `source` and `status` into `tried` in place. Links whose player
    builds its URL in JavaScript have no playlist and so no letter: they are
    counted as unresolved, never guessed at.
    """
    def status(i: int) -> str:
        res = results.get(i)
        if res is None:
            return "not_checked"
        if i in not_video:
            return "not_video"
        if _works(res):
            return "working"
        return "failed" if res.get("m3u8_url") else "unresolved"

    order = list(chosen) + [i for i in working if i not in chosen]
    order += [i for i in range(len(links)) if i not in order]
    urls = [(results.get(i) or {}).get("m3u8_url", "") for i in order]
    letter = dict(zip(order, quality.source_letters(urls)))
    counts = {k: 0 for k in ("working", "not_video", "failed", "unresolved", "not_checked")}
    groups: Dict[str, Dict[str, Any]] = {}
    for i in order:
        st = status(i)
        counts[st] += 1
        tried[i]["status"], tried[i]["source"] = st, letter[i]
        if letter[i]:
            g = groups.setdefault(letter[i], {
                "source": letter[i],
                "host": urlsplit(urls[order.index(i)]).hostname or "",
                "links": 0, "working": 0, "not_video": 0})
            g["links"] += 1
            if st in ("working", "not_video"):
                g[st] += 1
    # A working link without a playlist URL (not expected) is its own feed.
    real = {letter[i] or f"?{i}" for i in range(len(links)) if status(i) == "working"}
    return {"links": len(links), **counts, "sources": len(real),
            "groups": list(groups.values())}


def summary_text(summary: Dict[str, Any]) -> str:
    """'12 links: 4 working from 2 real sources (A x3, B x1), 2 not video,
    6 unresolved.' -- zero counts left out."""
    if not summary or not summary.get("links"):
        return ""
    n = summary["links"]
    text = f"{n} link{'s' if n != 1 else ''}"
    parts = []
    if summary["working"]:
        mirrors = ", ".join(f"{g['source']} \u00d7{g['working']}"
                            for g in summary["groups"] if g["working"])
        parts.append(f"{summary['working']} working from {summary['sources']} real source"
                     f"{'s' if summary['sources'] != 1 else ''}"
                     + (f" ({mirrors})" if mirrors else ""))
    else:
        parts.append("none working")
    for key, label in (("not_video", "not video"), ("failed", "failed"),
                       ("unresolved", "unresolved"), ("not_checked", "not checked")):
        if summary.get(key):
            parts.append(f"{summary[key]} {label}")
    return f"{text}: " + ", ".join(parts) + "."


def _measure_all(candidates: Dict[int, Dict[str, Any]],
                 measure_fn: Callable[[Dict[str, Any], float], Dict[str, Any]],
                 abort: Optional[threading.Event] = None,
                 budget: Optional[float] = None) -> Dict[int, Dict[str, Any]]:
    """Measure candidates one at a time, in page order, under a hard wall
    clock; the rest count as unmeasured.

    One at a time on purpose: measured together they share the link and the
    numbers stop meaning anything. Each runs on a daemon thread that is
    abandoned, not waited for, if the clock runs out: a server trickling a
    byte a second must not hold the event-page lock (and every Find and
    scheduled scan behind it). An abandoned measurement ends measuring.

    Each candidate gets its own slot -- an even share of what is left, at
    least RANK_SLOT_MIN_SEC, ending 1 s before the budget -- and quality.measure
    cuts its downloads short at the slot's end, so a slow stream at the top of
    the page is measured as slow instead of eating the budget unmeasured.
    """
    budget = RANK_BUDGET_SEC if budget is None else budget
    deadline = time.monotonic() + budget
    out: Dict[int, Dict[str, Any]] = {}
    order = sorted(candidates)
    for n, i in enumerate(order):
        now = time.monotonic()
        remaining = deadline - now
        if remaining <= 0 or (abort is not None and abort.is_set()):
            break
        slot_end = min(deadline - 1.0, now + max(remaining / (len(order) - n), RANK_SLOT_MIN_SEC))
        box: Dict[str, Any] = {}

        def run(res=candidates[i], box=box, slot_end=slot_end):
            try:
                box["q"] = measure_fn(res, slot_end)
            except Exception:  # a measuring bug must not sink the pick
                pass
        worker = threading.Thread(target=run, name="pvarr-measure", daemon=True)
        worker.start()
        while worker.is_alive() and time.monotonic() < deadline:
            if abort is not None and abort.is_set():
                return out      # shutting down: abandon it now
            worker.join(min(0.25, max(0.0, deadline - time.monotonic())))
        if worker.is_alive():
            break
        if "q" in box:
            out[i] = box["q"]
    return out


def find_streams(
    url: str,
    cookie: Optional[str] = None,
    user_agent: Optional[str] = None,
    probe_fn: Callable[[str], Dict[str, Any]] = _probe_one,
    abort: Optional[threading.Event] = None,
    fs_retry: bool = False,
    exclude: Iterable[str] = (),
) -> Dict[str, Any]:
    """The whole job: aggregator URL in, up to three working candidates out.

    `exclude` skips links already in hand, so a re-check during a recording
    looks further down the page instead of finding the same three again.
    """
    result: Dict[str, Any] = {"ok": False, "page": {}, "links_found": 0,
                              "picked": [], "tried": [], "message": ""}
    try:
        target = probe.clean_url(url)
    except probe.ProbeError as exc:
        result["message"] = str(exc)
        return result

    page = fetch_page(target, cookie=clean_cookie(cookie),
                      user_agent=clean_user_agent(user_agent),
                      abort=abort, fs_retry=fs_retry)
    result["page"] = {k: page[k] for k in ("ok", "url", "via", "status", "challenged", "attempts")}
    result["page"]["flaresolverr_failed"] = bool(page.get("flaresolverr_failed"))
    if not page["ok"]:
        result["message"] = page["message"]
        return result

    # Who is playing and when, read from the page already in hand.
    # Optional extra: a page it cannot read must never cost the stream scan.
    try:
        result["event"] = eventinfo.parse(page["html"], page["url"])["event"]
    except Exception:
        logger.exception("Could not read the game from %s", page["url"])
        result["event"] = None

    links = extract_links(page["html"], page["url"])
    # The SSRF guard from the embed walk, applied the same way: a link a
    # stranger's page names may not point PVArr into the LAN, unless the
    # operator pasted a LAN page in the first place.
    if not probe.is_private_url(target):
        public = [u for u in links if not probe.is_private_url(u)]
        result["skipped_private"] = len(links) - len(public)
        links = public
    held = {u.rstrip("/") for u in exclude}
    links = [u for u in links if u.rstrip("/") not in held][:MAX_LINKS]
    result["links_found"] = len(links)
    if not links:
        result["message"] = ("No stream links found on that page. If it only shows "
                             "them after a click or a login, paste the stream pages directly.")
        return result

    # Ranking: a public page's streams may not lead PVArr onto the LAN, the
    # same rule as the links themselves.
    allow_private = probe.is_private_url(target)
    measure = None
    if quality.enabled():
        def measure(res, deadline):
            return quality.measure(res, allow_private=allow_private, deadline=deadline)
    chosen = pick_first_working(links, probe_fn=probe_fn, abort=abort,
                                gather=GATHER, measure_fn=measure)
    result.update(picked=chosen["picked"], tried=chosen["tried"])
    result["ok"] = bool(chosen["picked"])
    n = len(chosen["picked"])
    result["summary"] = chosen["summary"]
    counts = summary_text(chosen["summary"])
    if n:
        result["message"] = (f"Picked {n} stream{'s' if n != 1 else ''}"
                             + (", best first by measured quality" if any("quality" in p for p in chosen["picked"])
                                else "")
                             + f". {counts}").rstrip()
    else:
        result["message"] = ("No recordable stream"
                             + (" before the time limit" if chosen["timed_out"] else "")
                             + f". {counts}").rstrip()
    return result


def read_event(
    url: str,
    cookie: Optional[str] = None,
    user_agent: Optional[str] = None,
    abort: Optional[threading.Event] = None,
    fs_retry: bool = False,
) -> Dict[str, Any]:
    """Fetch a page and read the game(s) it describes; no stream probing.

    An event page gives `event` (teams, league, start); a listing page gives
    `events`, one per game it links. Returns ``{ok, page, event, events,
    message}``; the event URLs are only returned, never fetched.
    """
    result: Dict[str, Any] = {"ok": False, "page": {}, "event": None,
                              "events": [], "message": ""}
    try:
        target = probe.clean_url(url)
    except probe.ProbeError as exc:
        result["message"] = str(exc)
        return result
    page = fetch_page(target, cookie=clean_cookie(cookie),
                      user_agent=clean_user_agent(user_agent),
                      abort=abort, fs_retry=fs_retry)
    result["page"] = {k: page[k] for k in ("ok", "url", "via", "status", "challenged", "attempts")}
    if not page["ok"]:
        result["message"] = page["message"]
        return result
    try:
        found = eventinfo.parse(page["html"], page["url"])
    except Exception:
        logger.exception("Could not read the game from %s", page["url"])
        found = {"event": None, "events": []}
    # The find_streams rule for links a stranger's page names: none into the
    # LAN unless the operator pasted a LAN page. A game link is scheduled as
    # an event page, whose scan then trusts private streams. One lookup per
    # host, not per game.
    if found["events"] and not probe.is_private_url(target):
        private: Dict[str, bool] = {}
        kept = []
        for ev in found["events"]:
            host = urlsplit(ev.get("url") or "").hostname or ""
            if host not in private:
                private[host] = probe.is_private_url(ev.get("url") or "")
            if not private[host]:
                kept.append(ev)
        result["skipped_private"] = len(found["events"]) - len(kept)
        found["events"] = kept
    result.update(found)
    result["ok"] = bool(found["event"] or found["events"])
    if found["events"]:
        n = len(found["events"])
        result["message"] = f"{n} game{'s' if n != 1 else ''} listed on that page."
    elif found["event"]:
        result["message"] = "Read the game from the page."
    else:
        result["message"] = ("No teams or games found on that page; "
                             "fill in the details by hand.")
    return result
