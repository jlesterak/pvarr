#!/usr/bin/env python3
"""
Library organisation: move a finished recording into a Plex-readable tree.

    <PVARR_LIBRARY_DIR>/<Sport>/Season <YYYY>/<Sport> - S<YYYY>E<NN> - <A> vs <B>.mp4

Off unless PVARR_LIBRARY_DIR is set. Capture and remux stay flat in the
recordings folder; this runs once, afterwards, and can only ever *move* a file
that already finished. If anything about it fails the recording stays where it
was and the caller reports why -- organising is a convenience and must never
be the reason footage is lost.

Why the shape is what it is (see TODO / the private design log):
- The episode number is found by scanning the season folder, never kept in a
  state file: a counter drifts silently the moment someone moves a file by hand.
- The library must sit on the same mount as the recordings. `rename(2)` fails
  with EXDEV across mount points even on one disk, so a separate `/library`
  bind would force a multi-GB copy with its own failure modes. We refuse with a
  clear message instead of copying.
- Names are built from an allowlist (`[A-Za-z0-9 _-]` after folding accents),
  so user text cannot add a path separator, a dot-file or a `..`.
"""

import errno
import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from app.naming import MAX_TOKEN_CHARS, RECORDING_EXTENSIONS, sanitize_token

logger = logging.getLogger("PVArrLibrary")

# One lock for "scan the season folder, pick the next number, move the file".
# Every completion runs on a recorder thread in this process, so a process-wide
# lock closes the race where two games finish together and both pick E07
# (O_EXCL would not: the two names differ by team, so both creates succeed).
_organise_lock = threading.Lock()

MAX_NAME_BYTES = 200          # whole filename, well inside the 255-byte limit
WALK_DEPTH = 4                # <root>/<Sport>/Season N/<file> plus one spare
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}   # a library may be shared over SMB
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})_")


class OrganiseError(Exception):
    """The recording could not be moved; it is still where it was."""


def library_root_setting() -> Optional[str]:
    raw = (os.environ.get("PVARR_LIBRARY_DIR") or "").strip()
    return raw or None


def resolve_library(recordings_dir: Path) -> Tuple[Optional[Path], str]:
    """The usable library root, or (None, why not). Never raises.

    The root is only *created* when its parent is inside the recordings
    folder (so it is certainly on the recordings mount). Anywhere else a
    missing directory means the operator forgot the bind mount, and creating it
    would put "organised" files in the container's writable layer, where they
    survive a restart and vanish on the next recreate.
    """
    raw = library_root_setting()
    if raw is None:
        return None, ""
    try:
        root = Path(raw).resolve()
        base = recordings_dir.resolve()
        if root == base or root in base.parents:
            return None, (f"PVARR_LIBRARY_DIR {raw} must be a folder inside the "
                          "recordings volume, not the recordings folder itself or a parent of it.")
        if not root.exists():
            if base in root.parents and root.parent.exists():
                root.mkdir(mode=0o775)
            else:
                return None, (f"PVARR_LIBRARY_DIR {raw} does not exist. Mount it, or "
                              "point it at a folder inside the recordings volume.")
        if not root.is_dir():
            return None, f"PVARR_LIBRARY_DIR {raw} is not a directory."
        if not os.access(root, os.W_OK | os.X_OK):
            return None, (f"PVARR_LIBRARY_DIR {raw} is not writable by this user; "
                          "fix its ownership (chown to PUID:PGID).")
        return root, ""
    except Exception as exc:    # includes RuntimeError("Symlink loop") from resolve()
        return None, f"PVARR_LIBRARY_DIR {raw} is unusable: {exc}"


def display_token(text: str, fallback: str) -> str:
    """A name for a folder or file: sanitised, spaced, length-capped.

    Reuses sanitize_token (accents folded, then an allowlist -- never a
    blocklist), turns its underscores back into spaces for Plex, and avoids
    Windows reserved device names.
    """
    token = sanitize_token(text, fallback).replace("_", " ").strip(" -") or fallback
    token = re.sub(r"\s+", " ", token)[:MAX_TOKEN_CHARS].strip(" -") or fallback
    if token.split(".")[0].upper() in _RESERVED:
        token += "_"
    return token


def _season_year(source: Path) -> str:
    """Calendar year of the recording, from the date PVArr put in its name."""
    m = _DATE_RE.match(source.name)
    if m:
        return m.group(1)
    return time.strftime("%Y", time.localtime(source.stat().st_mtime))


