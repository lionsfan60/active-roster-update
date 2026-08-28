"""EA's public Madden ratings API (drop-api.ea.com).

This is the feed behind ea.com/games/madden-nfl/ratings. It carries every attribute
for ~1,950 players plus X-Factor/Superstar abilities, and - the useful part - EA
re-publishes it as a separate "iteration" every week of the season. No Madden
install, no login, no scraping HTML.
"""
from __future__ import annotations

from ..http import get_json

BASE = "https://drop-api.ea.com/rating/madden-nfl"
PAGE = 100


def iterations(ttl: int = 3600) -> list[dict]:
    """Available weekly ratings drops, newest first ({'id': '9-week-8', 'label': ...})."""
    data = get_json(f"{BASE}/filters?locale=en", ttl=ttl)
    items = data.get("iterations", []) or []
    return sorted(items, key=lambda it: _iter_num(it["id"]), reverse=True)


def _iter_num(iteration_id: str) -> int:
    head = iteration_id.split("-", 1)[0]
    return int(head) if head.isdigit() else 0


def latest_iteration() -> dict | None:
    its = iterations()
    return its[0] if its else None


def fetch_players(iteration: str | None = None, ttl: int = 21600) -> list[dict]:
    """Every rated player for one weekly iteration, normalised to flat dicts."""
    out: list[dict] = []
    offset = 0
    total = None
    while total is None or offset < total:
        url = f"{BASE}?locale=en&limit={PAGE}&offset={offset}"
        if iteration:
            url += f"&iteration={iteration}"
        data = get_json(url, ttl=ttl)
        total = data.get("totalItems", 0)
        items = data.get("items", []) or []
        if not items:
            break
        for it in items:
            out.append(_normalise(it))
        offset += PAGE
    return out


def _normalise(item: dict) -> dict:
    stats = {}
    for key, val in (item.get("stats") or {}).items():
        v = val.get("value") if isinstance(val, dict) else val
        if isinstance(v, (int, float)):
            stats[key] = float(v)
    abilities = []
    for ab in item.get("playerAbilities") or []:
        abilities.append(
            {
                "label": ab.get("label", ""),
                "type": ((ab.get("type") or {}).get("id") or ""),
                "description": ab.get("description", ""),
            }
        )
    return {
        "madden_id": item.get("id"),
        "first": item.get("firstName", "") or "",
        "last": item.get("lastName", "") or "",
        "dob": (item.get("birthdate") or "")[:10],
        "height": item.get("height"),
        "weight": item.get("weight"),
        "jersey": item.get("jerseyNum"),
        "age": item.get("age"),
        "college": item.get("college", ""),
        "overall": int(item.get("overallRating") or 0),
        "iteration": (item.get("iteration") or {}).get("id", ""),
        "stats": stats,
        "abilities": abilities,
    }
