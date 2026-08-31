#!/usr/bin/env python3
"""
PVArr Session Persistence

Session state lives in an in-memory dict, so a `docker restart`, a Watchtower
update or a host reboot destroyed every in-flight recording: the FFmpeg child
died, the `.ts` survived on the volume but was orphaned -- no remux, no
notification, no library entry, and the Plex channel simply vanished. This
module writes one small JSON per session to `/config` so a restart can pick
them back up.

Three deliberate choices:

* **Written on state transitions only**, never on a timer. A recording that
  runs cleanly for three hours writes its file twice: once at start, once at
  the end. Ongoing disk writes are zero, which matters because the recordings
  volume is usually the system disk.
* **No progress counters are persisted.** Bytes written and elapsed time are
  recovered by `stat()`ing the `.ts` at resume. A counter in a file is a
  counter that disagrees with reality the moment the process dies, which is
  exactly when it gets read.
* **The recorder knows nothing about this.** It stays a pure capture engine;
  `server.py` owns the store and calls it at the transitions. Persistence that
  reaches into the capture loop is persistence that stalls the capture loop.

The files hold the stream URLs and any detected `Cookie` -- a resume against a
session-gated stream cannot work without them -- so they are written 0600 and
the directory 0700. `config/` is gitignored for the same reason.
"""

import json
import logging
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("PVArrSessions")

# Bump when the on-disk shape changes incompatibly. A record whose schema this
# code does not understand is left alone rather than guessed at.
SCHEMA_VERSION = 1

# How long a `.ts` may sit untouched before a resume is abandoned and the
# recording is finalised with what it already has. Measured from the file's
# mtime, NOT from the last state transition: under transitions-only writing a
# healthy three-hour recording's last transition is at t=0, so a gap measured
# from that would finalise exactly the long recordings this feature exists to
# save.
DEFAULT_MAX_RESUME_GAP_SEC = 300.0

# A session that dies, resumes, and dies again is not having bad luck -- it is
# reproducibly broken. Finalise rather than restart-loop forever.
DEFAULT_MAX_RESUME_ATTEMPTS = 3


def config_dir() -> Path:
    """Where session state is kept. `/config` in the container."""
    return Path(os.getenv("PVARR_CONFIG_DIR", "./config")).expanduser()


def max_resume_gap() -> float:
    try:
        return max(0.0, float(os.getenv("PVARR_MAX_RESUME_GAP", DEFAULT_MAX_RESUME_GAP_SEC)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESUME_GAP_SEC


def max_resume_attempts() -> int:
    try:
        return max(0, int(os.getenv("PVARR_MAX_RESUME_ATTEMPTS", DEFAULT_MAX_RESUME_ATTEMPTS)))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESUME_ATTEMPTS


class SessionStore:
    """One JSON file per live session, under `<config>/sessions/`.

    Every method is best-effort and never raises at the caller. Persistence
    failing is a degraded feature; it must not take a running recording down
    with it. When the directory is unwritable the store disables itself, says
    so once, and every call becomes a no-op.
    """

    def __init__(self, base_dir: Optional[Path] = None):
        self.dir = Path(base_dir) if base_dir else (config_dir() / "sessions")
        self.enabled = True
        self._warned = False
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self.dir, 0o700)
        except OSError as exc:
            self.enabled = False
            logger.warning(
                "Session persistence disabled: cannot use %s (%s). Recordings "
                "will not survive a restart. Fix ownership of the config mount "
                "-- see PUID/PGID in the README.", self.dir, exc,
            )

    # -- internals ---------------------------------------------------------

    def _path(self, recording_id: str) -> Path:
        # Session ids are generated internally (uuid4 hex), never caller-
        # supplied, but this is the one place a bad one would become a path.
        safe = "".join(c for c in str(recording_id) if c.isalnum() or c in "-_")
        if not safe:
            raise ValueError("Unusable recording id")
        return self.dir / f"{safe}.json"

    def _disable(self, exc: Exception) -> None:
        if not self._warned:
            logger.warning("Session persistence failing (%s); continuing without it.", exc)
            self._warned = True

    # -- public ------------------------------------------------------------

    def save(self, record: Dict[str, Any]) -> bool:
        """Write one session record atomically.

        Atomic because the most likely moment to be interrupted is a shutdown,
        which is precisely when this file is being written. A half-written
        JSON would be unparseable at the resume that follows.
        """
        if not self.enabled:
            return False
        try:
            path = self._path(record["id"])
            payload = dict(record, schema=SCHEMA_VERSION, updated_at=time.time())
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".tmp-", suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, KeyError, TypeError, ValueError) as exc:
            self._disable(exc)
            return False

    def load_all(self) -> List[Dict[str, Any]]:
        """Every readable session record. Unreadable ones are skipped, loudly."""
        if not self.enabled:
            return []
        records: List[Dict[str, Any]] = []
        try:
            files = sorted(self.dir.glob("*.json"))
        except OSError as exc:
            self._disable(exc)
            return []
        for path in files:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("Ignoring unreadable session file %s: %s", path.name, exc)
                continue
            if not isinstance(record, dict) or not record.get("id"):
                logger.warning("Ignoring malformed session file %s", path.name)
                continue
            if record.get("schema") != SCHEMA_VERSION:
                logger.warning(
                    "Ignoring session file %s: schema %s, this build understands %s.",
                    path.name, record.get("schema"), SCHEMA_VERSION,
                )
                continue
            records.append(record)
        return records

    def remove(self, recording_id: str) -> bool:
        """Forget a session. Called when it is genuinely finished."""
        if not self.enabled:
            return False
        try:
            self._path(recording_id).unlink(missing_ok=True)
            return True
        except (OSError, ValueError) as exc:
            self._disable(exc)
            return False


