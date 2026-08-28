"""Turn live NFL data + EA ratings into Axis Football roster rows."""
from __future__ import annotations

import hashlib
import json
from datetime import date

from . import config, positions
from .match import RatingsIndex, norm
from .model import Player
from .sources import espn


def clamp(v: float, lo: int = 1, hi: int = 99) -> int:
    return int(max(lo, min(hi, round(v))))


# ---------------------------------------------------------------- gathering


def gather_team(team: dict, ratings: RatingsIndex, cfg: dict, season: int,
                league_injuries: dict) -> list[Player]:
    """Live roster for one club with injuries and ratings attached."""
    players = espn.roster(team["abbrev"])
    depth = espn.depth_chart(team["id"], season)
    if not depth:
        print(f"  ! no depth chart for {team['abbrev']} - positions fall back to listed ones",
              flush=True)

    for p in players:
        entry = depth.get(p.espn_id) or {}
        p.slot = entry.get("slot", "")
        # rank 1 as the long snapper or kick returner does not make him a starter
        p.depth = entry.get("rank", 99) if positions.slot_to_axis(p.slot) else 99
        p.pos_axis = (positions.candidates_for(p) or [""])[0]
        if p.espn_id in league_injuries:
            p.injury = league_injuries[p.espn_id]
        rec, how = ratings.find(p)
        p.match = how
        if rec:
            p.madden_id = rec["madden_id"]
            p.overall = rec["overall"]
            p.ratings = rec["stats"]
            p.abilities = rec["abilities"]
    return [p for p in players if p.pos_axis]


def manual_overrides(team_abbrev: str) -> dict[str, set[str]]:
    """Hand-edited in/out list from mapping/overrides.json (the official inactives sheet)."""
    path = config.MAPPING / "overrides.json"
    if not path.exists():
        return {"out": set(), "in": set()}
    data = json.loads(path.read_text(encoding="utf-8"))
    team = data.get(team_abbrev.upper(), {}) if not team_abbrev.startswith("_") else {}
    return {
        "out": {norm(n) for n in team.get("out", [])},
        "in": {norm(n) for n in team.get("in", [])},
    }


def status_excluded(p: Player, cfg: dict, overrides: dict | None = None) -> str | None:
    """Reason this player is off the gameday roster, or None if available."""
    overrides = overrides or {"out": set(), "in": set()}
    key = norm(p.full)
    if key in overrides["out"]:
        return "Inactive (manual override)"
    if key in overrides["in"]:
        return None
    bad = [s.lower() for s in cfg["exclude_statuses"]]
    inj = (p.injury.status if p.injury else "") or ""
    if inj and any(b in inj.lower() for b in bad):
        detail = p.injury.detail
        return inj + (" (" + detail + ")" if detail else "")
    st = (p.status or "").lower()
    if "injur" in st or "reserve" in st or "suspend" in st:
        return p.status
    if not cfg["include_practice_squad"] and "practice" in st:
        return "Practice Squad"
    return None


# ---------------------------------------------------------------- ratings


def synth_profiles(all_players: list[Player]) -> dict[str, list[tuple[int, dict]]]:
    """Per Axis position, real rated players sorted by overall - templates for the unrated."""
    prof: dict[str, list[tuple[int, dict]]] = {}
    for p in all_players:
        if p.ratings and p.overall:
            prof.setdefault(p.pos_axis, []).append((p.overall, p.ratings))
    for pos in prof:
        prof[pos].sort(key=lambda t: t[0])
    return prof


def fill_unrated(p: Player, profiles: dict, cfg: dict) -> None:
    """Give an unrated player (rookie, UDFA, camp body) a believable profile."""
    tiers = cfg["unrated_target_overall"]
    target = tiers["starter"] if p.depth <= 1 else tiers["backup"] if p.depth <= 2 else tiers["depth"]
    pool = profiles.get(p.pos_axis) or []
    if not pool:
        p.ratings = {}
        p.overall = target
        return
    _ovr, stats = min(pool, key=lambda t: abs(t[0] - target))
    # jitter off the template player so two unrated players never come out identical,
    # but stay deterministic so re-running produces the same roster
    seed = int(hashlib.sha1((p.full + p.espn_id).encode()).hexdigest(), 16)
    jittered = {}
    for i, (key, val) in enumerate(sorted(stats.items())):
        jittered[key] = clamp(val + ((seed >> (i % 24)) % 7) - 3)
    p.ratings = jittered
    p.overall = target
    p.match = "synth"


