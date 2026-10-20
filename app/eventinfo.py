#!/usr/bin/env python3
"""
Event details from an event or listing page: who is playing, which league,
and when.

Pure: it reads HTML that aggregator.fetch_page already fetched and never
touches the network. Only generic signals are read, no site's own markup:

  - schema.org event data (`application/ld+json` SportsEvent: teams, start);
  - an "A vs B" heading, description or title;
  - a start time in a machine-readable attribute (`data-timestamp` and
    similar Unix epochs, or `<time datetime>`);
  - a "LIVE" label once the game is on, and the short status after it;
  - on a listing page, each link titled or labelled with two teams.

Team names are matched against the offline snapshot (app/tags.py), so the
form gets the canonical spelling and the league; an unknown team keeps the
page's spelling. Anything not found is left empty and the operator types it,
as before.
"""

import json
import re
import time
from datetime import datetime
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin

from app import tags

# A default recording window by league, in hours: the usual game length plus
# a margin for overtime and late starts. Editable in the form.
DURATION_HOURS = {
    "NFL": 3.5, "NCAAF": 3.75, "CFL": 3.25,
    "NBA": 2.75, "WNBA": 2.5, "NCAAM": 2.5, "NCAAW": 2.5,
    "NHL": 3.0, "NCAAH": 3.0,
    "MLB": 3.5, "NCAABSB": 3.5,
}
SOCCER_HOURS = 2.25
FALLBACK_HOURS = 3.0
MAX_EVENTS = 200
# "A vs B" text longer than this is not a game title; the cap also keeps the
# lazy regex below from going quadratic on a hostile page.
MAX_VS_TEXT = 200

_EPOCH_ATTRS = {"data-timestamp", "data-time", "data-start", "data-start-time",
                "data-starttime", "data-unix", "data-epoch", "data-date"}
_EARLIEST, _LATEST = 1_577_836_800, 4_102_444_800   # 2020 .. 2100
_VS_RE = re.compile(r"^\s*(?:watch\s+)?(.+?)\s+(?:vs\.?|v\.?|@|-|–)\s+(.+?)\s*$", re.I)
_TAIL_RE = re.compile(r"\s+(?:live\b|stream\b|online\b|free\b).*$", re.I)
_SKIP_CHUNK = re.compile(r"^(?:live|\||vs\.?|v|@|-|\d+|streams?|watch.*)$", re.I)


def _epoch(value: str) -> Optional[float]:
    """A plausible event time from an attribute: Unix seconds or ms, or ISO 8601."""
    v = (value or "").strip()
    if v.isdigit():
        n = int(v) / (1000 if len(v) == 13 else 1)
        return float(n) if _EARLIEST <= n <= _LATEST else None
    try:
        n = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None
    # A bare date or a time without an offset is ambiguous; ignore it.
    return n if "T" in v and ("+" in v[10:] or v.endswith("Z") or "-" in v[10:]) \
        and _EARLIEST <= n <= _LATEST else None


class _Scan(HTMLParser):
    """One pass: page-level signals, plus every link with its text chunks.

    "Page-level" means outside any link: on an event page the time and LIVE
    label sit in the header, while links to other events carry their own.
    """

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.meta: Dict[str, str] = {}
        self.base = ""
        self.heading = ""
        self.page: Dict[str, Any] = {"chunks": []}
        self.links: List[Dict[str, Any]] = []
        self.ld: List[str] = []
        self._link: Optional[Dict[str, Any]] = None
        self._in: Optional[str] = None          # "title", "h1" or "ld"
        self._buf: List[str] = []
        self._skip = 0                          # inside <script>/<style>

    def _target(self) -> Dict[str, Any]:
        return self._link if self._link is not None else self.page

    def handle_starttag(self, tag, attrs):
        a = {k.lower(): (v or "") for k, v in attrs}
        if tag in ("script", "style"):
            if tag == "script" and "ld+json" in a.get("type", "").lower():
                self._in, self._buf = "ld", []
            else:
                self._skip += 1
            return
        if tag == "base" and a.get("href") and not self.base:
            self.base = a["href"].strip()
        elif tag == "meta":
            name = (a.get("property") or a.get("name") or "").lower()
            if name in ("og:title", "og:description", "description"):
                self.meta.setdefault(name, a.get("content", ""))
        elif tag in ("title", "h1") and not self._in:
            self._in, self._buf = tag, []
        elif tag == "a" and a.get("href") and self._link is None:
            self._link = {"href": a["href"].strip(), "title": a.get("title", ""), "chunks": []}
        stamps = [a[k] for k in _EPOCH_ATTRS if k in a]
        if tag == "time" and "datetime" in a:
            stamps.append(a["datetime"])
        for s in stamps:
            t = _epoch(s)
            if t:
                self._target().setdefault("ts", t)
                break

    def handle_data(self, data):
        if self._in:
            self._buf.append(data)
            if self._in == "ld":
                return
        if self._skip:
            return
        text = " ".join(data.split())
        if text:
            self._target()["chunks"].append(text)

    def handle_endtag(self, tag):
        if tag == "script" and self._in == "ld":
            self.ld.append("".join(self._buf))
            self._in = None
            return
        if tag in ("script", "style") and self._skip:
            self._skip -= 1
            return
        if self._in == tag:
            text = " ".join("".join(self._buf).split())
            if tag == "title":
                self.meta.setdefault("title", text)
            elif not self.heading:
                self.heading = text
            self._in = None
        if tag == "a" and self._link is not None:
            self.links.append(self._link)
            self._link = None


