"""
Browser resolver - PVArr sidecar

Turns a page whose player builds its playlist URL in JavaScript into
{playlist URL + the headers the browser sent for it}, the way Chrome's network
tab shows it. Generic: no site knows about this code and this code knows about
no site.

Per request:
  1. A fresh browser context (no shared cookies or storage), audio muted,
     1280x720, service workers blocked, images/fonts/media blocked (a playlist
     request never is), requests and WebSockets to private addresses refused
     (a host the DNS server answers 0.0.0.0 for is counted apart, so a miss
     caused by a DNS blocklist says so).
  2. Every response in every frame is watched; the first one whose body is an
     HLS playlist (`#EXTM3U`) or a JSON segment list wins, and that request's
     own Referer / Origin / Cookie are kept.
  3. Load the page and wait up to WAIT_FIRST seconds.
  4. Nothing yet: nudge the player up to NUDGES times -- muted `video.play()`
     and a scripted `.click()` on play controls in every frame (popup ads
     ignore untrusted clicks), then one real click on the biggest player.
     New tabs and popups are closed as they open.
  5. Return the winner, or ok=False with a note. The context is always closed.

Every browser call has its own short limit, because a page can freeze its own
main thread and every call into it would wait forever; the caller adds an
overall limit on top. One Chromium process is shared by all requests ("new
headless" mode, which resolved far more pages than the old headless shell
in testing) and relaunched if it dies. Chromium runs without its own sandbox, as
Playwright does by default: the container and its network isolation are the
boundary (see docs/browser-sidecar.md).
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, Set

from netguard import host_of, request_is_private_resolved

BLOCK_TYPES = {"image", "media", "font"}
SEGMENT_RE = re.compile(r"\.(ts|m4s|aac|mp4|m4a|png|jpe?g|webp|gif)(\?|$)", re.I)
WAIT_FIRST = 8.0
NUDGES = 3
CALL_LIMIT = 5.0               # any single call into the page
MAX_BODY = 2 * 1024 * 1024     # a playlist is a few KB; never buffer a segment

_PLAY_SCRIPT = """() => { let n = 0;
  for (const v of document.querySelectorAll('video')) { v.muted = true; try { v.play(); } catch (e) {} }
  for (const b of document.querySelectorAll('[aria-label*=play i], [class*=play i], [id*=play i], [title*=play i]')) {
    const r = b.getBoundingClientRect();
    if (r.width > 8 && r.height > 8 && !(String(b.className)).match(/playlist|display|played/i)) {
      b.click(); n++; if (n >= 3) break; }
  }
  return n; }"""

_BIGGEST_PLAYER = """() => {
  let best = null, area = 0;
  for (const el of document.querySelectorAll('video, iframe, [class*=player], [id*=player]')) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height > area && r.width > 100 && r.height > 80) { area = r.width * r.height; best = r; }
  }
  return best && {x: best.x + best.width / 2, y: best.y + best.height / 2}; }"""


def user_agent(version: str) -> str:
    """A desktop Chrome UA naming the Chromium version actually running (the
    headless default says "HeadlessChrome", a version string desktop players
    don't expect)."""
    major = (version or "120").split(".")[0]
    return (f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{major}.0.0.0 Safari/537.36")


def playlist_kind(body: bytes) -> str:
    """'m3u8', 'json_list' or '' for a response body."""
    head = body[:64].lstrip()
    if head.startswith(b"#EXTM3U"):
        return "m3u8"
    if head[:1] == b"{":
        try:
            data = json.loads(body)
        except ValueError:
            return ""
        segs = data.get("segments") if isinstance(data, dict) else None
        if isinstance(segs, list) and segs and all(
                isinstance(s, dict) and isinstance(s.get("url"), str)
                and isinstance(s.get("duration"), (int, float)) for s in segs[:5]):
            return "json_list"
    return ""


def _browser_gone(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "closed" in text or "disconnected" in text or "crash" in text


async def _limited(coro, secs: float = CALL_LIMIT):
    """Run one call into the page; None if it fails or takes too long."""
    try:
        return await asyncio.wait_for(coro, max(0.1, secs))
    except Exception:
        return None


class Resolver:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _browser_ready(self, relaunch: bool = False):
        async with self._lock:
            if relaunch and self._browser is not None:
                old, self._browser = self._browser, None
                asyncio.ensure_future(_limited(old.close()))
            if self._browser is None or not self._browser.is_connected():
                from playwright.async_api import async_playwright
                if self._pw is None:
                    self._pw = await async_playwright().start()
                self._browser = await self._pw.chromium.launch(
                    headless=True, channel="chromium",
                    args=["--autoplay-policy=no-user-gesture-required", "--mute-audio"])
            return self._browser

    async def close(self) -> None:
        if self._browser is not None:
            await _limited(self._browser.close(), 10)
        if self._pw is not None:
            await _limited(self._pw.stop(), 10)
        self._browser = self._pw = None

    async def _new_context(self):
        """A fresh context; relaunches Chromium once if it has died."""
        for attempt in (0, 1):
            browser = await self._browser_ready(relaunch=attempt == 1)
            try:
                ua = user_agent(browser.version)
                ctx = await browser.new_context(user_agent=ua, viewport={"width": 1280, "height": 720},
                                                accept_downloads=False, service_workers="block")
                return ctx, ua
            except Exception as exc:
                if attempt or not _browser_gone(exc):
                    raise
        raise RuntimeError("unreachable")

    async def resolve(self, url: str, timeout: float) -> Dict[str, Any]:
        t0 = time.monotonic()
        left = lambda: timeout - (time.monotonic() - t0)
        out: Dict[str, Any] = {"ok": False, "m3u8_url": "", "kind": "", "referer": "", "origin": "",
                               "cookie": "", "user_agent": "", "page_title": "", "elapsed": 0.0,
                               "nudges": 0, "note": ""}
        try:
            ctx, out["user_agent"] = await self._new_context()
        except Exception as exc:
            out["note"] = f"browser unavailable: {type(exc).__name__}"
            return out
        found = asyncio.Event()
        tasks: Set[asyncio.Future] = set()
        blocked_private = 0
        blocked_dns = 0
        dns_cache: Dict[str, bool] = {}
        dns_blocked: Set[str] = set()       # hosts the resolver answers 0.0.0.0 for

        def count_blocked(url: str) -> None:
            nonlocal blocked_private, blocked_dns
            if host_of(url) in dns_blocked:
                blocked_dns += 1
            else:
                blocked_private += 1

        def spawn(coro) -> None:
            task = asyncio.ensure_future(coro)
            tasks.add(task)
            task.add_done_callback(lambda t: (tasks.discard(t), t.cancelled() or t.exception()))

        async def route(r):
            req = r.request
            try:
                if await request_is_private_resolved(req.url, dns_cache, dns_blocked):
                    count_blocked(req.url)
                    await r.abort()
                elif ".m3u8" in req.url.lower():
                    await r.continue_()
                elif req.resource_type in BLOCK_TYPES or SEGMENT_RE.search(req.url.split("#")[0]):
                    await r.abort()
                else:
                    await r.continue_()
            except Exception:
                pass        # the context closed under it

        async def web_socket(ws):
            if await request_is_private_resolved(ws.url, dns_cache, dns_blocked):
                count_blocked(ws.url)
                await ws.close()
            else:
                ws.connect_to_server()

        async def on_response(resp):
            try:
                if found.is_set():
                    return
                req = resp.request
                if req.resource_type not in ("xhr", "fetch", "document", "other", "media"):
                    return
                ctype = resp.headers.get("content-type", "")
                looks_playlist = ".m3u8" in resp.url.lower() or "mpegurl" in ctype
                if resp.status >= 400 or not (looks_playlist or "json" in ctype):
                    return
                length = resp.headers.get("content-length")
                # Only bodies known to be small, or that say they are playlists:
                # a page must not make us buffer a huge chunked response.
                if (int(length) if length and length.isdigit() else -1) > MAX_BODY \
                        or (length is None and not looks_playlist):
                    return
                body = await asyncio.wait_for(resp.body(), CALL_LIMIT)
                kind = playlist_kind(body[:MAX_BODY])
                if not kind or found.is_set():
                    return
                headers = await asyncio.wait_for(req.all_headers(), CALL_LIMIT)
                if found.is_set():
                    return
                out.update(ok=True, m3u8_url=resp.url, kind=kind, referer=headers.get("referer", ""),
                           origin=headers.get("origin", ""), cookie=headers.get("cookie", ""))
                found.set()
            except Exception:
                pass

        try:
            await ctx.route("**/*", route)
            await ctx.route_web_socket(re.compile(".*"), web_socket)
            ctx.on("response", lambda resp: spawn(on_response(resp)))
            page = await ctx.new_page()
            # Popups and new tabs (ad clickjacking) are closed as they open.
            ctx.on("page", lambda p: spawn(_limited(p.close())) if p is not page else None)
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=max(1000, int(min(20.0, left()) * 1000)))
            except Exception as exc:
                out["note"] = f"page load: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            await self._wait(found, min(WAIT_FIRST, left()))
            while not found.is_set() and out["nudges"] < NUDGES and left() > 1:
                out["nudges"] += 1
                for frame in page.frames:
                    if left() <= 1:
                        break
                    await _limited(frame.evaluate(_PLAY_SCRIPT), min(CALL_LIMIT, left()))
                if await self._wait(found, min(3.0, left())):
                    break
                box = await _limited(page.evaluate(_BIGGEST_PLAYER), min(CALL_LIMIT, left()))
                if box:
                    await _limited(page.mouse.click(box["x"], box["y"]), min(CALL_LIMIT, left()))
                await self._wait(found, min(5.0, left()))
            out["page_title"] = ((await _limited(page.title(), 2.0)) or "")[:120]
        except Exception as exc:
            if not out["note"]:
                out["note"] = f"browser error: {type(exc).__name__}"
        finally:
            for task in list(tasks):
                task.cancel()
            # Shielded: a cancelled request must still close its page (~0.5 GB).
            try:
                await asyncio.wait_for(asyncio.shield(ctx.close()), CALL_LIMIT)
            except Exception:
                # A page that will not close is stuck in a renderer: restart
                # Chromium so its memory comes back.
                await _limited(self._browser_ready(relaunch=True), 20)
        if not out["ok"] and not out["note"]:
            why = []
            if blocked_dns:
                why.append(f"{blocked_dns} request(s) to {len(dns_blocked)} host(s) blocked by DNS: the "
                           "sidecar's DNS server answers 0.0.0.0 for them, give it another one")
            if blocked_private:
                why.append(f"{blocked_private} request(s) to private addresses blocked")
            out["note"] = "no playlist request seen" + (f" ({'; '.join(why)})" if why else "")
        out["elapsed"] = round(time.monotonic() - t0, 1)
        return out

    @staticmethod
    async def _wait(event: asyncio.Event, secs: float) -> bool:
        if secs <= 0:
            return event.is_set()
        try:
            await asyncio.wait_for(event.wait(), secs)
            return True
        except asyncio.TimeoutError:
            return False
