"""Configuration: defaults + config.json overrides."""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Frozen into a one-file exe, everything editable (mapping/, config.json, out/, backups/)
# lives next to the exe rather than inside the temporary unpack directory.
if getattr(sys, "frozen", False):
    ROOT = Path(sys.executable).resolve().parent
else:
    ROOT = Path(__file__).resolve().parent.parent
MAPPING = ROOT / "mapping"
TEMPLATES = ROOT / "templates"

DEFAULTS: dict = {
    # The club you follow. "--gameday" with no team named uses this one.
    "favorite_team": "DET",

    # Where Axis Football lives. null = auto-detect from Steam.
    "axis_dir": None,
    "steam_appid": "5026790",              # Axis Football 2027
    "game_folder_names": ["Axis Football 2027", "Axis Football 2026", "Axis Football 2024"],

    "season": None,                         # null = current year
    "ratings_iteration": "latest",          # "latest" | e.g. "9-week-8" | "default"
    "roster_size": 53,

    # Injury policy - who gets cut from the gameday roster.
    "exclude_statuses": [
        "Out", "Injured Reserve", "Physically Unable to Perform", "Non-Football Injury",
        "Suspension", "Doubtful", "Practice Squad Injured",
    ],
    "questionable_penalty": 0.95,           # multiplier on SPEED/AGIL/FITNESS, 1.0 = off
    "include_practice_squad": False,

    # Ratings shaping
    "ability_scale": {"xFactor": 1.0, "superstarAbility": 0.5},
    "unrated_target_overall": {"starter": 70, "backup": 62, "depth": 55},
    "derive_potential": True,

    "preserve_cosmetics": True,
    "backup": True,
}


def load(path: str | Path | None = None) -> dict:
    cfg = dict(DEFAULTS)
    p = Path(path) if path else ROOT / "config.json"
    if p.exists():
        cfg.update(json.loads(p.read_text(encoding="utf-8")))
    return cfg


def mapping(name: str) -> dict:
    data = json.loads((MAPPING / f"{name}.json").read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}
