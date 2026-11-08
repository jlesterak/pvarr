"""
Browser resolver - PVArr sidecar

Turns a page whose player builds its playlist URL in JavaScript into
{playlist URL + the headers the browser sent for it}, the way Chrome's network
tab shows it. Generic: no site knows about this code and this code knows about
no site.

Per request:
  1. A fresh browser context (no shared cookies or storage), audio muted,
     1280x720, images/fonts/media blocked (a playlist request never is).
  2. Every response in every frame is watched; the first one whose body is an
     HLS playlist (`#EXTM3U`) or a JSON segment list wins, and that request's
     own Referer / Origin / Cookie are kept.
  3. Load the page and wait up to WAIT_FIRST seconds.
  4. Nothing yet: nudge the player up to NUDGES times -- muted `video.play()`
     and a scripted `.click()` on play controls in every frame (popup ads
     ignore untrusted clicks), then one real click on the biggest player.
     New tabs and popups are closed as they open.
  5. Return the winner, or ok=False with a note. The context is always closed.

One Chromium process is shared by all requests ("new headless" mode, which
pages detect far less often than the old headless shell); the caller limits
how many requests run at once.
"""

import asyncio
import json
import re
import time
from typing import Any, Dict, Optional

from netguard import request_is_private

UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")
BLOCK_TYPES = {"image", "media", "font"}
SEGMENT_RE = re.compile(r"\.(ts|m4s|aac|mp4|m4a|png|jpe?g|webp|gif)(\?|$)", re.I)
WAIT_FIRST = 8.0
NUDGES = 3
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


class Resolver:
    def __init__(self) -> None:
        self._pw = None
        self._browser = None
        self._lock = asyncio.Lock()

    async def _browser_ready(self):
        async with self._lock:
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
            await self._browser.close()
        if self._pw is not None:
            await self._pw.stop()
        self._browser = self._pw = None

    async def resolve(self, url: str, timeout: float) -> Dict[str, Any]:
        t0 = time.monotonic()
        out: Dict[str, Any] = {"ok": False, "m3u8_url": "", "kind": "", "referer": "", "origin": "",
                               "cookie": "", "user_agent": UA, "page_title": "", "elapsed": 0.0,
                               "nudges": 0, "note": ""}
        browser = await self._browser_ready()
        ctx = await browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 720},
                                        accept_downloads=False)
        found = asyncio.Event()
        blocked_private = 0

        async def route(r):
            nonlocal blocked_private
            req = r.request
            if request_is_private(req.url):
                blocked_private += 1
                await r.abort()
            elif ".m3u8" in req.url.lower():
                await r.continue_()
            elif req.resource_type in BLOCK_TYPES or SEGMENT_RE.search(req.url.split("#")[0]):
                await r.abort()
            else:
                await r.continue_()

        async def on_response(resp):
            if found.is_set():
                return
            req = resp.request
            if req.resource_type not in ("xhr", "fetch", "document", "other", "media"):
                return
            ctype = resp.headers.get("content-type", "")
            if resp.status >= 400 or not (".m3u8" in resp.url.lower() or "mpegurl" in ctype
                                          or "json" in ctype):
                return
            try:
                if int(resp.headers.get("content-length") or 0) > MAX_BODY:
                    return
                body = await resp.body()
            except Exception:
                return
            kind = playlist_kind(body[:MAX_BODY])
            if not kind or found.is_set():
                return
            headers = await req.all_headers()
            out.update(ok=True, m3u8_url=resp.url, kind=kind, referer=headers.get("referer", ""),
                       origin=headers.get("origin", ""), cookie=headers.get("cookie", ""))
            found.set()

        await ctx.route("**/*", route)
        ctx.on("response", lambda resp: asyncio.ensure_future(on_response(resp)))
        page = await ctx.new_page()
        # Popups and new tabs (ad clickjacking) are closed as they open.
        ctx.on("page", lambda p: asyncio.ensure_future(p.close()) if p is not page else None)
        left = lambda: timeout - (time.monotonic() - t0)
        try:
            try:
                await page.goto(url, wait_until="domcontentloaded",
                                timeout=max(1000, int(min(20.0, left()) * 1000)))
            except Exception as exc:
                out["note"] = f"page load: {type(exc).__name__}: {str(exc).splitlines()[0][:120]}"
            await self._wait(found, min(WAIT_FIRST, left()))
            while not found.is_set() and out["nudges"] < NUDGES and left() > 1:
                out["nudges"] += 1
                for frame in page.frames:
                    try:
                        await frame.evaluate(_PLAY_SCRIPT)
                    except Exception:
                        pass
                if await self._wait(found, min(3.0, left())):
                    break
                try:
                    box = await page.evaluate(_BIGGEST_PLAYER)
                    if box:
                        await page.mouse.click(box["x"], box["y"])
                except Exception:
                    pass
                await self._wait(found, min(5.0, left()))
            try:
                out["page_title"] = (await page.title())[:120]
            except Exception:
                pass
        finally:
            await ctx.close()
        if not out["ok"] and not out["note"]:
            out["note"] = "no playlist request seen" + (
                f" ({blocked_private} request(s) to private addresses blocked)" if blocked_private else "")
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