def axis_attributes(p: Player, cfg: dict) -> dict[str, int]:
    """Blend EA attributes into the Axis column set."""
    raw = json.loads((config.MAPPING / "attributes.json").read_text(encoding="utf-8"))
    blends = {k: v for k, v in raw.items() if not k.startswith("_")}
    blends.update(raw.get("_position_overrides", {}).get(p.pos_axis, {}))
    out: dict[str, int] = {}
    for col, weights in blends.items():
        total = num = 0.0
        for key, w in weights.items():
            if key in p.ratings:
                total += p.ratings[key] * w
                num += w
        out[col] = clamp(total / num) if num else 50

    # X-Factor / Superstar abilities nudge the matching Axis attribute
    ab_map = config.mapping("abilities")["labels"]
    scale = cfg["ability_scale"]
    for ab in p.abilities:
        label = (ab.get("label") or "").lower()
        bump_def = next((v for k, v in ab_map.items() if k in label), None)
        if not bump_def:
            continue
        factor = scale.get(ab.get("type", ""), 0.5)
        for col, amount in bump_def.items():
            if col in out:
                out[col] = clamp(out[col] + amount * factor)

    if p.injury and "questionable" in (p.injury.status or "").lower():
        pen = cfg["questionable_penalty"]
        for col in ("SPEED", "AGIL", "FITNESS"):
            if col in out:
                out[col] = clamp(out[col] * pen)
    return out


# ---------------------------------------------------------------- selection


def pick_squad(players: list[Player], cfg: dict, team_abbrev: str = "",
               layout: list[str] | None = None) -> tuple[list[Player], list[tuple[Player, str]]]:
    """Choose the gameday squad: drop the unavailable, then fill the game's own layout."""
    overrides = manual_overrides(team_abbrev) if team_abbrev else None

    benched: list[tuple[Player, str]] = []
    available: list[Player] = []
    for p in players:
        reason = status_excluded(p, cfg, overrides)
        if reason:
            benched.append((p, reason))
        else:
            available.append(p)

    if layout:
        return positions.assign(available, layout), benched

    # no template to follow (writing to ./out with no game installed) - group order
    plan = config.mapping("squad")
    by_pos: dict[str, list[Player]] = {}
    for p in available:
        by_pos.setdefault(positions.family(p.pos_axis), []).append(p)
    for fam in by_pos:
        by_pos[fam].sort(key=lambda p: (p.depth, -p.overall, p.last))
    squad: list[Player] = []
    for fam, count in plan.items():
        squad.extend(by_pos.get(fam, [])[:count])
    return squad[:cfg["roster_size"]], benched


# ---------------------------------------------------------------- rows


def _stable_pick(seed: str, options: list[str]) -> str:
    if not options:
        return "0"
    h = int(hashlib.sha1(seed.encode()).hexdigest()[:8], 16)
    return options[h % len(options)]


def cosmetic_pools(template_rows: list[dict], header: list[str]) -> dict[str, list[str]]:
    """Legal values for look-only columns, learned from the roster the game shipped."""
    pools: dict[str, list[str]] = {}
    wanted = {"SKIN", "VISOR", "SLEEVES", "BANDS", "WRAPS", "PORTRAIT"}
    for col in header:
        if col.replace(" ", "").upper() not in wanted:
            continue
        vals = sorted({r.get(col, "").strip() for r in template_rows if r.get(col, "").strip()})
        pools[col] = vals or ["0"]
    return pools


def _key(name: str) -> str:
    return name.replace(" ", "").replace("_", "").upper()


def build_rows(squad: list[Player], rf, cfg: dict) -> list[dict[str, str]]:
    """Render the chosen squad into rows matching the installed game CSV header.

    Column names are matched case- and space-insensitively: Axis renamed several of them
    between releases (HEIGHT -> Height, BLKING -> PAS BLK + RUN BLK, COVER -> M COV + Z COV),
    so the header the game ships with decides what actually gets written.
    """
    prior = {norm(r.get("FIRST", "") + " " + r.get("LAST", "")): r for r in rf.rows}
    pools = cosmetic_pools(rf.rows, rf.header)
    header_by_key = {_key(h): h for h in rf.header}
    used_numbers: set[int] = set()
    rows: list[dict[str, str]] = []

    def put(row: dict, name: str, value) -> None:
        col = header_by_key.get(_key(name))
        if col is not None:
            row[col] = str(value)

    for idx, p in enumerate(squad):
        attrs = axis_attributes(p, cfg)
        was = prior.get(norm(p.full), {}) if cfg["preserve_cosmetics"] else {}

        number = p.jersey if p.jersey is not None else 0
        while number in used_numbers:          # jersey 0 is legal, duplicates are not
            number = (number + 1) % 100
        used_numbers.add(number)

        row = {h: "" for h in rf.header}
        put(row, "INDEX", idx)
        put(row, "FIRST", p.first)
        put(row, "LAST", p.last)
        put(row, "NUMBER", number)
        put(row, "HEIGHT", p.height_in)
        put(row, "WEIGHT", p.weight_lb)
        put(row, "POS", p.pos_axis)
        put(row, "AGE", p.age)
        for col, val in attrs.items():
            put(row, col, val)

        for col, pool in pools.items():
            row[col] = was.get(col) or _stable_pick(p.full + "|" + col, pool)

        if header_by_key.get("POTENTIAL"):
            if cfg["derive_potential"]:
                put(row, "POTENTIAL", clamp(60 + (30 - p.age) * 3 + (p.overall - 70) / 2, 40, 99))
            else:
                put(row, "POTENTIAL", was.get(header_by_key["POTENTIAL"], "75"))
        rows.append(row)
    return rows


def current_season(cfg: dict) -> int:
    if cfg.get("season"):
        return int(cfg["season"])
    today = date.today()
    return today.year if today.month >= 3 else today.year - 1
