#!/usr/bin/env python3
"""
PVArr one-shot schedules: "at 7pm, find streams on this page and record until 10".

A schedule is a recording that has not started yet. It holds an aggregator
page (and/or up to three stream links pasted by hand), the naming fields, and
a window. When the window opens, the loop in `server.py` checks the aggregator
page, takes the best three working streams (`quality.py`), fills any spare slots with the
hand-pasted links, and starts an ordinary recording through the same code path
as the Start button, ending at the window's end.

Why the page is checked at start time and not when scheduling: stream links
carry tokens that expire within hours, and most of the links on an event page
do not exist until shortly before kick-off. The page URL is the stable thing.

The one fragile input is a browser cookie (e.g. Cloudflare's `cf_clearance`)
pasted for the event page. It can expire between scheduling and start; the loop then
reports the challenge, keeps retrying every couple of minutes until the window
closes, and notifies. FlareSolverr (`PVARR_FLARESOLVERR_URL`) is the fix that
does not depend on a cookie's lifetime.

One-shot only: there is no recurrence. Persistence mirrors `sessions.py` --
one small JSON file under the config directory, written atomically on state
changes only, 0600 because it may hold that cookie, best-effort: an unwritable
config dir means schedules do not survive a restart, never that the server
refuses to boot.

Everything in here is pure or file-local; the loop, the HTTP endpoints and the
recorder launch live in `server.py`.
"""

import json
import logging
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import urlsplit

from app import quality
from app.sessions import config_dir

logger = logging.getLogger("PVArrSchedules")

SCHEMA_VERSION = 1

TICK_SEC = 15.0
# Between event-page checks once a window is open and nothing was found yet.
# Each check probes up to 20 links for up to a minute, so this is a duty cycle
# of roughly a third, not a hammer -- and after RETRY_FAST_ATTEMPTS misses the
# page is plainly not ready, so it backs off to RETRY_SLOW_SEC.
RETRY_SEC = 120.0
RETRY_SLOW_SEC = 300.0
RETRY_FAST_ATTEMPTS = 5
# Live (waiting/searching) jobs accepted at once. Each one can hold a cookie
# on disk and costs a scan per retry; nobody schedules twenty games ahead.
MAX_LIVE_JOBS = 20
# A start time typed "now" in the browser reaches the server a moment later.
PAST_GRACE_SEC = 60.0
MAX_AHEAD_SEC = 30 * 24 * 3600.0
# Same ceiling as /api/recordings/start's duration_minutes.
MAX_WINDOW_SEC = 24 * 3600.0
# Finished jobs stay visible this long so the operator can see what happened.
KEEP_FINISHED_SEC = 24 * 3600.0
MAX_CANDIDATES = 3

WAITING, SEARCHING, STARTED, FAILED, MISSED = "waiting", "searching", "started", "failed", "missed"
FINISHED_STATES = (STARTED, FAILED, MISSED)

# The disk warning. Nothing is known about a stream before it is found, so a
# scheduled recording is costed at a typical 1080p sports rate (2.7 GB an
# hour); 720p feeds run nearer 4 Mbit/s, so this errs towards warning.
ASSUMED_MBPS = 6.0
# One notification per job, from this long before its start: late enough that
# the free space is the space the game will get, early enough to act on.
DISK_NOTIFY_LEAD_SEC = 20 * 60.0
_GB = 1024 ** 3

# Never returned by GET /api/schedules. A cf_clearance cookie is a live
# credential and port 8999 has no authentication.
_SECRET_FIELDS = ("agg_cookie", "stream_headers")


def new_job(**fields: Any) -> Dict[str, Any]:
    """A fresh job in the `waiting` state. Fields are trusted (validated by the caller)."""
    now = time.time()
    job = {
        "id": uuid.uuid4().hex[:8],
        "created_at": now,
        "state": WAITING,
        "message": "Waiting for the start time.",
        "attempts": 0,
        "next_try_at": None,
        "recording_id": None,
        "finished_at": None,
        "notified_miss": False,
    }
    job.update(fields)
    return job


