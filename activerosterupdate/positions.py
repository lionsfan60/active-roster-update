"""Fill an Axis roster slot by slot, following the layout the game already accepts.

Axis 2027 wants specific positions - LT, LG, C, RG, RT, DE, DT, ILB, OLB, LCB, RCB,
NCB, FS, SS - not position groups. Writing "OL" where it expects "C" leaves the game
unable to build a formation, and it hangs on kickoff.

Rather than invent a layout, we copy the one in the file the game shipped: whatever
position sits on row N of the existing roster, a player of that same position goes on
row N of ours. That preserves each team's shape and keeps the KickReturnerIndex /
PuntReturnerIndex directives pointing at the same kind of player.
"""
from __future__ import annotations

import json

from . import config
from .model import Player

_MAP = None


def _maps() -> dict:
    global _MAP
    if _MAP is None:
        _MAP = json.loads((config.MAPPING / "positions.json").read_text(encoding="utf-8"))
    return _MAP


def slot_to_axis(slot: str) -> str:
    return _maps()["_espn_slot_to_axis"].get((slot or "").lower(), "")


def candidates_for(p: Player) -> list[str]:
    """Axis positions this player could line up at, best first."""
    table = _maps()["_nfl_position_to_axis"]
    out: list[str] = []
    fromslot = slot_to_axis(p.slot)
    if fromslot:
        out.append(fromslot)
    for code in table.get(p.pos_nfl.upper(), []):
        if code not in out:
            out.append(code)
    return out


def family(code: str) -> str:
    return _maps()["_family"].get(code, code)


def template_layout(rf) -> list[str]:
    """The POS on each row of the roster the game already has, in order.

    Codes 2027 does not recognise (LB, S - leftovers from the 2026 mod) are rewritten to
    the specific slots it does, cycling through the alternatives so a team ends up with a
    sensible spread instead of four of the same.
    """
    col = rf.column("POS")
    if not col:
        return []
    swaps = _maps().get("_layout_normalize", {})
    seen: dict[str, int] = {}
    out = []
    for row in rf.rows:
        code = (row.get(col, "") or "").strip().upper()
        if code in swaps:
            options = swaps[code]
            n = seen.get(code, 0)
            seen[code] = n + 1
            code = options[n % len(options)]
        out.append(code)
    return out


def assign(players: list[Player], layout: list[str]) -> list[Player]:
    """Pick a player for every row of the layout. Returns them in row order.

    Each row is filled by the best unused player who can play that position: an exact
    depth-chart match first, then his listed position, then anyone from the same family
    so a row is never left empty.
    """
    unused = sorted(players, key=lambda p: (p.depth, -p.overall, p.last))
    taken: set[int] = set()
    filled: list[Player | None] = []

    def take(pred) -> Player | None:
        for p in unused:
            if id(p) not in taken and pred(p):
                taken.add(id(p))
                return p
        return None

    # scarce slots first, so a lone centre isn't spent filling a guard row
    order = sorted(
        range(len(layout)),
        key=lambda i: sum(1 for p in players if layout[i] in candidates_for(p)),
    )

    picks: dict[int, Player] = {}
    for i in order:
        want = layout[i]
        pick = (
            # 1. the depth chart says he plays exactly this slot
            take(lambda p, w=want: slot_to_axis(p.slot) == w)
            # 2. it is the natural home for his listed position (a centre before a guard,
            #    a tackle before either, so a real RT is not spent on a backup LT row)
            or take(lambda p, w=want: (candidates_for(p) or [""])[0] == w)
            # 3. he can play it
            or take(lambda p, w=want: w in candidates_for(p))
            # 4. same side of the ball, so the row is never left empty
            or take(lambda p, w=want: any(family(c) == family(w) for c in candidates_for(p)))
            or take(lambda p: True)
        )
        if pick:
            picks[i] = pick

    for i in range(len(layout)):
        p = picks.get(i)
        if p is not None:
            p.pos_axis = layout[i]
            filled.append(p)
    return [p for p in filled if p is not None]