def next_episode(season_dir: Path, sport: str, year: str) -> int:
    """Highest S<year>E<NN> already in the folder, plus one.

    Scans names instead of keeping a counter, so a file moved or deleted by
    hand cannot make the next number wrong. The `.original` backups and
    temp files are ignored (dot-prefixed or not matching the pattern).
    """
    pattern = re.compile(rf"^{re.escape(sport)} - S{year}E(\d+) - ")
    highest = 0
    try:
        for entry in season_dir.iterdir():
            m = pattern.match(entry.name)
            if m:
                highest = max(highest, int(m.group(1)))
    except FileNotFoundError:
        pass
    return highest + 1


def organised_name(sport: str, team_a: str, team_b: str, year: str,
                   episode: int, ext: str) -> str:
    """`NFL - S2026E07 - Bears vs Packers.mp4`, at most MAX_NAME_BYTES long."""
    head = f"{sport} - S{year}E{episode:02d} - "
    tail_ext = ext if ext.startswith(".") else f".{ext}"
    teams = f"{team_a} vs {team_b}"
    room = MAX_NAME_BYTES - len((head + tail_ext).encode("utf-8"))
    while len(teams.encode("utf-8")) > room and len(teams) > 1:
        teams = teams[:-1]
    return f"{head}{teams.rstrip(' -')}{tail_ext}"


def episode_title(naming: Dict[str, Any]) -> Optional[str]:
    """`Bears vs Packers`, the title tag a finished recording carries, or None
    when the form named no teams (a tag of "Team A vs Team B" helps nobody)."""
    if not (naming.get("team_a") or naming.get("team_b")):
        return None
    return (f"{display_token(naming.get('team_a', ''), 'Team A')} vs "
            f"{display_token(naming.get('team_b', ''), 'Team B')}")


def organise(source: Path, naming: Dict[str, Any], root: Path) -> Path:
    """Move `source` into the library and return its new path.

    Raises OrganiseError, leaving `source` untouched, on any problem.
    """
    source = Path(source)
    if source.suffix.lower() not in (".mp4", ".mkv"):
        raise OrganiseError("only a remuxed .mp4/.mkv is organised")
    if not source.is_file() or source.is_symlink():
        raise OrganiseError("source is not a regular file")

    sport = display_token(naming.get("sport", ""), "Sports")
    team_a = display_token(naming.get("team_a", ""), "Team A")
    team_b = display_token(naming.get("team_b", ""), "Team B")
    year = _season_year(source)
    root = root.resolve()

    with _organise_lock:
        season_dir = root / sport / f"Season {year}"
        try:
            season_dir.mkdir(parents=True, exist_ok=True, mode=0o775)
            # A pre-existing symlinked sport/season folder would send the file
            # outside the library. Re-resolve after mkdir and insist it stayed in.
            resolved = season_dir.resolve()
            if root not in resolved.parents or resolved != season_dir:
                raise OrganiseError(f"{season_dir} escapes the library (symlink?)")
            episode = next_episode(season_dir, sport, year)
            dest = season_dir / organised_name(sport, team_a, team_b, year,
                                               episode, source.suffix.lower())
            if dest.exists():
                raise OrganiseError(f"{dest.name} already exists")
            os.replace(source, dest)
        except OrganiseError:
            raise
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                raise OrganiseError(
                    "the library is on a different mount from the recordings; "
                    "put PVARR_LIBRARY_DIR inside the recordings volume or mount "
                    "one parent folder for both") from exc
            raise OrganiseError(str(exc)) from exc
    logger.info("Organised %s -> %s", source.name, dest.relative_to(root))
    return dest


def walk(root: Path, depth: int = WALK_DEPTH) -> List[Path]:
    """Recording files under the library, never following symlinks.

    Dot-files and dot-folders are skipped, so a half-written temp file or a
    hidden backup never shows up as a recording.
    """
    found: List[Path] = []
    root = Path(root)
    base_depth = len(root.parts)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        if len(here.parts) - base_depth >= depth:
            dirnames[:] = []
        dirnames[:] = [d for d in dirnames
                       if not d.startswith(".") and not (here / d).is_symlink()]
        for name in filenames:
            path = here / name
            if (name.startswith(".") or path.is_symlink()
                    or path.suffix.lower() not in RECORDING_EXTENSIONS):
                continue
            found.append(path)
    return found