def retry_delay(attempts: int) -> float:
    """Seconds until the next check after `attempts` misses."""
    return RETRY_SEC if attempts <= RETRY_FAST_ATTEMPTS else RETRY_SLOW_SEC


def live_count(jobs: Iterable[Dict[str, Any]]) -> int:
    return sum(1 for j in jobs if j.get("state") in (WAITING, SEARCHING))


def recover(job: Dict[str, Any]) -> Dict[str, Any]:
    """A job left `searching` by a crash or shutdown is simply due again."""
    if job.get("state") == SEARCHING:
        job["state"] = WAITING
        job["next_try_at"] = None
    return job


def decide(job: Dict[str, Any], now: float) -> str:
    """What the loop should do with this job right now: `wait`, `search` or `missed`.

    A start time that passed while PVArr was down is not a miss: if the window
    is still open the job is due and starts late. Only the window closing is.
    """
    if job.get("state") != WAITING:
        return "wait"
    if now >= float(job["end_time"]):
        return "missed"
    due_at = max(float(job["start_at"]), float(job.get("next_try_at") or 0.0))
    return "search" if now >= due_at else "wait"


def expected_bytes(seconds: float, mbps: float = ASSUMED_MBPS) -> int:
    """What a recording of this length writes at this stream rate."""
    return int(max(0.0, float(seconds)) * max(0.0, float(mbps)) * 1e6 / 8)


def disk_warning(need_bytes: int, floor_bytes: int, free_bytes: int, where: str,
                 others: int = 0) -> Optional[str]:
    """One sentence when `need_bytes` plus the kept-free floor will not fit.

    `need_bytes` covers this job and the `others` that write to the same
    volume before it (earlier schedules, recordings already running). No URL
    and no team name: it goes into a notification as it is.
    """
    if need_bytes + floor_bytes <= free_bytes:
        return None
    what = "this recording" if others <= 0 else (
        f"this recording and the {others} before it" if others > 1
        else "this recording and the one before it")
    floor = f" plus the {floor_bytes / _GB:g} GB kept free" if floor_bytes > 0 else ""
    return (f"Low disk: {what} will need about {need_bytes / _GB:.1f} GB "
            f"(estimated at {ASSUMED_MBPS:g} Mbit/s){floor}, and {free_bytes / _GB:.1f} GB "
            f"is free on {where}. Free some space before the start; PVArr deletes nothing.")


def disk_notify_due(job: Dict[str, Any], now: float) -> bool:
    """Is this the moment to tell the operator about a job's disk warning?
    Once per job, from DISK_NOTIFY_LEAD_SEC before the start until it starts."""
    return (job.get("state") == WAITING and not job.get("disk_notified")
            and now >= float(job["start_at"]) - DISK_NOTIFY_LEAD_SEC)


def describe_picks(result: Optional[Dict[str, Any]]) -> str:
    """One log line on what a scan picked and why, e.g.
    '#1 a.example 720p · 1.8 Mbps · 14.5x real-time; #2 b.example ...
    [same source as #1] -- 1 source(s)'.

    A scan's measurements otherwise reach only the dashboard's Find result, so
    an unattended recording left no record of how its streams were ranked.
    Host only, never the full link: page paths can carry tokens.
    """
    parts, first_of = [], {}
    for n, pick in enumerate((result or {}).get("picked") or [], 1):
        host = urlsplit(str(pick.get("url") or "")).hostname or "?"
        q = pick.get("quality")
        text = f"#{n} {host} {quality.summary(q) if q else 'unranked'}".rstrip()
        if pick.get("via") == "browser":
            text += " (found by the browser sidecar)"
        # Same real feed as an earlier pick (quality.source_key): that backup
        # dies with it.
        key = quality.source_key(pick.get("m3u8_url") or "")
        if key and key in first_of:
            text += f" [same source as #{first_of[key]}]"
        elif key:
            first_of[key] = n
        parts.append(text)
    if not parts:
        return "no working streams"
    sources = (result or {}).get("sources")
    skipped = sum(1 for t in (result or {}).get("tried") or [] if t.get("note") == "not checked (time budget)")
    return ("; ".join(parts) + (f" -- {sources} source(s)" if sources else "")
            + (f" ({skipped} not checked: time budget)" if skipped else ""))


