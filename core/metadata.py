"""
core/metadata.py
----------------
Enriches scraped game records with metadata from external APIs.

SCRAPED DATA IS NEVER OVERWRITTEN — this module only adds/updates the
'enrichment' sub-dict inside each game record.

Supported sources (tried in order):
  1. RAWG API      — descriptions, ratings, genres, screenshots, release date,
                     publisher, developer, Metacritic score.
  2. IGDB API      — covers, screenshots, summaries, genres, release dates.
                     Requires Twitch Client-ID + token (OAuth).
  3. Steam Web API — fallback for PC-matching titles (no key needed for search).

Public API
----------
  enrich_game(game, settings) -> dict
      Fetch metadata for one game. Mutates the 'enrichment' key in-place.
      Returns the enrichment dict.

  batch_enrich(games, settings, progress_fn) -> int
      Enrich all games missing metadata. Returns count enriched.

Settings dict keys
------------------
  rawg_api_key      str   RAWG API key (optional, higher rate limits)
  igdb_client_id    str   Twitch/IGDB Client-ID
  igdb_token        str   Twitch OAuth token
  auto_enrich       bool  Enrich on scrape
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from typing import Callable

# Edition / platform suffixes to strip before matching
_STRIP_PATTERNS = re.compile(
    r"\b(ultimate|deluxe|complete|definitive|standard|premium|gold|digital|"
    r"remastered|remake|enhanced|expanded|anniversary|game of the year|goty|"
    r"directors? cut|extended|special|limited|collectors?|bundle|edition|"
    r"ps5|ps4|playstation\s*5|playstation\s*4|ps\s*5|ps\s*4|"
    r"dlc|expansion|pack|season pass|upgrade|content)\b",
    re.IGNORECASE,
)

_MULTI_SPACE = re.compile(r"\s{2,}")


def clean_title(title: str) -> str:
    """Strip edition/platform noise for better API matching."""
    t = _STRIP_PATTERNS.sub(" ", title or "")
    t = _MULTI_SPACE.sub(" ", t).strip()
    # Remove trailing/leading punctuation
    t = re.sub(r"^[\s:\-–—]+|[\s:\-–—]+$", "", t)
    return t


# --- HTTP helpers ------------------------------------------------------------

def _get_json(url: str, headers: dict | None = None, timeout: int = 10) -> dict | list | None:
    try:
        req = urllib.request.Request(url, headers=headers or {})
        req.add_header("User-Agent", "exFAT-Ripper/2.0")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


def _post_json(url: str, body: bytes, headers: dict, timeout: int = 10) -> dict | list | None:
    try:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        req.add_header("User-Agent", "exFAT-Ripper/2.0")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception:
        return None


# --- RAWG --------------------------------------------------------------------

def _rawg_search(title: str, api_key: str | None) -> dict | None:
    """Search RAWG and return the best matching result dict, or None."""
    q = urllib.parse.urlencode({"search": title, "page_size": 5})
    key_param = f"&key={api_key}" if api_key else ""
    url = f"https://api.rawg.io/api/games?{q}{key_param}"
    data = _get_json(url)
    if not data or not isinstance(data.get("results"), list):
        return None
    results = data["results"]
    if not results:
        return None
    # Simple scoring: exact or near match wins
    clean = clean_title(title).lower()
    best = None
    best_score = -1
    for r in results:
        name = (r.get("name") or "").lower()
        score = 0
        if name == clean:
            score = 100
        elif clean in name or name in clean:
            score = 50
        else:
            # word overlap ratio
            cw = set(clean.split())
            nw = set(name.split())
            if cw and nw:
                score = int(100 * len(cw & nw) / max(len(cw), len(nw)))
        if score > best_score:
            best_score = score
            best = r
    if best_score < 20:
        return None   # too poor a match
    return best


def _rawg_detail(slug: str, api_key: str | None) -> dict | None:
    key_param = f"?key={api_key}" if api_key else ""
    url = f"https://api.rawg.io/api/games/{slug}{key_param}"
    return _get_json(url)


def _rawg_screenshots(slug: str, api_key: str | None) -> list[str]:
    key_param = f"?key={api_key}" if api_key else ""
    url = f"https://api.rawg.io/api/games/{slug}/screenshots{key_param}"
    data = _get_json(url)
    if not data:
        return []
    return [s["image"] for s in (data.get("results") or []) if s.get("image")][:6]


def fetch_rawg(title: str, settings: dict) -> dict | None:
    """
    Fetch enrichment from RAWG. Returns a normalised dict or None.
    """
    api_key = (settings.get("rawg_api_key") or "").strip() or None
    clean = clean_title(title)
    result = _rawg_search(clean, api_key)
    if not result:
        # Try original title
        result = _rawg_search(title, api_key)
    if not result:
        return None

    slug = result.get("slug", "")
    detail = _rawg_detail(slug, api_key) if slug else result
    if not detail:
        detail = result

    screenshots = _rawg_screenshots(slug, api_key) if slug else []

    genres = [g["name"] for g in (detail.get("genres") or [])]
    devs = [d["name"] for d in (detail.get("developers") or [])]
    pubs = [p["name"] for p in (detail.get("publishers") or [])]

    return {
        "source": "rawg",
        "api_id": detail.get("id"),
        "slug": slug,
        "description": detail.get("description_raw") or detail.get("description") or "",
        "rating": detail.get("rating"),
        "rating_count": detail.get("ratings_count"),
        "metacritic": detail.get("metacritic"),
        "release_date": detail.get("released"),
        "genres": genres,
        "developer": ", ".join(devs),
        "publisher": ", ".join(pubs),
        "cover_url": (detail.get("background_image") or ""),
        "screenshots": screenshots,
        "website": detail.get("website") or "",
        "playtime": detail.get("playtime"),
    }


# --- IGDB --------------------------------------------------------------------

def _igdb_token(client_id: str, client_secret: str) -> str | None:
    """Obtain a Twitch OAuth token for IGDB access."""
    url = (
        f"https://id.twitch.tv/oauth2/token"
        f"?client_id={urllib.parse.quote(client_id)}"
        f"&client_secret={urllib.parse.quote(client_secret)}"
        f"&grant_type=client_credentials"
    )
    data = _post_json(url, b"", {"Content-Type": "application/x-www-form-urlencoded"})
    if data and "access_token" in data:
        return data["access_token"]
    return None


def fetch_igdb(title: str, settings: dict) -> dict | None:
    """
    Fetch enrichment from IGDB. Requires client_id + token in settings.
    Returns a normalised dict or None.
    """
    client_id = (settings.get("igdb_client_id") or "").strip()
    token = (settings.get("igdb_token") or "").strip()
    if not client_id or not token:
        return None

    headers = {
        "Client-ID": client_id,
        "Authorization": f"Bearer {token}",
        "Content-Type": "text/plain",
    }
    clean = clean_title(title)
    query = (
        f'search "{clean}"; '
        f'fields name,summary,cover.url,genres.name,first_release_date,'
        f'involved_companies.company.name,involved_companies.developer,'
        f'involved_companies.publisher,rating,rating_count,screenshots.url;'
        f'limit 3;'
    )
    data = _post_json(
        "https://api.igdb.com/v4/games",
        query.encode(),
        headers,
    )
    if not data or not isinstance(data, list) or not data:
        return None

    r = data[0]
    devs = [c["company"]["name"] for c in (r.get("involved_companies") or [])
            if c.get("developer") and c.get("company")]
    pubs = [c["company"]["name"] for c in (r.get("involved_companies") or [])
            if c.get("publisher") and c.get("company")]
    genres = [g["name"] for g in (r.get("genres") or [])]
    cover = ""
    if r.get("cover", {}).get("url"):
        cover = "https:" + r["cover"]["url"].replace("t_thumb", "t_cover_big")
    screenshots = []
    for ss in (r.get("screenshots") or [])[:6]:
        if ss.get("url"):
            screenshots.append("https:" + ss["url"].replace("t_thumb", "t_screenshot_big"))

    release = None
    ts = r.get("first_release_date")
    if ts:
        import datetime
        try:
            release = datetime.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
        except Exception:
            pass

    return {
        "source": "igdb",
        "api_id": r.get("id"),
        "description": r.get("summary") or "",
        "rating": round(r["rating"] / 10, 1) if r.get("rating") else None,
        "rating_count": r.get("rating_count"),
        "metacritic": None,
        "release_date": release,
        "genres": genres,
        "developer": ", ".join(devs),
        "publisher": ", ".join(pubs),
        "cover_url": cover,
        "screenshots": screenshots,
        "website": "",
        "playtime": None,
    }


# --- Steam fallback ----------------------------------------------------------

def fetch_steam(title: str, _settings: dict) -> dict | None:
    """
    Basic Steam store search fallback. No API key required.
    Only used if RAWG and IGDB return nothing.
    """
    q = urllib.parse.urlencode({"term": clean_title(title), "l": "en"})
    url = f"https://store.steampowered.com/api/storesearch/?{q}&cc=US"
    data = _get_json(url)
    if not data or not data.get("items"):
        return None
    item = data["items"][0]
    appid = item.get("id")
    if not appid:
        return None

    detail_url = f"https://store.steampowered.com/api/appdetails?appids={appid}&cc=US&l=en"
    detail_data = _get_json(detail_url)
    if not detail_data:
        return None
    app = (detail_data.get(str(appid)) or {}).get("data") or {}
    if not app:
        return None

    genres = [g["description"] for g in (app.get("genres") or [])]
    devs = app.get("developers") or []
    pubs = app.get("publishers") or []
    screenshots = [
        s.get("path_full") for s in (app.get("screenshots") or [])[:6]
        if s.get("path_full")
    ]
    cover = app.get("header_image") or ""

    return {
        "source": "steam",
        "api_id": appid,
        "description": re.sub(r"<[^>]+>", " ", app.get("short_description") or ""),
        "rating": None,
        "rating_count": None,
        "metacritic": (app.get("metacritic") or {}).get("score"),
        "release_date": (app.get("release_date") or {}).get("date"),
        "genres": genres,
        "developer": ", ".join(devs),
        "publisher": ", ".join(pubs),
        "cover_url": cover,
        "screenshots": screenshots,
        "website": app.get("website") or "",
        "playtime": None,
    }


# --- Orchestrator ------------------------------------------------------------

def enrich_game(game: dict, settings: dict) -> dict:
    """
    Fetch enrichment metadata for one game.

    Mutates game["enrichment"] in-place and returns the enrichment dict.
    Scraped fields (firmware, backport, links, etc.) are never touched.
    """
    title = game.get("scraped_title") or game.get("title") or ""
    if not title:
        return {}

    existing = game.get("enrichment") or {}
    # Don't re-fetch if we already have a good description
    if existing.get("description") and existing.get("source"):
        return existing

    enrichment = None

    # 1. RAWG
    try:
        enrichment = fetch_rawg(title, settings)
        time.sleep(0.3)
    except Exception:
        pass

    # 2. IGDB fallback
    if not enrichment:
        try:
            enrichment = fetch_igdb(title, settings)
            time.sleep(0.3)
        except Exception:
            pass

    # 3. Steam fallback
    if not enrichment:
        try:
            enrichment = fetch_steam(title, settings)
            time.sleep(0.5)
        except Exception:
            pass

    if enrichment:
        game["enrichment"] = enrichment
        # Promote cover_url to top-level for cover cache
        if not game.get("cover") and enrichment.get("cover_url"):
            game["cover"] = enrichment["cover_url"]
        return enrichment

    return {}


def batch_enrich(
    games: list[dict],
    settings: dict,
    progress_fn: Callable[[str, int, int], None] | None = None,
) -> int:
    """
    Enrich all games that lack metadata.
    Returns count of games enriched.
    """
    total = len(games)
    enriched = 0
    for i, game in enumerate(games):
        existing = game.get("enrichment") or {}
        if existing.get("description") and existing.get("source"):
            continue
        title = game.get("scraped_title") or game.get("title") or ""
        if progress_fn:
            progress_fn(f"Enriching {i + 1}/{total}: {title}", i + 1, total)
        result = enrich_game(game, settings)
        if result:
            enriched += 1
        time.sleep(0.2)
    return enriched
