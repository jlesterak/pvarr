"""
PNG-wrapped segments - PVArr

Some sources serve each HLS segment as a real PNG image. The video is in the
image's pixels: read as raw RGB(A) bytes, they hold a short marker, a 4-byte
big-endian length and a gzip member whose content is plain MPEG-TS. Nothing is
encrypted; it is gzip inside an image. FFmpeg cannot read it, so the relay
(`app/relay.py`) hands FFmpeg the unwrapped TS instead.

Recognised by shape only: a PNG whose pixel bytes start with a gzip member,
right after its length, that inflates to MPEG-TS. Standard library only; one
~2.5 MB segment takes about 0.2 s of CPU.
"""

import struct
import zlib
from typing import Optional

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
MAX_WRAPPED_BYTES = 16 * 1024 * 1024     # one segment as served
MAX_PIXEL_BYTES = 64 * 1024 * 1024       # inflated image data
MAX_TS_BYTES = 64 * 1024 * 1024          # inflated payload
# The marker before the payload is short; looking further would only invite
# false hits inside ordinary image data.
_HEADER_SEARCH = 64
_GZIP_MAGIC = b"\x1f\x8b\x08"
_CHANNELS = {2: 3, 6: 4}                 # PNG colour type -> bytes per pixel (8-bit)


def is_png(data: bytes) -> bool:
    return data[:8] == PNG_SIGNATURE


def png_pixels(data: bytes) -> Optional[bytes]:
    """The raw, unfiltered pixel bytes of an 8-bit RGB/RGBA, non-interlaced PNG.

    None for anything else (other formats, damaged or oversized files).
    """
    if not is_png(data) or len(data) > MAX_WRAPPED_BYTES:
        return None
    pos, idat, header = 8, [], None
    try:
        while pos + 8 <= len(data):
            length, kind = struct.unpack(">I4s", data[pos:pos + 8])
            chunk = data[pos + 8:pos + 8 + length]
            if len(chunk) < length:
                return None
            if kind == b"IHDR":
                header = struct.unpack(">IIBBBBB", chunk[:13])
            elif kind == b"IDAT":
                idat.append(chunk)
            elif kind == b"IEND":
                break
            pos += 12 + length
        if header is None or not idat:
            return None
        width, height, depth, colour, _, _, interlace = header
        bpp = _CHANNELS.get(colour)
        if depth != 8 or bpp is None or interlace or not width or not height:
            return None
        stride = width * bpp
        if (stride + 1) * height > MAX_PIXEL_BYTES:
            return None
        raw = zlib.decompressobj().decompress(b"".join(idat), (stride + 1) * height)
    except (struct.error, zlib.error):
        return None
    if len(raw) < (stride + 1) * height:
        return None
    out = bytearray()
    prev = bytearray(stride)
    for y in range(height):
        start = y * (stride + 1)
        kind = raw[start]
        line = bytearray(raw[start + 1:start + 1 + stride])
        if kind == 1:        # Sub
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif kind == 2:      # Up
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif kind == 3:      # Average
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif kind == 4:      # Paeth
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                line[i] = (line[i] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 0xFF
        elif kind != 0:
            return None
        out += line
        prev = line
    return bytes(out)


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
    try:
        ts = zlib.decompressobj(31).decompress(pixels[at:at + length], MAX_TS_BYTES)
    except zlib.error:
        return None
    # MPEG-TS: sync byte every 188 bytes, three packets seen.
    if len(ts) >= 377 and ts[0] == ts[188] == ts[376] == 0x47:
        return ts
    return None
