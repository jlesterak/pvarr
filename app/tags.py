#!/usr/bin/env python3
"""
League and team tags, with autocomplete that works offline.

The new-recording form's Sport / Team A / Team B boxes are the recording's
tags: they are stored with the session and they name the file
(`2026-10-07_NHL_Colorado_Avalanche_vs_Winnipeg_Jets_1080p.mp4`). This module
suggests canonical values for them as you type -- "ncaa" offers every NCAA
league, "packe" offers the Green Bay Packers -- so the same team is spelt the
same way every time.

The data is a snapshot shipped in the repo (`app/data/teams.json`), read once
and searched in memory. Nothing here touches the network at runtime: PVArr is
meant to keep working with the internet down. The snapshot is refreshed by
hand, by a maintainer, with

    python3 -m app.tags --refresh

which asks a `TeamProvider` for each league's teams. The default provider is
ESPN's public JSON API -- free, no key, and the only free source found that
covers the NCAA (measured 2026-10-05: 762 college football teams, 362 men's
and 362 women's basketball). It is unofficial and could change without
notice; that is acceptable *because* it is only consulted on refresh, and a
league that fails to refresh keeps its previous teams. A keyed provider can be
added later by subclassing `TeamProvider` and registering it in `PROVIDERS`.
"""

import argparse
import json
import os
import sys
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import Dict, List, Optional

DATA_FILE = Path(__file__).resolve().parent / "data" / "teams.json"
MAX_SUGGESTIONS = 15

# The league catalogue is curated, not fetched: aliases are how people type,
# and no API knows that "cfb" means college football. `id` is what goes into
# the Sport box (and so the filename); `espn` is the provider's path.
LEAGUES: List[Dict] = [
    {"id": "NFL", "name": "National Football League", "aliases": ["pro football"], "espn": "football/nfl"},
    {"id": "NBA", "name": "National Basketball Association", "aliases": [], "espn": "basketball/nba"},
    {"id": "NHL", "name": "National Hockey League", "aliases": ["hockey"], "espn": "hockey/nhl"},
    {"id": "MLB", "name": "Major League Baseball", "aliases": ["baseball"], "espn": "baseball/mlb"},
    {"id": "MLS", "name": "Major League Soccer", "aliases": [], "espn": "soccer/usa.1"},
    {"id": "WNBA", "name": "Women's National Basketball Association", "aliases": [], "espn": "basketball/wnba"},
    {"id": "NWSL", "name": "National Women's Soccer League", "aliases": [], "espn": "soccer/usa.nwsl"},
    {"id": "CFL", "name": "Canadian Football League", "aliases": [], "espn": "football/cfl"},
    {"id": "NCAAF", "name": "NCAA Football", "aliases": ["cfb", "college football", "ncaa fb"], "espn": "football/college-football"},
    {"id": "NCAAM", "name": "NCAA Men's Basketball", "aliases": ["mbb", "ncaa mbb", "college basketball", "march madness"], "espn": "basketball/mens-college-basketball"},
    {"id": "NCAAW", "name": "NCAA Women's Basketball", "aliases": ["wbb", "ncaa wbb", "womens college basketball"], "espn": "basketball/womens-college-basketball"},
    {"id": "NCAAH", "name": "NCAA Men's Ice Hockey", "aliases": ["college hockey", "ncaa hockey"], "espn": "hockey/mens-college-hockey"},
    {"id": "NCAABSB", "name": "NCAA Baseball", "aliases": ["college baseball", "ncaa baseball"], "espn": "baseball/college-baseball"},
    {"id": "EPL", "name": "English Premier League", "aliases": ["premier league", "prem", "bpl"], "espn": "soccer/eng.1"},
    {"id": "UCL", "name": "UEFA Champions League", "aliases": ["champions league"], "espn": "soccer/uefa.champions"},
    {"id": "La Liga", "name": "Spanish La Liga", "aliases": ["laliga", "liga"], "espn": "soccer/esp.1"},
    {"id": "Bundesliga", "name": "German Bundesliga", "aliases": [], "espn": "soccer/ger.1"},
    {"id": "Serie A", "name": "Italian Serie A", "aliases": [], "espn": "soccer/ita.1"},
    {"id": "Ligue 1", "name": "French Ligue 1", "aliases": [], "espn": "soccer/fra.1"},
    {"id": "Liga MX", "name": "Mexican Liga MX", "aliases": [], "espn": "soccer/mex.1"},
]


