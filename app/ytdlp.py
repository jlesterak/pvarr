#!/usr/bin/env python3
"""
PVArr yt-dlp Resolver

The built-in probe fetches a page and looks for an m3u8 in the HTML. That
covers a lot of sites and none of the ones that matter most: a modern player
fetches its manifest over XHR and hands it straight to hls.js, so the URL never
appears in the document at all. The sponsor confirmed this on their providers --
`document.documentElement.outerHTML.includes('m3u8')` came back false on every
one of them.

yt-dlp is the ecosystem's answer to exactly that. It carries site-specific
extractors for thousands of streaming sites, so for a site it knows it resolves
the manifest in one step without rendering anything, and it can impersonate a
real browser's TLS fingerprint -- which is what defeats the "identical 403 to
every client" wall that a plain HTTP library cannot get past.

Run as a subprocess rather than imported:

* It is isolated. yt-dlp can hang on a slow origin or die on a malformed page,
  and neither may touch the recorder thread. A subprocess takes a timeout; an
  in-process call does not.
* It is replaceable. Sites change constantly and yt-dlp ships every few weeks;
  an operator can drop a newer binary in without waiting on a PVArr release.
* The argv is an explicit list, never a shell string. Every URL here came from
  an unauthenticated caller.
"""

import json
import logging
import shutil
import subprocess
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PVArrYtdlp")

# This runs on the failover path, where a live recording is already off the
# air and waiting. Long enough for a site that redirects a few times, short
# enough that an unresolvable candidate does not become the reason a viewer
# sees a gap. The recorder has two more routes waiting behind this one.
DEFAULT_TIMEOUT_SEC = 20

# Formats we can actually record. yt-dlp reports the delivery protocol, which
# is more reliable than sniffing the URL for ".m3u8".
_HLS_PROTOCOLS = ("m3u8", "m3u8_native")


# Keyed by binary path: the answer cannot change while we run, and this shells
# out.
_IMPERSONATE_CACHE: Dict[str, bool] = {}


def ytdlp_path() -> Optional[str]:
    """Where yt-dlp is, or None. Absent is normal and never fatal."""
    return shutil.which("yt-dlp")


def impersonation_available(binary: str) -> bool:
    """Whether this build can actually impersonate a browser's TLS fingerprint.

    A capability check, not a flag-name check, and the distinction is not
    academic. On yt-dlp 2024.04.09 `--help` mentions impersonate three times,
    and `--impersonate chrome` exits with a Python traceback -- the option is
    recognised but every target needs `curl_cffi`, which is an optional extra.
    Passing it on such a build turns every resolution into a hard failure.

    `--list-impersonate-targets` is the honest answer: it prints one row per
    target and marks the unusable ones "(not available)".
    """
    if binary in _IMPERSONATE_CACHE:
        return _IMPERSONATE_CACHE[binary]

    usable = False
    try:
        result = subprocess.run(
            [binary, "--list-impersonate-targets"],
            capture_output=True, text=True, timeout=15,
        )
        for line in (result.stdout or "").splitlines():
            stripped = line.strip()
            if (not stripped or stripped.startswith("[")
                    or stripped.startswith("Client") or set(stripped) <= set("- ")):
                continue          # banner, column headings, rule
            if "not available" not in stripped:
                usable = True
                break
    except (OSError, subprocess.SubprocessError):
        usable = False

    _IMPERSONATE_CACHE[binary] = usable
    return usable


def _pick_format(info: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Choose the HLS format worth recording.

    Prefers a master playlist -- FFmpeg picks the variant itself and can switch
    down mid-recording, which is exactly the resilience we want on a stream
    that is already marginal. Falls back to the highest-bandwidth media
    playlist when no master is offered.
    """
    formats: List[Dict[str, Any]] = info.get("formats") or []
    if not formats and info.get("url"):
        # A single-format extraction reports at the top level instead.
        formats = [info]

    hls = [
        f for f in formats
        if f.get("url") and str(f.get("protocol", "")).startswith(_HLS_PROTOCOLS)
    ]
    if not hls:
        # Some extractors leave protocol unset; fall back to the URL shape.
        hls = [f for f in formats if ".m3u8" in str(f.get("url", "")).lower()]
    if not hls:
        return None

    masters = [f for f in hls if f.get("format_id") in (None, "", "hls") or f.get("is_master")]
    if masters:
        return masters[0]
    return max(hls, key=lambda f: f.get("tbr") or f.get("height") or 0)


def resolve(
    url: str,
    timeout: int = DEFAULT_TIMEOUT_SEC,
    binary: Optional[str] = None,
    impersonate: bool = True,
) -> Optional[Dict[str, str]]:
    """Resolve a page URL to a stream URL plus the headers it needs.

    Returns None whenever yt-dlp is missing, fails, times out, or finds nothing
    recordable -- the caller has other routes to try, and a resolver failure
    must never end a recording.

    `-J` rather than `-g` deliberately: `-g` prints only the URL, while the
    JSON carries `http_headers`, which is where the Referer and User-Agent the
    stream expects actually live. Getting the URL without them just moves the
    403 one step later.
    """
    path = binary or ytdlp_path()
    if not path or not url:
        return None

    cmd = [
        path,
        "-J",                      # dump the full info JSON, do not download
        "--no-warnings",
        "--no-playlist",
        "--no-progress",
        "--socket-timeout", "10",
        "--retries", "1",
    ]
    if impersonate and impersonation_available(path):
        # Defeats TLS-fingerprint blocking -- the wall that answers every plain
        # HTTP client with an identical 403. Gated on a real capability check:
        # a build without curl_cffi does not ignore this flag, it dies on it.
        cmd += ["--impersonate", "chrome"]
    cmd.append(url)

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        logger.info("yt-dlp timed out after %ss resolving a candidate", timeout)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        logger.info("yt-dlp could not run: %s", exc)
        return None

    if result.returncode != 0 or not (result.stdout or "").strip():
        return None

    try:
        info = json.loads(result.stdout)
    except (ValueError, TypeError):
        return None
    if not isinstance(info, dict):
        return None

    chosen = _pick_format(info)
    if not chosen:
        return None

    headers = {k.lower(): v for k, v in (chosen.get("http_headers") or {}).items()}
    return {
        "m3u8_url": chosen["url"],
        "referer": headers.get("referer", "") or "",
        "user_agent": headers.get("user-agent", "") or "",
        "cookie": headers.get("cookie", "") or "",
        "title": str(info.get("title") or ""),
        "is_live": bool(info.get("is_live")),
        "extractor": str(info.get("extractor_key") or info.get("extractor") or ""),
    }
