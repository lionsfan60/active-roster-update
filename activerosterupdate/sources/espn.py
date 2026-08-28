"""ESPN public JSON feeds: teams, rosters, depth charts, league-wide injuries.

These are the same endpoints espn.com itself calls - no key, no login. They are the
source of truth for *who is on the team right now* and *who is hurt*.
"""
from __future__ import annotations

import re
from datetime import date

from ..http import get_json
from ..model import Injury, Player

SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
CORE = "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl"


def teams(ttl: int = 86400) -> list[dict]:
    """All 32 clubs: {id, abbrev, location, name, display, color, alt_color}."""
    data = get_json(f"{SITE}/teams", ttl=ttl)
    out = []
    for entry in data["sports"][0]["leagues"][0]["teams"]:
        t = entry["team"]
        out.append(
            {
                "id": t["id"],
                "abbrev": t["abbreviation"].upper(),
                "location": t.get("location", ""),
                "name": t.get("name", ""),
                "display": t.get("displayName", ""),
                "color": t.get("color", "000000"),
                "alt_color": t.get("alternateColor", "ffffff"),
            }
        )
    return sorted(out, key=lambda t: t["display"])


def _age_from_dob(dob: str, fallback: int) -> int:
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", dob or "")
    if not m:
        return fallback
    y, mo, d = (int(x) for x in m.groups())
    today = date.today()
    return today.year - y - ((today.month, today.day) < (mo, d))


def roster(team_abbrev: str, ttl: int = 1800) -> list[Player]:
    """Current roster for one club, including practice-squad/IR entries ESPN lists."""
    data = get_json(f"{SITE}/teams/{team_abbrev.lower()}/roster", ttl=ttl)
    players: list[Player] = []
    for group in data.get("athletes", []):
        for a in group.get("items", []):
            pos = (a.get("position") or {}).get("abbreviation", "") or ""
            dob = (a.get("dateOfBirth") or "")[:10]
            inj = None
            raw_inj = a.get("injuries") or []
            if raw_inj:
                first = raw_inj[0]
                inj = Injury(
                    status=first.get("status", "") or "",
                    detail=(first.get("details") or {}).get("type", "") or "",
                    source="espn-roster",
                )
            jersey = a.get("jersey") or "0"
            players.append(
                Player(
                    espn_id=str(a.get("id", "")),
                    first=a.get("firstName", "") or "",
                    last=a.get("lastName", "") or "",
                    team=team_abbrev.upper(),
                    pos_nfl=pos.upper(),
                    jersey=int(re.sub(r"\D", "", jersey) or 0),
                    height_in=int(a.get("height") or 72),
                    weight_lb=int(a.get("weight") or 200),
                    age=_age_from_dob(dob, int(a.get("age") or 25)),
                    dob=dob,
                    status=((a.get("status") or {}).get("name") or "Active"),
                    injury=inj,
                )
            )
    return players


def injuries(ttl: int = 900) -> dict[str, Injury]:
    """League-wide injury report keyed by ESPN athlete id."""
    data = get_json(f"{SITE}/injuries", ttl=ttl)
    out: dict[str, Injury] = {}
    for team in data.get("injuries", []):
        for item in team.get("injuries", []):
            ath = item.get("athlete") or {}
            aid = str(ath.get("id") or item.get("id") or "")
            if not aid:
                continue
            det = item.get("details") or {}
            out[aid] = Injury(
                status=item.get("status", "") or "",
                detail=det.get("type", "") or det.get("detail", "") or "",
                comment=(item.get("shortComment") or item.get("longComment") or "")[:400],
                source="espn-injuries",
            )
    return out


_ATH_ID = re.compile(r"/athletes/(\d+)")


def depth_chart(team_id: str, season: int, ttl: int = 3600) -> dict[str, dict]:
    """espn_id -> {"slot": "lt"|"lcb"|..., "rank": 1}.

    The slot is what makes a specific Axis position assignable: ESPN publishes lt/lg/c/
    rg/rt, lde/rde, mlb/slb/wlb, lcb/rcb/nb, fs/ss directly. Best (lowest) rank wins,
    and the slot that produced it is the one we keep.
    """
    url = f"{CORE}/seasons/{season}/teams/{team_id}/depthcharts"
    try:
        data = get_json(url, ttl=ttl)
    except RuntimeError:
        data = {}
    if not data.get("items"):
        # a transient failure here silently costs a whole team its LT/RT/LCB/RCB sides,
        # so try once more past the cache before giving up
        try:
            data = get_json(url, ttl=0)
        except RuntimeError:
            return {}
    out: dict[str, dict] = {}
    for formation in data.get("items", []):
        for slot, entry in (formation.get("positions") or {}).items():
            for ath in entry.get("athletes", []):
                m = _ATH_ID.search((ath.get("athlete") or {}).get("$ref", ""))
                if not m:
                    continue
                aid, rank = m.group(1), int(ath.get("rank") or 99)
                if rank < out.get(aid, {}).get("rank", 100):
                    out[aid] = {"slot": slot.lower(), "rank": rank}
    return out


def next_game(team_abbrev: str, ttl: int = 3600) -> dict | None:
    """The club's next scheduled game: {date, opponent, home, name, week}."""
    from datetime import datetime, timezone

    data = get_json(f"{SITE}/teams/{team_abbrev.lower()}/schedule", ttl=ttl)
    now = datetime.now(timezone.utc)
    upcoming = []
    for ev in data.get("events", []):
        raw = ev.get("date", "")
        try:
            when = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        comp = (ev.get("competitions") or [{}])[0]
        us = them = None
        home = False
        for c in comp.get("competitors", []):
            abbr = ((c.get("team") or {}).get("abbreviation") or "").upper()
            if abbr == team_abbrev.upper():
                us = abbr
                home = c.get("homeAway") == "home"
            else:
                them = abbr
        if not (us and them):
            continue
        upcoming.append(
            {
                "date": when,
                "opponent": them,
                "home": home,
                "name": ev.get("shortName", ""),
                "week": (ev.get("week") or {}).get("number"),
                "season_type": (ev.get("seasonType") or {}).get("name", ""),
            }
        )
    future = [g for g in upcoming if g["date"] >= now]
    if future:
        return min(future, key=lambda g: g["date"])
    return max(upcoming, key=lambda g: g["date"]) if upcoming else None