def build_record(
    recording_id: str,
    candidates: List[str],
    output_filepath: str,
    started_at: Optional[float],
    current_candidate_index: int = 0,
    header_overrides: Optional[Dict[str, Any]] = None,
    freeze_timeout_sec: int = 60,
    max_cycles: int = 3,
    min_free_gb: float = 5.0,
    resume_attempts: int = 0,
    rebroadcast: bool = False,
    channel_name: str = "",
    end_time: Optional[float] = None,
    max_hours: Optional[float] = None,
) -> Dict[str, Any]:
    """The shape written to disk. Everything needed to rebuild the recorder.

    Note what is absent: bytes_written, elapsed, status. Those are properties
    of a process that is no longer running, and are re-derived at resume from
    the file itself.
    """
    return {
        "id": recording_id,
        "candidates": list(candidates),
        "output_filepath": str(output_filepath),
        "started_at": started_at,
        "current_candidate_index": int(current_candidate_index),
        "header_overrides": header_overrides or {},
        "freeze_timeout_sec": int(freeze_timeout_sec),
        "max_cycles": int(max_cycles),
        "min_free_gb": float(min_free_gb),
        "resume_attempts": int(resume_attempts),
        "rebroadcast": bool(rebroadcast),
        "channel_name": str(channel_name or ""),
        # Absolute epoch, not a duration. A duration would restart its clock on
        # every resume, so a recording that crashed twice would run well past
        # the end the operator asked for.
        "end_time": float(end_time) if end_time else None,
        "max_hours": None if max_hours is None else float(max_hours),
    }


def resume_decision(
    record: Dict[str, Any],
    now: Optional[float] = None,
    gap_limit: Optional[float] = None,
    attempt_limit: Optional[int] = None,
) -> str:
    """Decide what to do with one persisted session at boot.

    Returns one of:
      "resume"   -- reattach and keep appending to the same file
      "finalise" -- the footage is worth keeping, but do not reconnect
      "discard"  -- there is nothing on disk to keep

    Kept as a pure function so the policy can be tested without a filesystem,
    a recorder or a running server.
    """
    now = time.time() if now is None else now
    gap_limit = max_resume_gap() if gap_limit is None else gap_limit
    attempt_limit = max_resume_attempts() if attempt_limit is None else attempt_limit

    if record.get("rebroadcast"):
        # A channel keeps nothing, so there is no footage to weigh up and no
        # gap that matters -- the buffer is deleted at shutdown by design.
        # Bring it back: the sponsor left a channel running and expects it to
        # be there. The attempt counter is deliberately not applied either;
        # a channel whose upstream is genuinely dead ends itself through
        # max_cycles and is removed that way, so there is no restart loop to
        # guard against.
        return "resume"

    path = Path(record.get("output_filepath", ""))
    try:
        stat = path.stat()
    except OSError:
        # The file is gone -- deleted by hand, or on a volume that did not
        # come back. Nothing to append to and nothing to post-process.
        return "discard"

    if stat.st_size <= 0:
        return "discard"

    if int(record.get("resume_attempts", 0)) >= attempt_limit:
        return "finalise"

    # An exact answer, where the gap heuristic below is a guess. If the window
    # has closed while we were down, the recording is simply over: reconnecting
    # would capture footage past the end that was asked for, and would then
    # have to stop almost immediately anyway.
    end_time = record.get("end_time")
    if end_time and now >= float(end_time):
        return "finalise"

    if (now - stat.st_mtime) > gap_limit:
        return "finalise"

    return "resume"