def _split_vs(text: str, backed: bool = False) -> Optional[Tuple[str, str]]:
    """("A", "B") from "A vs B" / "A - B" / "Watch A vs B live online", or None.

    A plain dash also separates site names from slogans ("Site - Your guide
    to..."), so "A - B" counts only when `backed` (a start time or LIVE label
    goes with it) or one side is a known team.
    """
    text = (text or "").split(" | ")[0]
    if len(text) > MAX_VS_TEXT:
        return None
    text = _TAIL_RE.sub("", text)
    m = _VS_RE.match(text)
    if not m:
        return None
    a, b = m.group(1).strip(), m.group(2).strip()
    if re.search(r"\s(?:vs\.?|v\.?|@)\s", text, re.I) or backed:
        return a, b
    if tags.find_team(a) or tags.find_team(_trim_to_team(b)[0]):
        return a, b
    return None


def _trim_to_team(name: str) -> Tuple[str, str]:
    """('Philadelphia Flyers', 'Hockey') from 'Philadelphia Flyers Hockey' when
    the shorter form is a known team; otherwise (name, '')."""
    words = name.split()
    for n in range(len(words), 0, -1):
        if tags.find_team(" ".join(words[:n])):
            return " ".join(words[:n]), " ".join(words[n:])
    return name, ""


def _live(chunks: List[str]) -> Tuple[bool, str]:
    """(is_live, status) from a "LIVE" chunk and the short text after it."""
    for i, c in enumerate(chunks):
        if c.upper() == "LIVE":
            # Only text set off by a separator ("LIVE | 2nd Period") is the
            # status; a bare LIVE is often followed by the next field.
            sep, nxt = (chunks[i + 1:i + 3] + ["", ""])[:2]
            if sep in ("|", "-", "•", "·") and nxt.upper() != "LIVE" and len(nxt) <= 40:
                return True, nxt
            return True, ""
    return False, ""


def _from_ld(blocks: List[str]) -> Optional[Dict[str, Any]]:
    """Teams and start from the first schema.org SportsEvent, if any."""
    for raw in blocks:
        try:
            data = json.loads(raw)
        except (ValueError, RecursionError):   # junk, or nested past the stack
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop(0)
            if not isinstance(item, dict):
                continue
            stack += item.get("@graph", []) if isinstance(item.get("@graph"), list) else []
            kind = item.get("@type")
            kinds = kind if isinstance(kind, list) else [kind]
            if not any(k in ("SportsEvent", "Event", "BroadcastEvent") for k in kinds):
                continue
            names = []
            for key in ("homeTeam", "awayTeam"):
                team = item.get(key)
                if isinstance(team, dict) and team.get("name"):
                    names.append(str(team["name"]))
            if len(names) < 2 and isinstance(item.get("competitor"), list):
                names = [str(c.get("name")) for c in item["competitor"]
                         if isinstance(c, dict) and c.get("name")][:2]
            if len(names) < 2:
                pair = _split_vs(str(item.get("name", "")), backed=True)
                names = list(pair) if pair else []
            if len(names) == 2:
                return {"teams": names, "ts": _epoch(str(item.get("startDate", "")))}
    return None


def duration_hours(league_id: str, label: str = "", url: str = "") -> float:
    if league_id in DURATION_HOURS:
        return DURATION_HOURS[league_id]
    league = tags.resolve_league(league_id)
    if (league and league["espn"].startswith("soccer/")) or "soccer" in (label + " " + url).lower():
        return SOCCER_HOURS
    return FALLBACK_HOURS