def merge_candidates(picks: Iterable[Dict[str, Any]], manual: Iterable[Optional[str]],
                     limit: int = MAX_CANDIDATES) -> List[str]:
    """Aggregator picks first (ranked order), then hand-pasted links, deduplicated.

    The pick's page URL is used, not its resolved m3u8: the recorder re-probes
    the page on every connect, which is what keeps a failover hours later from
    replaying a token that has since expired. The dashboard's Find button
    fills its slots the same way.
    """
    out: List[str] = []
    for url in [(p or {}).get("url") for p in picks] + list(manual):
        url = (url or "").strip()
        if url and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


RESCAN_MIN_INTERVAL_SEC = 600.0


def rescan_due(dead: int, total: int, last_at: float, now: float, busy: bool,
               blocked: bool = False) -> bool:
    """Should a running recording re-check its event page now?

    When two sources have died since the last check (or every source, for a
    session with fewer than two), at most once per RESCAN_MIN_INTERVAL_SEC,
    and never while a check for it is already running. A source blocked by
    this network's DNS is dead for good, so one (`blocked`) is enough.
    """
    if busy or total <= 0 or now - last_at < RESCAN_MIN_INTERVAL_SEC:
        return False
    return blocked or dead >= min(2, total)


def miss_reason(result: Optional[Dict[str, Any]]) -> str:
    """Why an event-page check produced nothing, in words fit for a notification.

    Composed here rather than forwarding the aggregator's own message, so no
    URL from the page can ride along into a third-party chat history.
    """
    if not result:
        return "No event page to check and no stream links were given."
    page = result.get("page") or {}
    if result.get("error"):
        return "The event-page check failed with an internal error (see the log)."
    if page.get("challenged") and page.get("flaresolverr_failed"):
        # A normal miss: FlareSolverr solves most event pages but not every
        # run, so the retry loop is exactly what this case needs.
        return ("The event page answered with a Cloudflare check that "
                "FlareSolverr did not solve this time (see the log).")
    if page.get("challenged"):
        return ("The event page answered with a Cloudflare check. A pasted "
                "cf_clearance cookie may have expired; FlareSolverr avoids this.")
    status = page.get("status")
    if not page or page.get("ok") is False or (status is not None and status != 200):
        return "The event page could not be loaded" + (f" (HTTP {status})." if status else ".")
    if not result.get("links_found"):
        return "The event page lists no stream links yet."
    tried = len(result.get("tried") or [])
    return f"None of the {tried} links checked gave a recordable stream yet."


def miss_kind(result: Optional[Dict[str, Any]]) -> str:
    """The category behind miss_reason(), for the dashboard's badge.

    `not_ready` (the page lists no links yet: normal before kick-off),
    `blocked` (a Cloudflare check), `page_error`, `no_working` (links found,
    none recordable), `error`, or `no_input`. Same branches in the same order
    as miss_reason(), so the badge and the sentence cannot disagree.
    """
    if not result:
        return "no_input"
    page = result.get("page") or {}
    if result.get("error"):
        return "error"
    if page.get("challenged"):
        return "blocked"
    status = page.get("status")
    if not page or page.get("ok") is False or (status is not None and status != 200):
        return "page_error"
    if not result.get("links_found"):
        return "not_ready"
    return "no_working"


# What to do by hand, per probe.fixable_kind(). Shown beside the link.
FIX_HINTS = {
    "devtools_m3u8": "Player builds its link in JavaScript: open it, copy the .m3u8 "
                     "from DevTools (Network, filter m3u8) and record that.",
    "headers": "Host wants a cookie or referer: copy them from DevTools into the "
               "link's advanced fields.",
    "token": "Its token was rejected: open the page for a fresh link.",
}
MAX_FIXABLE = 5


