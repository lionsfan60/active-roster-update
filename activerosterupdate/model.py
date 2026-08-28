"""Canonical player/team records - the format both sources normalise into."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Injury:
    status: str = ""          # Out, Questionable, Doubtful, Injured Reserve, Suspension, ...
    detail: str = ""          # body part / designation
    comment: str = ""
    source: str = ""

    @property
    def is_out(self) -> bool:
        s = self.status.lower()
        return any(k in s for k in ("out", "injured reserve", "reserve", "pup", "nfi",
                                    "suspension", "suspended", "doubtful"))


@dataclass
class Player:
    espn_id: str = ""
    first: str = ""
    last: str = ""
    team: str = ""            # ESPN team abbreviation, e.g. "ARI"
    pos_nfl: str = ""         # ESPN position abbreviation
    pos_axis: str = ""        # QB/RB/WR/TE/OL/DL/LB/DB/K/P
    jersey: int = 0
    height_in: int = 72
    weight_lb: int = 200
    age: int = 25
    dob: str = ""
    status: str = "Active"    # ESPN roster status
    depth: int = 99           # depth-chart rank inside the slot (1 = starter)
    slot: str = ""            # ESPN depth-chart slot: lt, rg, lcb, mlb, pk, ...
    injury: Injury | None = None

    # from EA ratings
    madden_id: int | None = None
    overall: int = 0
    ratings: dict[str, float] = field(default_factory=dict)
    abilities: list[dict[str, Any]] = field(default_factory=list)
    match: str = "none"       # how ratings were matched: exact/dob/fuzzy/none

    @property
    def full(self) -> str:
        return f"{self.first} {self.last}".strip()

    @property
    def playable(self) -> bool:
        return not (self.injury and self.injury.is_out)
