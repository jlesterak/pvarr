#!/usr/bin/env python3
"""
PVArr Bounded Stream Buffer

Rebroadcast serves a live stream to Plex/Emby/Jellyfin without keeping it. The
problem that shapes this module is fan-out: several clients must watch one
upstream pull, because re-pulling a session-gated sports feed once per viewer
is a good way to get the account throttled.

Recording solves fan-out for free -- the `.ts` on disk *is* the shared buffer,
and every client tails it independently. Rebroadcast cannot grow a file
forever, so this is that same idea with a ceiling: a fixed-size file written in
a circle.

**Why a file and not memory.** An in-memory hub was considered and rejected. At
10 Mbps a client that connects and stops reading accumulates ~75 MB a minute,
`docker-compose.yml` sets no memory limit, and the OOM killer takes the largest
process -- uvicorn, PID 1 -- which would kill every concurrent *recording* too.
On a 4 GB host that is roughly 27 minutes from one wedged Plex client to losing
the game you were recording. The same 75 MB as a file is page cache: the kernel
reclaims it under pressure instead of killing us, and serves it from RAM anyway.

**Why written in place and not rotated.** Log-style rotation truncates or
renames the file out from under a reader that is holding an open descriptor,
which hands it zero-fill in the middle of a transport stream. A fixed file
written in a circle never changes size, so a reader's descriptor stays valid
for the life of the channel.

**Packet alignment.** MPEG-TS is a stream of 188-byte packets and a decoder
that starts mid-packet produces garbage until it resynchronises. Capacity is
forced to a multiple of 188 and positions are derived from a monotonic absolute
offset, so `offset % 188` is preserved across every wrap: a reader that starts
on a packet boundary stays on one forever. FFmpeg's mpegts output begins at
packet zero, so every multiple of 188 is a boundary.

Positional `pread`/`pwrite` throughout -- they do not touch the shared file
offset, so one writer and many readers can share a single descriptor safely.
"""

import logging
import os
import threading
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger("PVArrRing")

# An MPEG-TS packet. Everything here is a multiple of it.
TS_PACKET_SIZE = 188

# Roughly 60 seconds at 10 Mbps, rounded to a whole number of TS packets. Deep
# enough that a client can join late or stall briefly without a gap, shallow
# enough that four channels cost ~300 MB of reclaimable page cache rather than
# unreclaimable heap.
DEFAULT_CAPACITY_BYTES = 75_000_000 // TS_PACKET_SIZE * TS_PACKET_SIZE


def default_capacity() -> int:
    """Ring size in bytes, from PVARR_BUFFER_MB, floored to whole packets."""
    raw = os.getenv("PVARR_BUFFER_MB")
    if raw is None:
        return DEFAULT_CAPACITY_BYTES
    try:
        megabytes = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_CAPACITY_BYTES
    # One second at 10 Mbps is about the least that can absorb a hiccup;
    # below that the ring is churning without buying anything.
    capacity = max(int(megabytes * 1024 * 1024), TS_PACKET_SIZE * 1000)
    return capacity // TS_PACKET_SIZE * TS_PACKET_SIZE


class RingBuffer:
    """A fixed-size file written in a circle, with many independent readers.

    One writer thread calls `write()`. Any number of reader threads call
    `read()` with the absolute offset they have consumed to. Offsets are
    absolute and monotonic -- they count every byte ever written and never
    wrap, even though positions in the file do.
    """

    def __init__(self, path, capacity: Optional[int] = None):
        self.path = Path(path)
        capacity = capacity if capacity is not None else default_capacity()
        # Whole packets, or alignment does not survive a wrap.
        self.capacity = max(TS_PACKET_SIZE, capacity // TS_PACKET_SIZE * TS_PACKET_SIZE)
        self._lock = threading.Lock()
        self._write_offset = 0
        self._closed = False

        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Create at full size up front. Readers pread at any position, and a
        # short file would return b"" and look like a stalled stream.
        self._fd = os.open(str(self.path), os.O_RDWR | os.O_CREAT, 0o600)
        os.ftruncate(self._fd, self.capacity)

    # -- properties --------------------------------------------------------

    @property
    def write_offset(self) -> int:
        """Total bytes ever written. Never resets."""
        return self._write_offset

    @property
    def closed(self) -> bool:
        return self._closed

    def oldest_offset(self) -> int:
        """The earliest absolute offset still held in the ring."""
        return max(0, self._write_offset - self.capacity)

    def live_offset(self) -> int:
        """Where a client joining *now* should start: the newest packet boundary.

        A late joiner wants the live edge, not a minute of history -- Plex is
        tuning a live channel, and replaying the buffer would put every viewer
        a minute behind and drifting further on every reconnect.
        """
        return self._write_offset - (self._write_offset % TS_PACKET_SIZE)

    # -- writing -----------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Append to the ring, overwriting the oldest bytes. Never blocks.

        A slow reader cannot stall the capture thread; it simply gets lapped
        and resynchronises. That is the whole point of the design: the upstream
        pull must keep pace with the stream regardless of what any client does.
        """
        if self._closed or not data:
            return 0

        # A chunk bigger than the ring can only leave its own tail behind.
        if len(data) > self.capacity:
            data = data[-self.capacity:]

        with self._lock:
            start = self._write_offset % self.capacity
            first = min(len(data), self.capacity - start)
            os.pwrite(self._fd, data[:first], start)
            if first < len(data):
                os.pwrite(self._fd, data[first:], 0)
            self._write_offset += len(data)
            return len(data)

    # -- reading -----------------------------------------------------------

    def read(self, offset: int, max_bytes: int = 65536) -> Tuple[bytes, int]:
        """Read from `offset`, returning the bytes and the next offset.

        Returns `(b"", offset)` when the reader is already current -- there is
        nothing new yet, which is not an error and not the end of the stream.

        A reader that has fallen further behind than the ring is deep has been
        lapped: its bytes are gone, overwritten by newer ones. Rather than
        serve it a torn stream it is skipped forward to the oldest data still
        held, realigned to a packet boundary. The client sees a discontinuity,
        which a player resynchronises through, instead of corruption.
        """
        if self._closed:
            return b"", offset

        write_offset = self._write_offset
        oldest = max(0, write_offset - self.capacity)

        if offset < oldest:
            # Lapped. Round the resync point UP to a packet boundary: rounding
            # down would point at bytes that have already been overwritten.
            offset = oldest + (-oldest % TS_PACKET_SIZE)

        available = write_offset - offset
        if available <= 0:
            return b"", offset

        want = min(max_bytes, available)
        start = offset % self.capacity
        first = min(want, self.capacity - start)
        chunk = os.pread(self._fd, first, start)
        if first < want:
            chunk += os.pread(self._fd, want - first, 0)

        # The writer may have lapped us *during* the read, in which case what
        # we just copied is a mix of old and new bytes. Detect it after the
        # fact and throw the read away rather than emit a torn stream.
        if self._write_offset - offset > self.capacity:
            resync = self.oldest_offset()
            return b"", resync + (-resync % TS_PACKET_SIZE)

        return chunk, offset + len(chunk)

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        """Close and delete the backing file. The buffer is not a recording."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            try:
                os.close(self._fd)
            except OSError:
                pass
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("Could not remove buffer %s: %s", self.path, exc)
