#!/usr/bin/env python3
"""
The optional browser sidecar (`browser/`, docs/browser-sidecar.md), seen from PVArr.

Some players build the playlist URL in JavaScript, so the probe's HTML walk
never sees it and the link stays "unresolved" (the probe marks exactly that
case `fixable: devtools_m3u8`: page fetched, no playlist in it). When
`PVARR_BROWSER_URL` is set, those links and only those are handed to the
sidecar, which loads the page in Chromium and reports the playlist the page
requested plus the headers it used. PVArr then checks that playlist with its
own probe, exactly as it checks any other link: nothing the sidecar says is
trusted without a real fetch.

Unset, nothing here runs. Every failure (sidecar down, busy, slow, a bad
answer) comes back as "nothing found" and never raises: the sidecar is a
best-effort extra, never something a recording depends on. Each call runs on
an abandoned-not-joined daemon thread, so a Stop or a container shutdown never
waits for a page that is still loading.
"""

import json
import logging
import math
import os
import re
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlsplit

import requests

from app import probe
from app.sessions import config_dir

logger = logging.getLogger("PVArrBrowser")

# What the sidecar may spend on one page. The sidecar clamps to 5-60 s itself;
# on live links resolved pages took 7-10 s and misses ran to ~33 s.
DEFAULT_TIMEOUT_SEC = 30
TIMEOUT_BOUNDS = (10, 60)
# The sidecar answers within its timeout plus its own 15 s grace.
CLIENT_GRACE_SEC = 20
CONNECT_TIMEOUT_SEC = 3
# The answer is a small JSON object; anything bigger is not one.
MAX_ANSWER_BYTES = 64 * 1024
FIELD_LIMITS = {"m3u8_url": 4096, "referer": 2048, "cookie": 8192, "user_agent": 512,
                "page_title": 200}
# A page the browser found nothing on is not asked again for this long: every
# failover lap would otherwise pay the full timeout again for the same answer.
MISS_MEMO_SEC = 300
# After this many calls in a row that could not reach the sidecar at all, it
# is left alone for DOWN_PAUSE_SEC (one log line, not one per connect).
DOWN_AFTER = 3
DOWN_PAUSE_SEC = 300

# What the browser found before, per link host (`browser-hosts.json` in the
# config dir): the pre-game look asks the likeliest pages first. Learned from
# this installation's own results only; nothing is shipped with PVArr.
HOSTS_SCHEMA = 1
MAX_HOSTS = 300
MAX_HOST_LEN = 253
# This many misses in a row since the last hit put a host at the back.
MISS_STREAK_LAST = 2
MISS_STREAK_FORGET_OK = 3

_lock = threading.Lock()
_misses: Dict[str, float] = {}
_unreachable = 0
_down_until = 0.0
_CTL = re.compile(r"[\x00-\x1f\x7f]")
_HOST_OK = re.compile(r"^[a-z0-9._:-]+$")


def browser_url() -> str:
    """The sidecar's base URL, or "" when it is not configured."""
    raw = (os.environ.get("PVARR_BROWSER_URL") or "").strip().rstrip("/")
    if raw and not raw.lower().startswith(("http://", "https://")):
        logger.warning("Ignoring PVARR_BROWSER_URL: it must start with http:// or https://")
        return ""
    return raw


def browser_timeout() -> int:
    """Seconds the sidecar may spend on one page (PVARR_BROWSER_TIMEOUT)."""
    raw = os.environ.get("PVARR_BROWSER_TIMEOUT")
    try:
        value = int(float(raw)) if raw not in (None, "") else DEFAULT_TIMEOUT_SEC
    except (ValueError, OverflowError):
        logger.warning("Ignoring invalid PVARR_BROWSER_TIMEOUT=%r", raw)
        value = DEFAULT_TIMEOUT_SEC
    low, high = TIMEOUT_BOUNDS
    return max(low, min(high, value))


