"""Locate the Axis Football install and pair NFL clubs with its team-mod folders."""
from __future__ import annotations

import difflib
import json
import re
from pathlib import Path

from .. import config

VDF_PATH = re.compile(r'"path"\s+"([^"]+)"')


def steam_libraries() -> list[Path]:
    roots = [
        Path(r"C:/Program Files (x86)/Steam"),
        Path(r"C:/Program Files/Steam"),
    ]
    libs: list[Path] = []
    for root in roots:
        vdf = root / "steamapps" / "libraryfolders.vdf"
        if vdf.exists():
            text = vdf.read_text(encoding="utf-8", errors="replace")
            for m in VDF_PATH.finditer(text):
                libs.append(Path(m.group(1).replace("\\\\", "/")))
    for extra in (Path("D:/SteamLibrary"), Path("E:/SteamLibrary")):
        if extra.exists():
            libs.append(extra)
    seen: list[Path] = []
    for lib in libs:
        if lib not in seen and lib.exists():
            seen.append(lib)
    return seen


def find_game(cfg: dict) -> Path | None:
    """Root folder of the Axis Football install, or None."""
    if cfg.get("axis_dir"):
        p = Path(cfg["axis_dir"])
        return p if p.exists() else None

    names = cfg.get("game_folder_names") or []
    for lib in steam_libraries():
        common = lib / "steamapps" / "common"
        if not common.exists():
            continue
        for name in names:
            cand = common / name
            if cand.exists():
                return cand
        for cand in sorted(common.glob("Axis Football*"), reverse=True):
            return cand
    return None


def mods_dir(game_dir: Path) -> Path | None:
    """The Mods/Team Mods folder Axis reads custom teams from."""
    for cand in (game_dir / "Mods" / "Team Mods", game_dir / "Mods" / "Teams"):
        if cand.exists():
            return cand
    mods = game_dir / "Mods"
    if mods.exists():
        for sub in mods.iterdir():
            if sub.is_dir() and "team" in sub.name.lower():
                return sub
    return None


def team_folders(mods: Path) -> list[str]:
    return sorted(d.name for d in mods.iterdir() if d.is_dir())


def auto_link(nfl_teams: list[dict], folders: list[str]) -> dict[str, str]:
    """ESPN abbreviation -> Axis team-mod folder name, matched on city/nickname."""
    link: dict[str, str] = {}
    remaining = list(folders)
    lowered = {f.lower(): f for f in remaining}

    for team in nfl_teams:
        target = None
        for key in (team["display"], team["location"] + " " + team["name"], team["name"]):
            if key.lower() in lowered:
                target = lowered[key.lower()]
                break
        if not target:
            hits = [f for f in remaining if team["name"].lower() in f.lower()]
            if len(hits) == 1:
                target = hits[0]
        if not target:
            close = difflib.get_close_matches(team["display"].lower(),
                                              [f.lower() for f in remaining], n=1, cutoff=0.6)
            if close:
                target = lowered.get(close[0])
        if target:
            link[team["abbrev"]] = target
            if target in remaining:
                remaining.remove(target)
    return link


def load_links() -> dict[str, str]:
    p = config.MAPPING / "teams.json"
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    return {k: v for k, v in data.items() if not k.startswith("_")}


def save_links(links: dict[str, str]) -> Path:
    p = config.MAPPING / "teams.json"
    payload = {"_comment": "ESPN team abbreviation -> Axis 'Mods/Team Mods' folder name. Edit freely."}
    payload.update(dict(sorted(links.items())))
    p.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return p