def fixable_links(result: Optional[Dict[str, Any]],
                  limit: int = MAX_FIXABLE) -> List[Dict[str, str]]:
    """Links from a page check that a manual step could get recording.

    Dashboard only (GET /api/schedules, same as agg_url): never put these in
    a notification, where a page's link would reach a third-party chat.
    """
    out: List[Dict[str, str]] = []
    for t in (result or {}).get("tried") or []:
        hint = FIX_HINTS.get(str(t.get("fixable") or ""))
        if hint and t.get("url"):
            out.append({"url": str(t["url"]), "hint": hint})
        if len(out) >= limit:
            break
    return out


def public_view(job: Dict[str, Any]) -> Dict[str, Any]:
    """A job as GET /api/schedules returns it: secrets replaced by flags."""
    view = {k: v for k, v in job.items() if k not in _SECRET_FIELDS}
    view["agg_cookie_set"] = bool(job.get("agg_cookie"))
    view["has_stream_headers"] = bool(job.get("stream_headers"))
    return view


def prune(jobs: Dict[str, Dict[str, Any]], now: float,
          keep_sec: float = KEEP_FINISHED_SEC) -> List[str]:
    """Drop finished jobs older than `keep_sec`. Returns the ids removed."""
    gone = [jid for jid, job in jobs.items()
            if job.get("state") in FINISHED_STATES
            and now - float(job.get("finished_at") or job.get("created_at") or 0) > keep_sec]
    for jid in gone:
        jobs.pop(jid, None)
    return gone


class ScheduleStore:
    """All jobs in memory, mirrored to one JSON file on every change.

    One file rather than one per job: there are a handful of jobs at most, and
    a single atomic replace cannot leave the set half-updated. Never raises at
    the caller; an unwritable directory disables persistence, loudly, once.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else (config_dir() / "schedules.json")
        self.jobs: Dict[str, Dict[str, Any]] = {}
        self.enabled = True
        self._warned = False
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._disable(exc)
        self._load()

    def _disable(self, exc: Exception) -> None:
        self.enabled = False
        if not self._warned:
            logger.warning("Schedule persistence disabled (%s); schedules will not "
                           "survive a restart. Fix ownership of the config mount.", exc)
            self._warned = True
            # A file that can no longer be updated still says what it said
            # last: a job since started would be loaded as `waiting` at the
            # next boot and recorded a second time. Better to lose the list.
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass

    def _load(self) -> None:
        if not self.enabled or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.warning("Ignoring unreadable schedule file %s: %s", self.path, exc)
            return
        if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
            logger.warning("Ignoring schedule file %s: unknown schema.", self.path)
            return
        for job in data.get("jobs") or []:
            if isinstance(job, dict) and job.get("id") and job.get("start_at") \
                    and job.get("end_time"):
                self.jobs[str(job["id"])] = recover(job)

    def save(self) -> bool:
        """Write every job atomically. A half-written file would lose them all."""
        if not self.enabled:
            return False
        try:
            payload = {"schema": SCHEMA_VERSION, "jobs": list(self.jobs.values())}
            fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".tmp-sched-",
                                       suffix=".json")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(payload, fh, indent=2)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
            return True
        except (OSError, TypeError, ValueError) as exc:
            self._disable(exc)
            return False

    def add(self, job: Dict[str, Any]) -> None:
        self.jobs[job["id"]] = job
        self.save()

    def get(self, job_id: str) -> Optional[Dict[str, Any]]:
        return self.jobs.get(job_id)

    def remove(self, job_id: str) -> bool:
        if self.jobs.pop(job_id, None) is None:
            return False
        self.save()
        return True

    def all(self) -> List[Dict[str, Any]]:
        return sorted(self.jobs.values(), key=lambda j: float(j.get("start_at") or 0))