# Fan shorthand no API carries. Kept here, not in the snapshot, so a refresh
# never erases it. Keyed by the provider's display name; small on purpose --
# add the ones you actually type.
TEAM_ALIASES: Dict[str, List[str]] = {
    "Colorado Avalanche": ["avs"],
    "Montreal Canadiens": ["habs"],
    "Ottawa Senators": ["sens"],
    "Washington Capitals": ["caps"],
    "Tampa Bay Lightning": ["bolts"],
    "Carolina Hurricanes": ["canes"],
    "Pittsburgh Penguins": ["pens"],
    "Toronto Maple Leafs": ["leafs"],
    "Vegas Golden Knights": ["vgk"],
    "San Francisco 49ers": ["niners"],
    "New England Patriots": ["pats"],
    "Tampa Bay Buccaneers": ["bucs"],
    "Jacksonville Jaguars": ["jags"],
    "Miami Dolphins": ["phins", "fins"],
    "Green Bay Packers": ["pack"],
    "Dallas Mavericks": ["mavs"],
    "Cleveland Cavaliers": ["cavs"],
    "Philadelphia 76ers": ["sixers"],
    "Minnesota Timberwolves": ["wolves", "twolves"],
    "Boston Red Sox": ["sox"],
    "Chicago White Sox": ["sox"],
    "Arizona Diamondbacks": ["dbacks", "d-backs"],
    "Manchester United": ["man utd", "man u"],
    "Manchester City": ["man city"],
    "Tottenham Hotspur": ["spurs"],
}


# --------------------------------------------------------------------------
# Matching
# --------------------------------------------------------------------------

def _score(query: str, exact: List[str], fields: List[str]) -> Optional[int]:
    """Lower is better: 0 exact code/alias, 1 a field starts with the query,
    2 a word in a field does, 3 substring of a field; None for no match.

    Codes (abbreviations, aliases) match exactly or by prefix only -- as a
    substring they are noise ("avs" is inside "SAVS").
    """
    best = None
    for code in exact:
        c = (code or "").lower()
        if c and query == c:
            return 0
        if c and c.startswith(query):
            best = 1
    for field in fields:
        f = (field or "").lower()
        if not f:
            continue
        if f.startswith(query):
            return 1
        if any(w.startswith(query) for w in f.split()):
            best = 2 if best is None else min(best, 2)
        elif query in f and best is None:
            best = 3
    return best


def resolve_league(value: str) -> Optional[Dict]:
    """The catalogue entry a typed league names exactly, or None."""
    q = (value or "").strip().lower()
    for league in LEAGUES:
        if q in [league["id"].lower(), league["name"].lower()] + [a.lower() for a in league["aliases"]]:
            return league
    return None


def suggest_leagues(query: str, limit: int = MAX_SUGGESTIONS) -> List[Dict[str, str]]:
    q = (query or "").strip().lower()
    hits = []
    for order, league in enumerate(LEAGUES):
        names = [league["id"], league["name"]] + league["aliases"]
        score = 0 if not q else _score(q, [league["id"]] + league["aliases"], names)
        if score is not None:
            hits.append((score, order, {"value": league["id"], "label": league["name"]}))
    return [h[2] for h in sorted(hits, key=lambda h: h[:2])][:limit]


