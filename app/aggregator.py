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
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import parse_qsl, unquote, urljoin, urlsplit, urlunsplit

import requests

from app import probe

logger = logging.getLogger("PVArrAggregator")

MAX_LINKS = 20
PICK = 3
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
    return kept[:MAX_LINKS]


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
) -> Dict[str, Any]:
    """Probe links concurrently; keep the first `pick` that work, in link order.

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
    results: Dict[int, Dict[str, Any]] = {}
    deadline = time.monotonic() + budget
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    futures = {pool.submit(probe_fn, url): i for i, url in enumerate(links)}

    def settled() -> bool:
        found = 0
        for i in range(len(links)):
            if i not in results:
                return False
            if _works(results[i]):
                found += 1
                if found >= pick:
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

    picked, tried = [], []
    for i, url in enumerate(links):
        res = results.get(i)
        if res is None:
            tried.append({"url": url, "ok": False,
                          "note": "not checked (time budget)" if timed_out else "not needed"})
            continue
        ok = _works(res)
        if ok and len(picked) < pick:
            picked.append({
                "url": url,
                "m3u8_url": res.get("m3u8_url", ""),
                "referer": res.get("referer", ""),
                "user_agent": res.get("user_agent", ""),
                "impersonate": bool(res.get("impersonate")),
                "message": res.get("message", ""),
            })
        tried.append({"url": url, "ok": ok,
                      "note": res.get("message", "") if not ok else "works"})
    return {"picked": picked, "tried": tried, "timed_out": timed_out}


def find_streams(
    url: str,
    cookie: Optional[str] = None,
    user_agent: Optional[str] = None,
    probe_fn: Callable[[str], Dict[str, Any]] = _probe_one,
    abort: Optional[threading.Event] = None,
    fs_retry: bool = False,
) -> Dict[str, Any]:
    """The whole job: aggregator URL in, up to three working candidates out."""
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

    links = extract_links(page["html"], page["url"])
    # The SSRF guard from the embed walk, applied the same way: a link a
    # stranger's page names may not point PVArr into the LAN, unless the
    # operator pasted a LAN page in the first place.
    if not probe.is_private_url(target):
        public = [u for u in links if not probe.is_private_url(u)]
        result["skipped_private"] = len(links) - len(public)
        links = public
    result["links_found"] = len(links)
    if not links:
        result["message"] = ("No stream links found on that page. If it only shows "
                             "them after a click or a login, paste the stream pages directly.")
        return result

    chosen = pick_first_working(links, probe_fn=probe_fn, abort=abort)
    result.update(picked=chosen["picked"], tried=chosen["tried"])
    result["ok"] = bool(chosen["picked"])
    n = len(chosen["picked"])
    checked = sum(1 for t in chosen["tried"] if not t["note"].startswith("not "))
    if n:
        result["message"] = (f"{n} working stream{'s' if n != 1 else ''} out of "
                             f"{checked} checked ({len(links)} links on the page).")
    else:
        result["message"] = (f"None of the {checked} links checked gave a recordable stream"
                             + (" before the time limit" if chosen["timed_out"] else "") + ".")
    return result
