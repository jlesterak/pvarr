"""
Address rules for the browser sidecar - PVArr

The sidecar opens pages it is told to, so it must not become a way into the
operator's LAN. The same rule as PVArr's `app/probe.py:is_private_url`, copied
because this image does not contain PVArr:

- `page_url_problem`: the page URL must be http(s) and its host must resolve to
  public addresses only (full DNS check, once per request).
- `request_is_private`: every request and WebSocket the browser opens is
  checked for a literal private/loopback/link-local IP or a `localhost` name,
  and refused. No DNS here (a page makes hundreds of requests). This is a
  first line only: a public name that resolves to a private address, or a
  redirect to one, is not seen here, which is why the docs make network
  isolation of the container a requirement.

`PVARR_BROWSER_ALLOW_LOOPBACK=1` lets loopback (127.0.0.0/8, ::1, localhost)
through and nothing else. It exists for the test suite, whose fixture pages
are served on 127.0.0.1; LAN addresses stay blocked even then.
"""

import asyncio
import ipaddress
import os
import socket
from typing import Dict, Optional
from urllib.parse import urlsplit


def allow_loopback() -> bool:
    return os.environ.get("PVARR_BROWSER_ALLOW_LOOPBACK", "") == "1"


_NAT64 = ipaddress.ip_network("64:ff9b::/96")


def _private_ip(addr: "ipaddress._BaseAddress") -> bool:
    # A NAT64 address carries an IPv4 one in its last 32 bits; judge that.
    if isinstance(addr, ipaddress.IPv6Address) and addr in _NAT64:
        addr = ipaddress.IPv4Address(int(addr) & 0xFFFFFFFF)
    if addr.is_loopback and allow_loopback():
        return False
    return not addr.is_global or addr.is_multicast


def _localhost(host: str) -> bool:
    host = host.rstrip(".").lower()     # "localhost." is loopback too
    return (host == "localhost" or host.endswith(".localhost")) and not allow_loopback()


def page_url_problem(url: str) -> Optional[str]:
    """Why this page URL is refused, or None if it may be opened."""
    if not isinstance(url, str) or len(url) > 4096:
        return "URL missing or too long"
    if any(c.isspace() or not c.isprintable() for c in url):
        return "URL contains spaces or control characters"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return "only http(s) URLs with a host"
    host = parts.hostname.rstrip(".")
    if _localhost(host):
        return "private address"
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except (OSError, UnicodeError):
            return None     # does not resolve: the page load will simply fail
        addresses = [ipaddress.ip_address(i[4][0].split("%")[0]) for i in infos]
    if any(_private_ip(a) for a in addresses):
        return "private address"
    return None


def request_is_private(url: str) -> bool:
    """True for a browser request to a literal private IP or a localhost name."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return False        # data:, blob: and the like never leave the browser
    host = (parts.hostname or "").rstrip(".")
    if _localhost(host):
        return True
    try:
        return _private_ip(ipaddress.ip_address(host))
    except ValueError:
        return False


async def request_is_private_resolved(url: str, cache: Dict[str, bool]) -> bool:
    """request_is_private, plus host names: resolved (once per page, via
    `cache`) and refused when any address is private. Without this a page
    could reach a service by its Docker name -- PVArr itself shares a network
    with the sidecar, and the holder's blackhole routes do not cover that
    network. A name that does not resolve is let through: the request fails."""
    if request_is_private(url):
        return True
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https", "ws", "wss"):
        return False
    host = (parts.hostname or "").rstrip(".").lower()
    if not host:
        return False
    try:
        ipaddress.ip_address(host)
        return False            # a literal public IP: request_is_private passed it
    except ValueError:
        pass
    if host not in cache:
        try:
            infos = await asyncio.wait_for(
                asyncio.get_running_loop().getaddrinfo(host, None), 3)
            cache[host] = any(_private_ip(ipaddress.ip_address(i[4][0].split("%")[0]))
                              for i in infos)
        except (OSError, UnicodeError, asyncio.TimeoutError, ValueError):
            cache[host] = False
    return cache[host]
