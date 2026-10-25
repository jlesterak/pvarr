#!/usr/bin/env python3
"""Patch the pinned hls-proxy so it relays segments hosted off the playlist's domain.

Upstream hls-proxy only relays hosts it learned from a page scrape or from the
channel's own playlist URL, and answers 403 "Unknown upstream host" for anything
else. Live playlists often point at a separate CDN host, so every segment was
refused and the fallback could never rescue such a stream.

The patch registers the host of each segment URL found in a playlist the proxy
itself fetched (reusing the playlist host's Referer), except hosts that are
private, loopback or link-local IP literals.

It also passes a client's Range header upstream and relays the 206 and its
Content-Range. Upstream ignored Range and answered 200 with the whole file, so
playlists using EXT-X-BYTERANGE (segments packed at an offset inside one file)
gave ffmpeg the wrong bytes and every segment failed to open. Exits non-zero if the pinned source
no longer matches, so a bumped commit fails the image build instead of
silently shipping an unpatched proxy.

Usage: patch-hls-proxy.py /usr/local/bin/hls-proxy.py
"""
import sys

CALL_ANCHOR = "        # Mirror the upstream file extension onto the proxy path."
CALL = "        _learn_segment_host(full, base_url)\n"
DEF_ANCHOR = "def main():\n"
DEF = '''def _learn_segment_host(full, base_url):
    """PVArr patch: trust segment hosts named by a playlist we fetched."""
    import ipaddress
    host = re.match(r"https?://[^/]+", full)
    base = re.match(r"https?://[^/]+", base_url)
    if not host or host.group(0) in _referer_map:
        return
    name = urllib.parse.urlparse(full).hostname or ""
    try:
        ip = ipaddress.ip_address(name)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_unspecified:
            return
    except ValueError:
        pass
    _referer_map[host.group(0)] = _referer_map.get(base.group(0), "") if base else ""


'''


RANGE_ANCHOR = '            headers = {"User-Agent": UPSTREAM_UA}\n'
RANGE = (
    '            if self.headers.get("Range"):\n'
    '                headers["Range"] = self.headers["Range"]\n'
)
STATUS_ANCHOR = (
    "                self.send_response(200)\n"
    '                self.send_header("Content-Type", content_type)\n'
)
STATUS = (
    "                self.send_response(resp.status if resp.status == 206 else 200)\n"
    '                self.send_header("Content-Type", content_type)\n'
    '                if resp.headers.get("Content-Range"):\n'
    '                    self.send_header("Content-Range", resp.headers["Content-Range"])\n'
)


def patch(source: str) -> str:
    if "_learn_segment_host" in source:
        raise SystemExit("hls-proxy is already patched")
    for anchor in (CALL_ANCHOR, DEF_ANCHOR, RANGE_ANCHOR, STATUS_ANCHOR):
        if source.count(anchor) != 1:
            raise SystemExit("hls-proxy source changed: patch anchors not found")
    source = source.replace(CALL_ANCHOR, CALL + CALL_ANCHOR)
    source = source.replace(RANGE_ANCHOR, RANGE_ANCHOR + RANGE)
    source = source.replace(STATUS_ANCHOR, STATUS)
    return source.replace(DEF_ANCHOR, DEF + DEF_ANCHOR)


if __name__ == "__main__":
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        patched = patch(f.read())
    with open(path, "w", encoding="utf-8") as f:
        f.write(patched)