def worth_trying(probe_result: Dict[str, Any]) -> bool:
    """True for the JavaScript-player case only: the probe fetched the page and
    found no playlist in it. A dead playlist, a 404, a refused segment or a
    DNS-blocked host never goes to the browser, so failover is not slowed."""
    return (bool(browser_url())
            and not probe_result.get("ok")
            and probe_result.get("fixable") == "devtools_m3u8")


def _host(page_url: str) -> str:
    try:
        host = (urlsplit(page_url).hostname or "").rstrip(".").lower()
    except ValueError:
        return ""
    return host if 0 < len(host) <= MAX_HOST_LEN and _HOST_OK.match(host) else ""


class HostMemo:
    """Per link host: how often the browser found a playlist there (`ok`), the
    misses in a row since (`streak`), and when it last did (`last_ok`). One
    small JSON file, written whole and atomically; a missing, unreadable or
    unknown file means "nothing learned yet" and never an error. An unwritable
    config dir keeps the memory for this run only."""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (config_dir() / "browser-hosts.json")
        self.hosts: Dict[str, Dict[str, float]] = {}
        self._lock = threading.Lock()
        self._warned = False
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            hosts = data.get("hosts") if isinstance(data, dict) and data.get("schema") == HOSTS_SCHEMA else None
            for host, entry in list((hosts or {}).items())[:MAX_HOSTS]:
                if not (isinstance(host, str) and 0 < len(host) <= MAX_HOST_LEN and _HOST_OK.match(host)
                        and isinstance(entry, dict)):
                    continue
                values = {k: entry.get(k) or 0 for k in ("ok", "streak", "last_ok", "seen")}
                if all(isinstance(v, (int, float)) and not isinstance(v, bool)
                       and math.isfinite(v) and v >= 0 for v in values.values()):
                    self.hosts[host] = {k: float(v) for k, v in values.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, AttributeError, OverflowError, RecursionError) as exc:
            logger.warning("Ignoring unreadable browser host memory %s: %s", self.path, exc)
            self.hosts = {}

    def record(self, page_url: str, found: bool) -> None:
        host = _host(page_url)
        if not host:
            return
        now = time.time()
        with self._lock:
            entry = self.hosts.setdefault(host, {"ok": 0.0, "streak": 0.0, "last_ok": 0.0, "seen": 0.0})
            entry["seen"] = now
            if found:
                entry.update(ok=entry["ok"] + 1, streak=0.0, last_ok=now)
            else:
                entry["streak"] += 1
            if len(self.hosts) > MAX_HOSTS:     # forget the hosts not met for longest
                for old in sorted(self.hosts, key=lambda h: self.hosts[h]["seen"])[:len(self.hosts) - MAX_HOSTS]:
                    del self.hosts[old]
            payload = {"schema": HOSTS_SCHEMA, "hosts": self.hosts}
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tmp-browser-", suffix=".json")
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as fh:
                        json.dump(payload, fh)
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, self.path)
                except BaseException:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
                    raise
            except (OSError, TypeError, ValueError) as exc:
                if not self._warned:
                    self._warned = True
                    logger.warning("The browser's host memory will not survive a restart (%s). "
                                   "Fix ownership of the config mount.", exc)

    def rank(self, page_url: str) -> Tuple[int, float]:
        """Sort key, smallest first: hosts the browser resolved before (most
        recent first), then hosts not met yet, then hosts that keep missing."""
        with self._lock:
            entry = self.hosts.get(_host(page_url))
            if not entry:
                return (1, 0.0)
            if entry["ok"] > 0 and entry["streak"] < MISS_STREAK_FORGET_OK:
                return (0, -entry["last_ok"])
            return (2, 0.0) if entry["streak"] >= MISS_STREAK_LAST else (1, 0.0)


_memo: Optional[HostMemo] = None


def host_memo() -> HostMemo:
    global _memo
    with _lock:
        if _memo is None:
            _memo = HostMemo()
        return _memo


def host_rank(page_url: str) -> Tuple[int, float]:
    """Where this page goes in the browser's queue (see HostMemo.rank)."""
    try:
        return host_memo().rank(page_url)
    except Exception:       # ordering is a nicety; it must never sink a scan
        return (1, 0.0)