def _event(team_a: str, team_b: str, label: str, ts: Optional[float],
           chunks: List[str], url: str) -> Dict[str, Any]:
    """Canonical teams and league from the snapshot; page text otherwise."""
    hits_a, hits_b = tags.find_team(team_a), tags.find_team(team_b)
    in_b = {lg for lg, _ in hits_b}
    common = [lg for lg, _ in hits_a if lg in in_b]
    hint = tags.resolve_league(label)
    if hint and (not common or hint["id"] in common):
        league = hint["id"]
    elif common:
        league = common[0]
    else:
        league = ""

    def canon(hits, raw):
        for lg, name in hits:
            if not league or lg == league:
                return name
        return raw

    live, status = _live(chunks)
    hours = duration_hours(league, label, url)
    return {
        "url": url,
        "sport": league or label.strip(),
        "league_known": bool(league),
        "team_a": canon(hits_a, team_a),
        "team_b": canon(hits_b, team_b),
        "start_at": ts,
        "end_at": ts + hours * 3600 if ts else None,
        "duration_hours": hours,
        "live": live,
        "status": status,
    }


def _label_after(chunks: List[str], teams: Tuple[str, str]) -> str:
    """The first plain text after the team names: a league or sport label."""
    try:
        start = max(chunks.index(teams[0]), chunks.index(teams[1])) + 1
    except ValueError:
        start = 0
    for c in chunks[start:]:
        if c not in teams and not _SKIP_CHUNK.match(c) and len(c) <= 40:
            return c
    return ""


def _page_event(scan: _Scan, page_url: str) -> Optional[Dict[str, Any]]:
    found = _from_ld(scan.ld)
    teams, label, ts = None, "", scan.page.get("ts")
    backed = bool(ts) or _live(scan.page["chunks"])[0]
    if found:
        teams, ts = tuple(found["teams"]), found["ts"] or ts
    else:
        for text in (scan.heading, scan.meta.get("og:description"),
                     scan.meta.get("description"), scan.meta.get("og:title"),
                     scan.meta.get("title")):
            pair = _split_vs(text or "", backed)
            if pair:
                b, rest = _trim_to_team(pair[1])
                teams, label = (pair[0], b), rest
                break
    if not teams:
        return None
    if not label:
        # "A vs B <League> Live Stream": what follows team B in the title.
        for text in (scan.meta.get("og:title"), scan.meta.get("title")):
            text = _TAIL_RE.sub("", (text or "").split(" | ")[0])
            if teams[1] in text:
                label = text.split(teams[1], 1)[1].strip(" -|:–")
                if label:
                    break
    return _event(teams[0], teams[1], label, ts, scan.page["chunks"], page_url)


def parse(html: str, page_url: str) -> Dict[str, Any]:
    """{"event": {...} or None, "events": [...]} for one fetched page.

    `event` is this page's own game; `events` are the games its links name
    (a listing page), live ones first, then by start time.
    """
    scan = _Scan()
    try:
        scan.feed(html or "")
        scan.close()
    except Exception:  # malformed markup: keep what was read so far
        pass

    try:
        base = urljoin(page_url, scan.base) if scan.base else page_url
    except ValueError:
        base = page_url
    events, seen = [], {page_url.rstrip("/")}
    for link in scan.links:
        try:
            url = urljoin(base, link["href"])
        except ValueError:   # e.g. "http://[::1": not a link anyone can follow
            continue
        if not url.startswith(("http://", "https://")) or url.rstrip("/") in seen:
            continue
        chunks = link["chunks"]
        # Backed: a time or LIVE label, or both halves of the title also
        # shown as separate text (two team names laid out side by side).
        halves = [h.strip() for h in re.split(r"\s[-–]\s", link["title"], maxsplit=1)]
        backed = (bool(link.get("ts")) or _live(chunks)[0]
                  or (len(halves) == 2 and all(h in chunks for h in halves)))
        pair = _split_vs(link["title"], backed)
        if not pair and len(chunks) >= 2 and tags.find_team(chunks[0]) and tags.find_team(chunks[1]):
            pair = (chunks[0], chunks[1])
        if not pair:
            continue
        seen.add(url.rstrip("/"))
        ev = _event(pair[0], pair[1], _label_after(chunks, pair), link.get("ts"), chunks, url)
        events.append(ev)
        if len(events) >= MAX_EVENTS:
            break
    events.sort(key=lambda e: (not e["live"], e["start_at"] or 0))
    return {"event": _page_event(scan, page_url), "events": events}


def window(event: Dict[str, Any], now: Optional[float] = None) -> Tuple[float, float]:
    """(start, end) epochs to schedule: the page's times, or from now for a
    game already on or with no time shown."""
    now = time.time() if now is None else now
    start = event.get("start_at") or now
    if event.get("live"):
        start = max(start, now)
    return start, start + event["duration_hours"] * 3600
