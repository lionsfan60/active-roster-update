"""Tiny cached HTTP/JSON client - stdlib only, no pip installs required."""
from __future__ import annotations

import gzip
import hashlib
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

# ESPN's edge rejects unfamiliar/custom User-Agents with a 403 (a bare "activerosterupdate/0.1"
# is refused, "curl/..." and "python-urllib/..." are served). Keep a plain client UA.
UA = "curl/8.7.1"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"


def _cache_path(url: str) -> Path:
    return CACHE_DIR / (hashlib.sha1(url.encode()).hexdigest() + ".json.gz")


def get_json(url: str, *, ttl: int = 3600, retries: int = 3, timeout: int = 30) -> dict:
    """GET a JSON document, memoised on disk for `ttl` seconds (ttl=0 disables the cache)."""
    cp = _cache_path(url)
    if ttl and cp.exists() and (time.time() - cp.stat().st_mtime) < ttl:
        with gzip.open(cp, "rt", encoding="utf-8") as fh:
            return json.load(fh)

    last: Exception | None = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            with gzip.open(cp, "wt", encoding="utf-8") as fh:
                json.dump(data, fh)
            return data
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            last = exc
            time.sleep(1.5 * (attempt + 1))

    # network died - fall back to a stale cache rather than failing the whole sync
    if cp.exists():
        with gzip.open(cp, "rt", encoding="utf-8") as fh:
            return json.load(fh)
    raise RuntimeError(f"GET failed after {retries} tries: {url} ({last})")