def _remember(page_url: str, found: bool) -> None:
    try:
        host_memo().record(page_url, found)
    except Exception:
        logger.debug("Could not record the browser's result for a host", exc_info=True)


def _clean_answer(answer: Any) -> Dict[str, Any]:
    """Only the fields PVArr uses, as bounded single-line strings; a value
    that could not go into a header safely is dropped, not passed on."""
    if not isinstance(answer, dict):
        return {"ok": False, "note": "the browser sidecar sent a malformed answer"}
    out: Dict[str, Any] = {"ok": answer.get("ok") is True}
    for key, limit in FIELD_LIMITS.items():
        value = answer.get(key)
        out[key] = value if isinstance(value, str) and len(value) <= limit and not _CTL.search(value) else ""
    note = answer.get("note")
    out["note"] = _CTL.sub(" ", note)[:200] if isinstance(note, str) else ""
    return out


def _ask_sidecar(base: str, page_url: str, timeout: int) -> Dict[str, Any]:
    """POST /resolve. The sidecar's cleaned answer, or {"ok": False, "note": why,
    "unreachable": bool}. Only the URL and a timeout are sent: never the
    operator's cookie or User-Agent, which the page's JavaScript would see."""
    try:
        with requests.Session() as session:
            session.trust_env = False   # no proxy settings may reroute this
            with session.post(f"{base}/resolve", json={"url": page_url, "timeout": timeout},
                              timeout=(CONNECT_TIMEOUT_SEC, timeout + CLIENT_GRACE_SEC),
                              allow_redirects=False, stream=True) as resp:
                if resp.status_code == 503:
                    return {"ok": False, "note": "the browser sidecar is busy"}
                if resp.status_code == 400:
                    # A refused page URL; the reason (a host the sidecar's DNS
                    # blocks, a private address) is the useful part.
                    try:
                        detail = json.loads(resp.raw.read(MAX_ANSWER_BYTES, decode_content=True)).get("detail")
                    except Exception:
                        detail = None
                    why = _CTL.sub(" ", detail)[:150] if isinstance(detail, str) and detail else "no reason given"
                    return {"ok": False, "note": f"the browser sidecar refused the page ({why})"}
                if resp.status_code != 200:
                    return {"ok": False, "note": f"the browser sidecar answered HTTP {resp.status_code}"}
                body = resp.raw.read(MAX_ANSWER_BYTES + 1, decode_content=True)
        if len(body) > MAX_ANSWER_BYTES:
            return {"ok": False, "note": "the browser sidecar's answer was too large"}
        return _clean_answer(json.loads(body))
    except requests.Timeout:
        return {"ok": False, "note": "the browser sidecar did not answer in time"}
    except (requests.ConnectionError, OSError) as exc:
        return {"ok": False, "unreachable": True,
                "note": f"the browser sidecar could not be reached ({type(exc).__name__})"}
    except Exception as exc:
        return {"ok": False, "note": f"the browser sidecar sent a malformed answer ({type(exc).__name__})"}


def _ask_abandonably(base: str, page_url: str, timeout: int,
                     abort: Optional[threading.Event], deadline: Optional[float]) -> Dict[str, Any]:
    """_ask_sidecar on a daemon thread, given up on (not joined) at `abort`
    or `deadline` (time.monotonic()); the thread then ends on its own timeouts."""
    box: Dict[str, Any] = {}
    worker = threading.Thread(target=lambda: box.update(_ask_sidecar(base, page_url, timeout)),
                              name="pvarr-browser", daemon=True)
    worker.start()
    while worker.is_alive():
        if abort is not None and abort.is_set():
            return {"ok": False, "note": "stopped"}
        if deadline is not None and time.monotonic() >= deadline:
            return {"ok": False, "note": "out of time"}
        worker.join(0.25)
    return box or {"ok": False, "note": "no answer"}


