"""Check GitHub for a newer build, and replace this exe with it.

Windows will not let a running exe overwrite itself, so the update is staged into a temp
folder and a small script does the swap after we exit, then starts the new one. Files the
user owns - config.json, their team folder matches, their manual overrides - are never
touched.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from . import __version__, config
from .http import UA

API = "https://api.github.com/repos/{repo}/releases/latest"

# Refreshed by an update because the tool owns them; anything else in mapping/ is yours.
TOOL_OWNED = ("attributes.json", "positions.json", "squad.json", "abilities.json")
NEVER_TOUCH = ("config.json", "teams.json", "overrides.json")


def _version_tuple(text: str) -> tuple:
    cleaned = "".join(c if c.isdigit() or c == "." else " " for c in text.replace("v", " "))
    parts = [int(p) for p in cleaned.replace(".", " ").split() if p.isdigit()]
    return tuple(parts) or (0,)


def is_newer(tag: str, current: str = __version__) -> bool:
    return _version_tuple(tag) > _version_tuple(current)


def latest_release(cfg: dict | None = None, timeout: int = 15) -> dict | None:
    """{'tag', 'url', 'name', 'notes'} for the newest release, or None if the check fails."""
    cfg = cfg or config.load()
    repo = cfg.get("update_repo") or "lionsfan60/active-roster-update"
    req = urllib.request.Request(API.format(repo=repo),
                                 headers={"User-Agent": UA, "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except Exception:
        return None
    assets = [a for a in data.get("assets", []) if a.get("name", "").lower().endswith(".zip")]
    if not assets:
        return None
    return {
        "tag": data.get("tag_name", ""),
        "name": assets[0].get("name", ""),
        "url": assets[0].get("browser_download_url", ""),
        "notes": (data.get("body") or "")[:2000],
    }


def check(cfg: dict | None = None) -> dict | None:
    """The newest release, but only if it is newer than what is running."""
    release = latest_release(cfg)
    if release and is_newer(release["tag"]):
        return release
    return None


def download(release: dict, on_progress=None, timeout: int = 180) -> Path:
    dest = Path(tempfile.gettempdir()) / "activerosterupdate-update.zip"
    req = urllib.request.Request(release["url"], headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp, dest.open("wb") as fh:
        total = int(resp.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = resp.read(262144)
            if not chunk:
                break
            fh.write(chunk)
            got += len(chunk)
            if on_progress:
                on_progress(got, total)
    return dest


def stage(zip_path: Path) -> Path:
    staged = Path(tempfile.gettempdir()) / "activerosterupdate-staged"
    if staged.exists():
        import shutil
        shutil.rmtree(staged, ignore_errors=True)
    staged.mkdir(parents=True)
    with zipfile.ZipFile(zip_path) as z:
        z.extractall(staged)
    zip_path.unlink(missing_ok=True)
    return staged


def _install_dir() -> Path:
    return Path(sys.executable).parent if getattr(sys, "frozen", False) else config.ROOT


def apply_and_restart(staged: Path) -> None:
    """Swap the files in after we exit, then start the new build.

    The running exe is locked, so a detached script waits for this process to go away and
    does the copy itself.
    """
    target = _install_dir()
    exe = Path(sys.executable).name if getattr(sys, "frozen", False) else "ActiveRosterUpdate.exe"

    lines = [
        "@echo off",
        "ping 127.0.0.1 -n 4 >nul",
        f'copy /Y "{staged}\\{exe}" "{target}\\{exe}" >nul',
    ]
    for name in TOOL_OWNED:
        src = staged / "mapping" / name
        if src.exists():
            lines.append(f'copy /Y "{src}" "{target}\\mapping\\{name}" >nul')
    for name in ("START HERE.txt", "README.txt"):
        if (staged / name).exists():
            lines.append(f'copy /Y "{staged}\\{name}" "{target}\\{name}" >nul')
    lines += [
        f'start "" "{target}\\{exe}"',
        'del "%~f0"',
    ]

    script = Path(tempfile.gettempdir()) / "activerosterupdate-apply.cmd"
    script.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")

    flags = 0x00000008 | 0x08000000          # DETACHED_PROCESS | CREATE_NO_WINDOW
    subprocess.Popen(["cmd", "/c", str(script)], creationflags=flags, close_fds=True)
    os._exit(0)
