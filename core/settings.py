"""
core/settings.py
----------------
Load and save persistent app settings (API keys, preferences, paths).

Settings are stored as settings.json next to the exe (in data_dir).

Default values are always returned even if the file doesn't exist yet,
so callers never need to guard against missing keys.
"""
from __future__ import annotations

import json
from pathlib import Path


DEFAULTS: dict = {
    # API keys
    "rawg_api_key": "",
    "igdb_client_id": "",
    "igdb_token": "",
    # Paths
    "covers_folder": "",          # blank = auto (data_dir/covers)
    # Behaviour
    "auto_enrich": True,          # enrich metadata automatically on scrape
    "prefer_cached_covers": True, # use cached cover even if remote is available
    "offline_mode": False,        # block all network calls
    "download_covers_on_scrape": True,
    # Misc
    "theme": "dark",
    "default_view": "grid",       # grid | list
    "games_per_page": 12,
}


def _path(data_dir: Path) -> Path:
    return data_dir / "settings.json"


def load(data_dir: Path) -> dict:
    """Load settings, merging with defaults so new keys always exist."""
    path = _path(data_dir)
    settings = dict(DEFAULTS)
    if path.exists():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update(stored)
        except Exception:
            pass
    return settings


def save(data_dir: Path, settings: dict) -> bool:
    """Save settings to disk. Returns True on success."""
    path = _path(data_dir)
    try:
        merged = dict(DEFAULTS)
        merged.update(settings)
        path.write_text(json.dumps(merged, indent=2, ensure_ascii=False),
                        encoding="utf-8")
        return True
    except Exception:
        return False


def get_covers_dir(settings: dict, data_dir: Path) -> Path:
    """Return the covers directory, creating it if needed."""
    custom = (settings.get("covers_folder") or "").strip()
    if custom:
        p = Path(custom)
    else:
        p = data_dir / "covers"
    p.mkdir(parents=True, exist_ok=True)
    return p