def resolve(page_url: str, timeout: Optional[int] = None,
            abort: Optional[threading.Event] = None, deadline: Optional[float] = None,
            probe_timeout: int = probe.DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """Ask the sidecar for the page's playlist, then verify it with the probe.

    Returns a probe-shaped result (`probe.probe_stream`) with `via: "browser"`
    and `input_url` the page; `ok` only when the probe confirmed the playlist.
    `timeout` caps the sidecar's time on the page (default PVARR_BROWSER_TIMEOUT).
    """
    global _unreachable, _down_until
    out: Dict[str, Any] = {"ok": False, "input_url": page_url, "m3u8_url": "", "via": "browser",
                           "referer": "", "cookie": "", "message": ""}
    base = browser_url()
    if not base:
        out["message"] = "The browser sidecar is not configured."
        return out
    now = time.monotonic()
    with _lock:
        for url in [u for u, t in _misses.items() if t <= now]:
            del _misses[url]
        if _misses.get(page_url, 0) > now:
            out["message"] = "Browser: found no playlist on this page a few minutes ago; not asked again yet."
            return out
        if _down_until > now:
            out["message"] = "Browser: the sidecar was unreachable; paused for a few minutes."
            return out
    started = time.monotonic()
    answer = _ask_abandonably(base, page_url, min(timeout or browser_timeout(), browser_timeout()),
                              abort, deadline)
    took = time.monotonic() - started
    with _lock:
        if answer.get("unreachable"):
            _unreachable += 1
            if _unreachable >= DOWN_AFTER:
                _down_until = time.monotonic() + DOWN_PAUSE_SEC
                logger.warning("Browser sidecar unreachable %d times in a row; not asking it for %d s.",
                               _unreachable, DOWN_PAUSE_SEC)
        elif answer.get("note") not in ("stopped", "out of time"):
            _unreachable = 0        # only a real answer ends an unreachable streak
    m3u8 = answer.get("m3u8_url") or ""
    if not answer.get("ok") or not m3u8:
        if answer.get("note") not in ("stopped", "out of time") and not answer.get("unreachable") \
                and "busy" not in (answer.get("note") or ""):
            with _lock:
                _misses[page_url] = time.monotonic() + MISS_MEMO_SEC
            # Only a page the browser really looked at counts against its
            # host: a refused page (HTTP 400, e.g. blocked by DNS) or an error
            # answer says nothing about what the browser would have found.
            if (answer.get("note") or "").startswith("no playlist request seen"):
                _remember(page_url, False)
        out["message"] = f"Browser ({took:.0f} s): " + (answer.get("note") or "no playlist found") + "."
        return out
    _remember(page_url, True)
    # The page's JavaScript chose this URL. A public page may not lead PVArr
    # into the LAN (the find_streams rule), checked before the probe fetches it.
    try:
        m3u8 = probe.clean_url(m3u8)
    except probe.ProbeError:
        out["message"] = "Browser: the page's playlist URL is not a web address; not used."
        return out
    if probe.is_private_url(m3u8) and not probe.is_private_url(page_url):
        out["message"] = "Browser: the page's playlist is on a private address; not used."
        return out
    referer = answer.get("referer") or ""
    try:
        result = probe.probe_stream(
            m3u8, referer=referer or None, user_agent=answer.get("user_agent") or None,
            cookie=answer.get("cookie") or None, timeout=probe_timeout, use_ytdlp=False)
    except Exception as exc:  # the probe must never sink a scan or a connect
        out["message"] = f"Browser found a playlist, but probing it failed: {exc}"
        return out
    if result.get("m3u8_url") and probe.is_private_url(result["m3u8_url"]) \
            and not probe.is_private_url(page_url):
        out["message"] = "Browser: the page's playlist led to a private address; not used."
        return out
    result["via"] = "browser"
    result["input_url"] = page_url
    # The probe saw only the playlist; the title is the page's, as the browser
    # rendered it (for the team-name check).
    result["page_title"] = answer.get("page_title") or ""
    if not result.get("referer") and referer:
        result["referer"] = referer
    result["message"] = f"Browser ({took:.0f} s): " + (result.get("message") or "")
    return result


def _reset_for_tests() -> None:
    global _unreachable, _down_until, _memo
    with _lock:
        _misses.clear()
        _unreachable = 0
        _down_until = 0.0
        _memo = None
