"""Join live ESPN roster entries to EA ratings records.

EA's feed carries no team or position, so the join is name-based. Birthdate is the
tiebreaker that makes it safe (every EA record has one, and so does ESPN).
"""
from __future__ import annotations

import difflib
import re
import unicodedata

SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}


def norm(name: str) -> str:
    s = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z ]", "", s).lower().strip()
    parts = [p for p in s.split() if p not in SUFFIXES]
    return " ".join(parts)


class RatingsIndex:
    def __init__(self, ea_players: list[dict]):
        self.players = ea_players
        self.by_dob: dict[tuple[str, str], dict] = {}
        self.by_name: dict[str, list[dict]] = {}
        for p in ea_players:
            key = (norm(p["last"]), p["dob"])
            self.by_dob.setdefault(key, p)
            self.by_name.setdefault(norm(f"{p['first']} {p['last']}"), []).append(p)
        self._names = list(self.by_name.keys())

    def find(self, player) -> tuple[dict | None, str]:
        """Returns (ea_record, how) where how is exact|dob|fuzzy|none."""
        full = norm(player.full)
        dob_key = (norm(player.last), player.dob)

        hit = self.by_dob.get(dob_key)
        if hit and norm(hit["first"])[:3] == norm(player.first)[:3]:
            return hit, "dob"

        cands = self.by_name.get(full, [])
        if len(cands) == 1:
            return cands[0], "exact"
        if len(cands) > 1:
            # same name twice - break the tie on birthdate, then on build
            for c in cands:
                if c["dob"] == player.dob:
                    return c, "dob"
            best = min(cands, key=lambda c: abs((c.get("weight") or 0) - player.weight_lb))
            return best, "exact"

        close = difflib.get_close_matches(full, self._names, n=1, cutoff=0.90)
        if close:
            c = self.by_name[close[0]][0]
            same_build = abs((c.get("height") or 0) - player.height_in) <= 2 and abs(
                (c.get("weight") or 0) - player.weight_lb
            ) <= 25
            if same_build:
                return c, "fuzzy"
        return None, "none"
