"""
PNG-wrapped segments - PVArr

Some sources serve each HLS segment as a real PNG image. The video is in the
image's pixels: read as raw RGB(A) bytes, they hold a short marker, a 4-byte
big-endian length and a gzip member whose content is plain MPEG-TS. Nothing is
encrypted; it is gzip inside an image. FFmpeg cannot read it, so the relay
(`app/relay.py`) hands FFmpeg the unwrapped TS instead.

Recognised by shape only: a PNG whose pixel bytes start with a gzip member,
right after its length, that inflates to MPEG-TS. The image is decoded by the
FFmpeg PVArr already ships (about 0.1 s per ~2.5 MB segment, outside Python's
GIL); a pure-Python decode cost 0.4-1.8 s depending on the encoder's filters.
"""

import struct
import subprocess
import zlib
from typing import Optional

from app.check_deps import find_executable

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_WRAPPED_BYTES = 16 * 1024 * 1024     # one segment as served
# Real segments are 3-6 MB. The payload is gzip, which does not compress
# again, so a genuine image inflates to about its own size and its payload to
# about the gzip's: a large ratio is a decompression bomb, refused before
# anything inflates it or a cache holds the result.
MAX_PIXEL_BYTES = 24 * 1024 * 1024       # inflated image data
MAX_TS_BYTES = 24 * 1024 * 1024          # inflated payload
MAX_PIXEL_RATIO = 4                      # inflated image data / IDAT bytes
MAX_TS_RATIO = 10                        # payload / gzip bytes
MAX_IDAT_CHUNKS = 10000
DECODE_TIMEOUT_SEC = 10
# The marker before the payload is short; looking further would only invite
# false hits inside ordinary image data.
_HEADER_SEARCH = 64
_GZIP_MAGIC = b"\x1f\x8b\x08"
_CHANNELS = {2: 3, 6: 4}                 # PNG colour type -> bytes per pixel (8-bit)


def is_png(data: bytes) -> bool:
    return data[:8] == PNG_SIGNATURE


def png_pixels(data: bytes) -> Optional[bytes]:
    """The raw pixel bytes of an 8-bit RGB/RGBA, non-interlaced PNG.

    None for anything else (other formats, damaged or oversized files). The
    header is checked here; FFmpeg does the decode (argv list, stdin/stdout,
    killed on timeout by `subprocess.run`, so no child outlives the call).
    """
    if not is_png(data) or len(data) > MAX_WRAPPED_BYTES:
        return None
    pos, idat_bytes, idat_chunks, header = 8, 0, 0, None
    try:
        while pos + 8 <= len(data):
            length, kind = struct.unpack(">I4s", data[pos:pos + 8])
            if pos + 12 + length > len(data):
                return None
            if kind == b"IHDR":
                header = struct.unpack(">IIBBBBB", data[pos + 8:pos + 21])
            elif kind == b"IDAT":
                idat_bytes += length
                idat_chunks += 1
                if idat_chunks > MAX_IDAT_CHUNKS:
                    return None
            elif kind == b"IEND":
                break
            pos += 12 + length
    except struct.error:
        return None
    if header is None or not idat_bytes:
        return None
    width, height, depth, colour, _, _, interlace = header
    bpp = _CHANNELS.get(colour)
    if depth != 8 or bpp is None or interlace or not width or not height:
        return None
    size = width * bpp * height
    if size > MAX_PIXEL_BYTES or size > MAX_PIXEL_RATIO * idat_bytes:
        return None
    cmd = [find_executable("ffmpeg") or "ffmpeg", "-nostdin", "-hide_banner", "-v", "error",
           "-f", "png_pipe", "-i", "pipe:0",
           "-f", "rawvideo", "-pix_fmt", "rgb24" if bpp == 3 else "rgba", "pipe:1"]
    try:
        res = subprocess.run(cmd, input=data, capture_output=True, timeout=DECODE_TIMEOUT_SEC)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or len(res.stdout) != size:
        return None
    return res.stdout


def unwrap(data: bytes) -> Optional[bytes]:
    """The MPEG-TS carried in a PNG-wrapped segment, or None if it is not one."""
    pixels = png_pixels(data)
    if pixels is None:
        return None
    at = pixels.find(_GZIP_MAGIC, 4, _HEADER_SEARCH)
    if at < 0:
        return None
    (length,) = struct.unpack(">I", pixels[at - 4:at])
    if not 0 < length <= len(pixels) - at:
        return None
    inflate = zlib.decompressobj(31)
    try:
        ts = inflate.decompress(pixels[at:at + length], min(MAX_TS_BYTES, MAX_TS_RATIO * length))
    except zlib.error:
        return None
    if not inflate.eof:     # cut off at the cap (or truncated): not a whole segment
        return None
    # MPEG-TS: sync byte every 188 bytes, three packets seen.
    if len(ts) >= 377 and ts[0] == ts[188] == ts[376] == 0x47:
        return ts
    return None
