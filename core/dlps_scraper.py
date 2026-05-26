"""
core/dlps_scraper.py
--------------------
dlpsgame.com scraping for the app. A wrapper around the user's own
dlpsgame_ps5_scraper.py — it imports that module and drives its functions
(fetch, parse_game_post, extract_post_urls, detect_last_page) so the heavy
logic lives in one place and stays in sync.

Unlike the exFAT scraper, dlpsgame:
  * uses cloudscraper, not a browser — no Chromium needed
  * serves already-resolved host links — no link-lock decryption needed

Three modes (see scrape_library's `mode`):
  * "full"   — scrape every game.
  * "new"    — scrape only post URLs not already in the library.
  * "update" — scrape new games AND re-fetch games on the most recent
               category pages, replacing a stored game only if its content
               actually changed (fingerprint comparison). This catches new
               links / versions added to existing posts — dlpsgame surfaces
               freshly-updated games on the early pages, so a shallow recheck
               finds nearly all of them.
"""
from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path
from typing import Callable


# In update mode, how many of the most-recent category pages to re-fetch
# so that new links / versions on EXISTING games are caught.
#   0  = re-check ALL pages (every game) — most thorough, slowest;
#        an Update then takes about as long as a full Scrape.
#   N  = re-check only the first N pages — faster; relies on dlpsgame
#        bumping freshly-updated games toward the front (it usually does).
# Set to 0 per the chosen behaviour: every Update re-checks the whole library.
UPDATE_RECHECK_PAGES = 0


