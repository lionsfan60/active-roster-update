"""Put real player photos into the game's portrait slots.

Axis reads portraits from Mods/Player Portraits/Skin Tone N/{Large,Small}/<id>.png -
128x128 RGBA, transparent background. A roster row picks one with its SKIN (which folder)
and PORTRAIT (which file) columns.

ESPN publishes a headshot for every player, keyed by the same id the roster sync already
joins on, so the work is: fetch, crop to head-and-shoulders, resize, write, then point the
roster row at it.
"""
from __future__ import annotations

import shutil
import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image

from .http import UA

HEADSHOT = "https://a.espncdn.com/i/headshots/nfl/players/full/{espn_id}.png"
SIZE = 128


def fetch_headshot(espn_id: str, timeout: int = 20) -> Image.Image | None:
    req = urllib.request.Request(HEADSHOT.format(espn_id=espn_id), headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = resp.read()
    except Exception:
        return None
    if len(data) < 2000:          # ESPN serves a tiny placeholder when it has no photo
        return None
    try:
        return Image.open(BytesIO(data)).convert("RGBA")
    except Exception:
        return None


def to_portrait(img: Image.Image, size: int = SIZE) -> Image.Image:
    """Crop to the head and shoulders the way the game's own portraits are framed."""
    bbox = img.getchannel("A").getbbox() or (0, 0, *img.size)
    left, top, right, bottom = bbox
    subject_h = bottom - top
    centre_x = (left + right) // 2

    # a square as tall as the subject, centred on him, anchored just above the head
    side = int(subject_h * 1.02)
    pad = int(subject_h * 0.04)
    box = (centre_x - side // 2, max(0, top - pad),
           centre_x + side // 2, max(0, top - pad) + side)

    canvas = Image.new("RGBA", (side, side), (255, 255, 255, 0))
    crop = img.crop(box)
    canvas.paste(crop, (0, 0), crop)
    return canvas.resize((size, size), Image.LANCZOS)


class PortraitStore:
    """The Mods/Player Portraits folder, with a backup taken before anything is replaced."""

    def __init__(self, mods_root: Path, backup_root: Path):
        self.root = Path(mods_root) / "Player Portraits"
        self.backup = Path(backup_root) / ("portraits-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
        self._backed_up: set[Path] = set()

    def exists(self) -> bool:
        return self.root.is_dir()

    def skin_dirs(self, skin: str) -> list[Path]:
        base = self.root / f"Skin Tone {skin}"
        return [base / "Large", base / "Small"] if base.is_dir() else []

    def available_ids(self, skin: str) -> list[int]:
        dirs = self.skin_dirs(skin)
        if not dirs:
            return []
        return sorted(int(p.stem) for p in dirs[0].glob("*.png") if p.stem.isdigit())

    def write(self, skin: str, portrait_id: int, image: Image.Image) -> list[Path]:
        written = []
        for d in self.skin_dirs(skin):
            d.mkdir(parents=True, exist_ok=True)
            dest = d / f"{portrait_id}.png"
            if dest.exists() and dest not in self._backed_up:
                rel = dest.relative_to(self.root)
                (self.backup / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dest, self.backup / rel)
                self._backed_up.add(dest)
            image.save(dest, "PNG")
            written.append(dest)
        return written


class Ledger:
    """Which portrait slot belongs to which player, keyed by ESPN id.

    Without this, a sync that adds a signing hands him a recycled portrait id and he
    plays the week wearing a team-mate's face. The ledger lets a player keep his own
    slot across syncs, and tells us who still needs one.
    """

    def __init__(self, path: Path):
        self.path = Path(path)
        self.data: dict[str, dict] = {}
        if self.path.exists():
            import json
            self.data = json.loads(self.path.read_text(encoding="utf-8"))

    def get(self, espn_id: str) -> dict | None:
        return self.data.get(str(espn_id))

    def set(self, espn_id: str, skin: str, portrait_id: int) -> None:
        self.data[str(espn_id)] = {"skin": str(skin), "id": int(portrait_id)}

    def taken(self, skin: str) -> set[int]:
        return {e["id"] for e in self.data.values() if e["skin"] == str(skin)}

    def save(self) -> None:
        import json
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True), encoding="utf-8")


def next_free_id(store: "PortraitStore", ledger: Ledger, skin: str) -> int:
    on_disk = store.available_ids(skin)
    return max(max(on_disk, default=0), max(ledger.taken(skin), default=0)) + 1