@lru_cache(maxsize=1)
def load_snapshot(path: str = str(DATA_FILE)) -> Dict:
    """The bundled teams, or an empty set if the file is missing or corrupt --
    autocomplete is a convenience and must never stop a recording."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data.get("leagues"), dict):
            return data
    except (OSError, ValueError, AttributeError):
        pass
    return {"leagues": {}}


@lru_cache(maxsize=1)
def _team_index() -> tuple:
    """The snapshot flattened and lower-cased once, so a keystroke is one
    pass over ~2,400 tuples rather than ~2,400 rounds of string building."""
    rows = []
    for order, (league_id, teams) in enumerate(load_snapshot().get("leagues", {}).items()):
        for row in teams:
            name, abbr, location, nickname = (list(row) + ["", "", "", ""])[:4]
            codes = [c.lower() for c in [abbr] + TEAM_ALIASES.get(name, []) if c]
            fields = [f.lower() for f in (name, nickname, location) if f]
            label = f"{league_id} · {abbr}" if abbr else league_id
            rows.append((order, league_id, name, codes, fields, label))
    return tuple(rows)


def suggest_teams(query: str, league: str = "", limit: int = MAX_SUGGESTIONS) -> List[Dict[str, str]]:
    """Teams matching `query` on name, nickname, city or abbreviation.

    Filtered to `league` when it names a known league, otherwise all leagues.
    Without a league the same school appears in several NCAA sports; it is
    suggested once.
    """
    q = (query or "").strip().lower()
    if not q:
        return []
    chosen = resolve_league(league)
    hits, seen = [], set()
    for order, league_id, name, codes, fields, label in _team_index():
        if chosen and league_id != chosen["id"]:
            continue
        if name in seen:
            continue
        score = _score(q, codes, fields)
        if score is None:
            continue
        seen.add(name)
        hits.append((score, order, name.lower(), {"value": name, "label": label}))
    return [h[3] for h in sorted(hits, key=lambda h: h[:3])][:limit]


# --------------------------------------------------------------------------
# Providers (refresh only)
# --------------------------------------------------------------------------

class TeamProvider:
    """Where a refresh gets a league's teams. Subclass to add a source.

    `teams()` returns rows of [name, abbreviation, location, nickname] and
    raises on any failure, so the refresh can keep that league's old data.
    """
    name = "base"

    def teams(self, league: Dict) -> List[List[str]]:
        raise NotImplementedError


class ESPNProvider(TeamProvider):
    """ESPN's public site API. Free, no key, unofficial."""
    name = "espn"
    BASE = "https://site.api.espn.com/apis/site/v2/sports"

    def fetch(self, path: str) -> Dict:
        import requests
        resp = requests.get(f"{self.BASE}/{path}/teams", params={"limit": 1000}, timeout=20)
        resp.raise_for_status()
        return resp.json()

    def teams(self, league: Dict) -> List[List[str]]:
        return parse_espn_teams(self.fetch(league["espn"]))


def parse_espn_teams(payload: Dict) -> List[List[str]]:
    rows = []
    for league in payload["sports"][0]["leagues"]:
        for entry in league.get("teams", []):
            team = entry.get("team") or {}
            name = (team.get("displayName") or "").strip()
            if not name:
                continue
            # ESPN's "name" is the nickname for pro teams ("Packers") and the
            # club name for soccer; "location" is the city or school.
            nickname = team.get("name") or team.get("shortDisplayName") or ""
            rows.append([name, team.get("abbreviation") or "",
                         team.get("location") or "", nickname if nickname != name else ""])
    if not rows:
        raise ValueError("no teams in response")
    return sorted(rows, key=lambda r: r[0].lower())


PROVIDERS = {"espn": ESPNProvider}


def refresh(provider: TeamProvider, path: Path = DATA_FILE, log=print) -> Dict:
    """Rebuild the snapshot league by league; a failed league keeps its old teams."""
    old = load_snapshot.__wrapped__(str(path)).get("leagues", {})
    leagues: Dict[str, List[List[str]]] = {}
    for league in LEAGUES:
        try:
            leagues[league["id"]] = provider.teams(league)
            log(f"{league['id']}: {len(leagues[league['id']])} teams")
        except Exception as exc:  # any provider failure: keep what we had
            if league["id"] in old:
                leagues[league["id"]] = old[league["id"]]
            log(f"{league['id']}: refresh failed ({exc}); kept {len(old.get(league['id'], []))} old teams")
    data = {"source": provider.name, "leagues": leagues}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
        fh.write("\n")
    # mkstemp creates 0600; the image COPYs this file as root and PVArr runs
    # as PUID, which then could not read it.
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)
    load_snapshot.cache_clear()
    _team_index.cache_clear()
    return data


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="PVArr league/team tag data")
    parser.add_argument("--refresh", action="store_true",
                        help="rebuild app/data/teams.json from the provider (needs internet)")
    parser.add_argument("--provider", default="espn", choices=sorted(PROVIDERS))
    args = parser.parse_args(argv)
    if not args.refresh:
        parser.print_help()
        return 0
    refresh(PROVIDERS[args.provider]())
    return 0


if __name__ == "__main__":
    sys.exit(main())