def _load_user_scraper():
    """Import the bundled dlpsgame_ps5_scraper.py as a module."""
    # Force headless so no Chromium window pops up when run from the app.
    import os
    os.environ["DLPS_HEADLESS"] = "1"

    here = Path(__file__).resolve().parent
    candidates = [
        here / "dlpsgame_ps5_scraper.py",
        here.parent / "dlpsgame_ps5_scraper.py",
    ]
    if getattr(sys, "frozen", False):
        meipass = Path(getattr(sys, "_MEIPASS", ""))
        candidates.insert(0, meipass / "dlpsgame_ps5_scraper.py")
        candidates.insert(1, meipass / "core" / "dlpsgame_ps5_scraper.py")

    for path in candidates:
        if path.is_file():
            spec = importlib.util.spec_from_file_location(
                "dlpsgame_ps5_scraper", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
    raise FileNotFoundError(
        "dlpsgame_ps5_scraper.py was not found next to the app. "
        "It must be bundled alongside core/.")


# ---------------------------------------------------------------------------
# Page-aware URL gathering — walk category pages, remember which page each
# post URL came from (so update mode can target the recent pages).

def _gather_urls_by_page(S, max_pages: int, report) -> list:
    """Return [(page_number, post_url), ...] in page order, deduplicated.

    A thin reimplementation of the user script's gather_post_urls that keeps
    the page number — the user's version flattens it away.
    """
    report("Finding game pages...", 0, 0)
    html = S.fetch(S.CATEGORY_URL)
    if html is None:
        raise RuntimeError("Could not load the dlpsgame PS5 category page.")

    last = S.detect_last_page(html)
    if max_pages:
        last = min(last, max_pages)

    out = []
    seen = set()
    for u in S.extract_post_urls(html):
        if u not in seen:
            seen.add(u)
            out.append((1, u))
    report(f"Page 1: {len(out)} games", 1, last)

    for page in range(2, last + 1):
        time.sleep(S._request_delay())
        page_url = S.PAGE_URL.format(page=page)
        html = S.fetch(page_url)
        if html is None:
            report(f"Page {page}: unavailable, skipping", page, last)
            continue
        added = 0
        for u in S.extract_post_urls(html):
            if u not in seen:
                seen.add(u)
                out.append((page, u))
                added += 1
        report(f"Page {page}: +{added} ({len(out)} total)", page, last)
    return out


# ---------------------------------------------------------------------------
# Fingerprint — used in update mode to tell if a re-fetched game changed.

def fingerprint(game: dict) -> str:
    """A signature of the things that matter about a dlpsgame game, so we can
    tell whether a re-fetched copy actually differs from the stored one.

    Covers version, size, counts, and the full set of download links."""
    parts = [
        str(game.get("version", "")),
        str(game.get("size", "")),
        str(game.get("region", "")),
        str(game.get("package_count", "")),
        str(game.get("dlc_count", "")),
        str(game.get("backport_count", "")),
    ]
    # Every link, sorted, so a new/changed/removed link flips the fingerprint.
    links = []
    flat = game.get("links") or {}
    for host, urls in flat.items():
        for u in urls or []:
            links.append(f"{host}|{u}")
    parts.append("~".join(sorted(links)))
    return "::".join(parts)


# ---------------------------------------------------------------------------
# Main entry point.

def scrape_library(progress: Callable[[str, int, int], None] | None = None,
                   mode: str = "full",
                   max_pages: int = 0,
                   existing_games: list | None = None) -> dict:
    """Scrape dlpsgame.com.

    mode:
      "full"   — scrape everything; returns all games.
      "new"    — scrape only post URLs not in existing_games.
      "update" — scrape new games + re-fetch the recent category pages and
                 replace any existing game whose content changed.

    Returns {"games": [...], "stats": {...}}.
      * full/new: "games" is the freshly-scraped set (caller merges).
      * update:   "games" is the COMPLETE merged library, ready to save.
    """
    def report(msg, cur=0, tot=0):
        if progress:
            try:
                progress(msg, cur, tot)
            except Exception:
                pass

    S = _load_user_scraper()
    existing_games = existing_games or []
    by_url = {g.get("url", ""): g for g in existing_games if g.get("url")}

    # ---- gather post URLs (page-aware) ----
    page_urls = _gather_urls_by_page(S, max_pages, report)

    # ---- decide which URLs to (re)scrape ----
    to_scrape = []        # URLs we will fetch
    recheck = set()       # known URLs being re-fetched for changes

    if mode == "full":
        to_scrape = [u for _p, u in page_urls]
    elif mode == "new":
        to_scrape = [u for _p, u in page_urls if u not in by_url]
    elif mode == "update":
        # 0 means "re-check every page"; N means "first N pages only".
        for page, u in page_urls:
            if u not in by_url:
                to_scrape.append(u)                       # brand-new game
            elif UPDATE_RECHECK_PAGES == 0 or page <= UPDATE_RECHECK_PAGES:
                to_scrape.append(u)                       # re-check for changes
                recheck.add(u)
            # else: known + outside recheck range -> skip
    else:
        to_scrape = [u for _p, u in page_urls]

    total = len(to_scrape)
    new_count = sum(1 for u in to_scrape if u not in by_url)
    report(f"{total} pages to fetch "
           f"({new_count} new, {len(recheck)} rechecked)", 0, total)

    # ---- fetch + parse ----
    fresh = {}      # url -> freshly scraped game
    changed = 0
    try:
        for i, url in enumerate(to_scrape, 1):
            time.sleep(S._request_delay())
            html = S.fetch(url)
            if not html:
                report(f"[{i}/{total}] unreachable, skipped", i, total)
                continue
            try:
                game = S.parse_game_post(url, html)
                game["source"] = "dlpsgame"
                fresh[url] = game
                tag = ""
                if url in recheck:
                    if fingerprint(game) != fingerprint(by_url[url]):
                        tag = "  (updated)"
                        changed += 1
                    else:
                        tag = "  (no change)"
                report(f"[{i}/{total}] {game.get('title','?')}{tag}",
                       i, total)
            except Exception as e:
                report(f"[{i}/{total}] parse error: {e}", i, total)
    finally:
        try:
            S.shutdown_backends()
        except Exception:
            pass

    # ---- assemble the result ----
    if mode == "update":
        # Build the complete merged library: keep existing games, swap in
        # any that changed, append brand-new ones.
        merged = []
        added = 0
        for g in existing_games:
            u = g.get("url", "")
            if u in fresh and u in recheck:
                merged.append(fresh[u])      # replace with re-fetched copy
            else:
                merged.append(g)             # unchanged — keep as-is
        for u, g in fresh.items():
            if u not in by_url:
                merged.append(g)             # brand-new game
                added += 1
        return {
            "games": merged,
            "stats": {"new": added, "changed": changed,
                      "rechecked": len(recheck), "total": len(merged)},
        }

    # full / new: just return what we scraped
    games = list(fresh.values())
    return {
        "games": games,
        "stats": {"new": len(games), "changed": 0,
                  "rechecked": 0, "total": len(games)},
    }
