#!/usr/bin/env python3
"""
dlpsgame_ps5_scraper.py
-----------------------
Scrapes every PS5 game post from dlpsgame.com and produces a JSON file
containing per-game metadata plus all download host links.

Per-game fields:
    title, url, version, size, region, genre, release_date, description,
    links: { mediafire: [...], "1fichier": [...], pixeldrain: [...], ... },
    all_external_links: [...]   # safety net for anything we didn't classify

Usage:
    pip install cloudscraper beautifulsoup4
    python dlpsgame_ps5_scraper.py            # scrapes all pages
    python dlpsgame_ps5_scraper.py --pages 3  # first 3 pages only (testing)
    python dlpsgame_ps5_scraper.py --resume   # skip games already in output

Uses cloudscraper to transparently solve Cloudflare's JS challenge that
blocks plain requests. If a future Cloudflare update breaks this, swap
in Playwright (see README).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import cloudscraper
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://dlpsgame.com"
CATEGORY_URL = f"{BASE_URL}/category/ps5/"
PAGE_URL = f"{BASE_URL}/category/ps5/page/{{page}}/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

REQUEST_TIMEOUT = 45
# Adaptive request delay. Starts brisk; if the site returns a rate-limit
# response the delay ramps up, and it eases back down after clean runs.
# This avoids paying a flat 2.5s on every one of ~580 games (~24 min of
# pure waiting) while still backing off when the site actually pushes back.
DELAY_MIN = 0.8
DELAY_MAX = 4.0
DELAY_START = 1.2
_current_delay = DELAY_START          # mutated at runtime by _note_*()
_clean_streak = 0
MAX_RETRIES = 3
RETRY_BACKOFF = 2


def _request_delay() -> float:
    """The delay to wait before the next request, in seconds."""
    return _current_delay


def _note_rate_limited() -> None:
    """Call when the site returns 403/429/503 — ramp the delay up."""
    global _current_delay, _clean_streak
    _clean_streak = 0
    _current_delay = min(DELAY_MAX, _current_delay * 1.6)


def _note_ok() -> None:
    """Call after a clean fetch — ease the delay back down after a streak."""
    global _current_delay, _clean_streak
    _clean_streak += 1
    if _clean_streak >= 8 and _current_delay > DELAY_MIN:
        _current_delay = max(DELAY_MIN, _current_delay * 0.85)
        _clean_streak = 0


# Kept for backwards-compat with any code referencing the old constant.
DELAY_BETWEEN_REQUESTS = DELAY_START

# When we hit 429 and exhaust retries, sleep this long before resuming.
# Cloudflare's per-IP rate window typically clears within a few minutes;
# we ramp up from 60s on the first cool-off to 5 min on later ones.
RATE_LIMIT_COOLOFF_SECONDS = [60, 180, 300, 600]
RATE_LIMIT_MAX_COOLOFFS = len(RATE_LIMIT_COOLOFF_SECONDS)

OUTPUT_FILE = "ps5_games.json"

# Hosts we recognise -> category key in output. Order matters: more specific first.
HOST_PATTERNS: list[tuple[str, str]] = [
    ("mediafire.com",    "mediafire"),
    ("1fichier.com",     "1fichier"),
    ("pixeldrain.com",   "pixeldrain"),
    ("pixeldrain.net",   "pixeldrain"),
    ("gofile.io",        "gofile"),
    ("mega.nz",          "mega"),
    ("mega.co.nz",       "mega"),
    ("rapidgator.net",   "rapidgator"),
    ("nitroflare.com",   "nitroflare"),
    ("uploadhaven.com",  "uploadhaven"),
    ("krakenfiles.com",  "krakenfiles"),
    ("filecrypt.cc",     "filecrypt"),
    ("filecrypt.co",     "filecrypt"),
    ("buzzheavier.com",  "buzzheavier"),
    ("send.cm",          "send.cm"),
    ("workupload.com",   "workupload"),
    ("ddownload.com",    "ddownload"),
    ("dailyuploads.net", "dailyuploads"),
    ("turbobit.net",     "turbobit"),
    ("fikper.com",       "fikper"),
    ("hexload.com",      "hexload"),
    ("vikingfile.com",   "vikingfile"),
    ("datanodes.to",     "datanodes"),
    ("rootz.so",         "rootz"),
    ("xrel.to",          "xrel"),
    ("akirabox.to",      "akirabox"),
    ("akirabox.com",     "akirabox"),
    ("ranoz.gg",         "ranoz"),
    ("transfer.it",      "transfer.it"),
    ("drive.google.com", "gdrive"),
    ("ouo.io",           "shortener_ouo"),
    ("ouo.press",        "shortener_ouo"),
    ("linkvertise.com",  "shortener_linkvertise"),
    ("shrinkme.io",      "shortener_shrinkme"),
    ("shrinkearn.com",   "shortener_shrinkearn"),
    ("clk.sh",           "shortener_clk"),
    ("shortlinkto.",     "shortener_shortlinkto"),
    ("za.gl",            "shortener_zagl"),
    ("safelink",         "shortener_safelink"),
]

# Internal/site URLs we don't care about as "external download links"
INTERNAL_HOSTS = {"dlpsgame.com", "www.dlpsgame.com"}

# Metadata keys we try to pluck out of the post body
META_LABELS = {
    "version":      ["version", "patch"],
    "size":         ["size", "file size", "game size"],
    "region":       ["region"],
    "genre":        ["genre", "genres", "category"],
    "release_date": ["release date", "release", "released", "date release"],
    "language":     ["language", "languages"],
}


# ---------------------------------------------------------------------------
# HTTP — two backends: cloudscraper (fast) and Playwright (real browser, robust)
# ---------------------------------------------------------------------------

_BACKEND = "cloudscraper"   # set by main() based on --browser flag and runtime failures
_SCRAPER = None             # cloudscraper session
_PW = None                  # playwright instance
_PW_BROWSER = None          # playwright browser
_PW_CTX = None              # playwright browser context (persists cookies)


def _init_cloudscraper() -> None:
    global _SCRAPER
    if _SCRAPER is not None:
        return
    _SCRAPER = cloudscraper.create_scraper(
        browser={"browser": "chrome", "platform": "windows", "mobile": False},
        delay=10,
    )
    _SCRAPER.headers.update(HEADERS)


def _init_playwright() -> None:
    global _PW, _PW_BROWSER, _PW_CTX
    if _PW_CTX is not None:
        return
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("\n!! Playwright is not installed. Install it with:")
        print("       pip install playwright")
        print("       playwright install chromium\n")
        sys.exit(1)
    _PW = sync_playwright().start()

    from pathlib import Path
    profile_dir = Path.home() / ".dlpsgame_scraper_profile"
    profile_dir.mkdir(exist_ok=True)

    # When run inside the desktop app, DLPS_HEADLESS=1 is set so no browser
    # window ever pops up. The standalone script leaves this unset and keeps
    # its original visible-browser behaviour.
    import os as _os
    _headless = _os.environ.get("DLPS_HEADLESS", "") == "1"

    _PW_CTX = _PW.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=_headless,
        viewport={"width": 1280, "height": 800},
        locale="en-US",
        user_agent=HEADERS["User-Agent"],
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
    )
    _PW_BROWSER = None
    _PW_CTX.add_init_script(
        "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
    )
    print("    (playwright: chromium launched, persistent profile)")


def _fetch_cloudscraper(url: str) -> tuple[str | None, str | None]:
    """Returns (html, error). html=None means failure."""
    _init_cloudscraper()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            r = _SCRAPER.get(url, timeout=REQUEST_TIMEOUT)
            print(f"    [cloudscraper HTTP {r.status_code}, {len(r.content)} bytes] attempt {attempt}")
            if r.status_code == 404:
                return None, "404"
            if r.status_code in (403, 503, 429):
                snippet = r.text[:200].replace("\n", " ")
                print(f"    ! blocked {r.status_code}: {snippet!r}")
                _note_rate_limited()
                last_err = f"HTTP {r.status_code}"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return None, last_err
            r.raise_for_status()
            _note_ok()
            return r.text, None
        except cloudscraper.exceptions.CloudflareException as e:
            print(f"    ! cloudscraper: {type(e).__name__}: {e}")
            last_err = f"{type(e).__name__}: {e}"
        except Exception as e:
            print(f"    ! cloudscraper attempt {attempt}/{MAX_RETRIES}: {type(e).__name__}: {e}")
            last_err = f"{type(e).__name__}: {e}"
        if attempt < MAX_RETRIES:
            time.sleep(RETRY_BACKOFF * attempt)
    return None, last_err


def _fetch_playwright(url: str) -> tuple[str | None, str | None]:
    _init_playwright()
    last_err = None
    for attempt in range(1, MAX_RETRIES + 1):
        page = _PW_CTX.new_page()
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=REQUEST_TIMEOUT * 1000)
            status = resp.status if resp else 0
            # Wait for the real post body element to appear (clears CF challenge)
            try:
                page.wait_for_selector("div.entry-content, article", timeout=25000)
                status = 200
            except Exception:
                body_text = page.evaluate("document.body && document.body.innerText || ''")
                if "Just a moment" in body_text or "Enable JavaScript and cookies" in body_text:
                    pass
                elif len(body_text) > 500:
                    status = 200
            html = page.content()
            print(f"    [playwright HTTP {status}, {len(html)} bytes] attempt {attempt}")
            if status == 404:
                return None, "404"
            if status == 200 and ("entry-content" in html or "<article" in html):
                return html, None
            if status in (200, 403, 503, 429):
                # 200 with no entry-content = still on challenge page
                last_err = f"HTTP {status}"
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * attempt)
                    continue
                return None, last_err
            return html, None
        except Exception as e:
            print(f"    ! playwright attempt {attempt}/{MAX_RETRIES}: {type(e).__name__}: {e}")
            last_err = f"{type(e).__name__}: {e}"
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)
        finally:
            try:
                page.close()
            except Exception:
                pass
    return None, last_err


def _fetch_raw(url: str) -> tuple[str | None, str | None]:
    """Single fetch attempt through the active backend. Returns (html, err).

    cloudscraper is tried first for EVERY url. If it fails on a given page we
    fall back to Playwright for *that page only* — we do NOT permanently flip
    the backend, so one bad page doesn't force every remaining game through
    the slow browser path. (A page-host can succeed with cloudscraper even
    right after another failed.)
    """
    if _BACKEND == "playwright_forced":
        return _fetch_playwright(url)

    # cloudscraper-first for every page
    html, err = _fetch_cloudscraper(url)
    if html is not None:
        return html, None

    # this page needs a browser — fall back just for it
    print(f"    *** cloudscraper failed ({err}); using Playwright for this page ***")
    return _fetch_playwright(url)


# How many cool-offs we've burned this run. Persists across fetch() calls so a
# stretch of consecutive 429s ramps the wait time up instead of looping at 60s.
_COOLOFF_INDEX = 0


def fetch(url: str) -> str | None:
    """Fetch with auto-backoff on 429 (rate limit) responses.

    On 429: sleep for an increasing cool-off (60s → 3min → 5min → 10min),
    then retry once. After RATE_LIMIT_MAX_COOLOFFS consecutive 429s, give up
    on this URL and let the caller skip it. The cool-off counter resets when
    any successful fetch happens.
    """
    global _COOLOFF_INDEX
    html, err = _fetch_raw(url)
    if html is not None:
        _COOLOFF_INDEX = 0  # success resets the cool-off ramp
        return html

    # Cool off on 429 or 5xx (server overload) or timeout-like errors —
    # all signs the site is struggling and needs a breather.
    transient = False
    if err:
        if "429" in err or "503" in err or "502" in err or "504" in err or "500" in err:
            transient = True
        low = err.lower()
        if "timeout" in low or "connect" in low or "reset" in low:
            transient = True
    if transient:
        while _COOLOFF_INDEX < RATE_LIMIT_MAX_COOLOFFS:
            wait = RATE_LIMIT_COOLOFF_SECONDS[_COOLOFF_INDEX]
            _COOLOFF_INDEX += 1
            print(f"\n=== Server transient ({err}). Cool-off #{_COOLOFF_INDEX}: "
                  f"sleeping {wait}s before retrying... ===")
            _sleep_with_heartbeat(wait)
            html, err = _fetch_raw(url)
            if html is not None:
                print(f"=== Cool-off worked. Resuming. ===\n")
                _COOLOFF_INDEX = 0
                return html
            # Still transient? keep cooling. Other error? give up.
            still_transient = False
            if err:
                if "429" in err or "503" in err or "502" in err or "504" in err or "500" in err:
                    still_transient = True
                low = err.lower()
                if "timeout" in low or "connect" in low or "reset" in low:
                    still_transient = True
            if not still_transient:
                break
        print(f"\n!! Server still misbehaving after {RATE_LIMIT_MAX_COOLOFFS} cool-offs.\n"
              f"   Run again later with --resume to continue.\n")
    return None


def _sleep_with_heartbeat(seconds: int) -> None:
    """Sleep but print a dot every 15s so the user sees we're still alive."""
    waited = 0
    while waited < seconds:
        chunk = min(15, seconds - waited)
        time.sleep(chunk)
        waited += chunk
        print(f"    ...waited {waited}/{seconds}s", flush=True)


def shutdown_backends() -> None:
    global _PW, _PW_BROWSER, _PW_CTX
    try:
        if _PW_CTX is not None:
            _PW_CTX.close()
        if _PW_BROWSER is not None:
            _PW_BROWSER.close()
        if _PW is not None:
            _PW.stop()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Pagination + post discovery
# ---------------------------------------------------------------------------

def detect_last_page(html: str) -> int:
    """Detect the last page number on a paginated archive.

    First tries CSS selectors for common pagination widgets; if those find
    nothing, falls back to scanning the raw HTML for any /category/ps5/page/N/
    pattern.
    """
    soup = BeautifulSoup(html, "html.parser")
    nums: list[int] = []
    for a in soup.select("a.page-numbers, .nav-links a, .pagination a, .pages a, .wp-pagenavi a"):
        txt = a.get_text(strip=True).replace(",", "")
        if txt.isdigit():
            nums.append(int(txt))
        m = re.search(r"/page/(\d+)/", a.get("href", ""))
        if m:
            nums.append(int(m.group(1)))

    # Fallback: scan raw HTML for any /page/N/ link, regardless of widget
    for m in re.finditer(r"/page/(\d+)/", html):
        nums.append(int(m.group(1)))

    if not nums:
        print("    !! could not detect pagination; only page 1 will be scraped")
        print("       (if there are more pages, the site theme changed --")
        print("        re-run with --pages 28 to force a range)")
        return 1
    last = max(nums)
    print(f"    pagination detected: last page = {last}")
    return last


def extract_post_urls(html: str) -> list[str]:
    """Find game post URLs on a category archive page."""
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    seen = set()

    # Prefer the canonical bookmark links from article titles
    for sel in ("h2.entry-title a", "h2.post-title a", "article h2 a",
                "a[rel='bookmark']"):
        for a in soup.select(sel):
            href = a.get("href")
            if not href:
                continue
            href = urljoin(BASE_URL, href)
            parsed = urlparse(href)
            if parsed.netloc not in INTERNAL_HOSTS:
                continue
            path = parsed.path
            if any(skip in path for skip in
                   ("/category/", "/tag/", "/page/", "/author/", "/feed",
                    "/wp-content", "/wp-admin")):
                continue
            if href in seen:
                continue
            seen.add(href)
            urls.append(href)
        if urls:
            break
    return urls


# ---------------------------------------------------------------------------
# Per-post extraction
# ---------------------------------------------------------------------------

def _host_matches(host: str, pattern: str) -> bool:
    """Check if `host` is exactly `pattern` or a subdomain of `pattern`.
    Avoids substring-match bugs (e.g. 'x.com' would match 'akirabox.com').
    """
    return host == pattern or host.endswith("." + pattern)


def classify_host(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if not host or host in INTERNAL_HOSTS:
        return None
    if "downloadgameps3" in host:
        return "_aggregator"
    for pattern, key in HOST_PATTERNS:
        if _host_matches(host, pattern):
            return key
    return "other"


def text_blocks(soup: BeautifulSoup) -> str:
    """Concatenated visible text of the post content, for regex sweeps."""
    container = (
        soup.select_one("div.entry-content")
        or soup.select_one("article .post-content")
        or soup.select_one("article")
        or soup
    )
    return container.get_text("\n", strip=True)


def extract_metadata(soup: BeautifulSoup) -> dict:
    """Pull metadata from the post body.

    dlpsgame.com posts don't use rigid "Size: 50 GB" labels, so we mix:
      - regex patterns for version, size, and dates
      - label-based matching for fields that do appear that way (voice,
        screen languages, etc.)
    """
    body = text_blocks(soup)
    found: dict[str, str] = {}

    # ---- regex sweeps over the full body ----

    # Version: "(v01.000)", "v 1.05", "Patch v1.10"
    m = re.search(r"\bv\s*([0-9]+(?:\.[0-9]+){1,4})\b", body, flags=re.I)
    if m:
        found["version"] = "v" + m.group(1)

    # Size: "50 GB", "1.5 TB", "850 MB"
    m = re.search(r"\b(\d+(?:[.,]\d+)?\s*(?:GB|MB|TB|G|M)\b)", body, flags=re.I)
    if m:
        found["size"] = m.group(1).strip()

    # Region: "Region : USA" or standalone "USA", "EUR", "JPN" near "Region"
    m = re.search(r"region\s*[:\-–]?\s*([A-Z/]{2,15})\b", body, flags=re.I)
    if m:
        found["region"] = m.group(1).strip().upper()

    # Release date: "Release Date: ..." or "Released: ..."
    m = re.search(
        r"(?:release\s*date|released?)\s*[:\-–]\s*([A-Za-z0-9 ,\-/.]{4,40})",
        body, flags=re.I,
    )
    if m:
        found["release_date"] = m.group(1).strip()

    # Voice languages: "Voice : English ..." (stop before "Screen languages")
    m = re.search(
        r"voice\s*[:\-–]\s*(.+?)(?=\s*(?:screen\s*languages?|$|\n))",
        body, flags=re.I,
    )
    if m:
        found["voice_languages"] = m.group(1).strip()

    # Screen languages: "Screen languages : German, Simplified Chinese, ..."
    m = re.search(r"screen\s*languages?\s*[:\-–]\s*([^\n]{2,400})", body, flags=re.I)
    if m:
        found["screen_languages"] = m.group(1).strip()
    elif "voice_languages" in found:
        # If only Voice was given, mirror it into a general "language" field
        found["language"] = found["voice_languages"]

    # ---- label-based fallback for anything we haven't filled yet ----
    for line in body.splitlines():
        m = re.match(r"\s*([A-Za-z ]{2,30})\s*[:\-–]\s*(.+?)\s*$", line)
        if not m:
            continue
        label = m.group(1).strip().lower()
        value = m.group(2).strip()
        if not value or len(value) > 300:
            continue
        for field, aliases in META_LABELS.items():
            if field in found:
                continue
            if any(label == a or label.startswith(a) for a in aliases):
                found[field] = value
                break

    # Genre often comes from the post's WordPress category tags rather than body text.
    # Look at <a rel="category tag"> if we still don't have one.
    if "genre" not in found:
        cats = soup.select("a[rel~='category']")
        # Filter out the platform tag (PS5) and dedupe
        genres = []
        for c in cats:
            t = c.get_text(strip=True)
            if not t or t.upper() in {"PS5", "PS4", "PS3", "PS2", "PC", "SWITCH", "PSN"}:
                continue
            if t not in genres:
                genres.append(t)
        if genres:
            found["genre"] = ", ".join(genres)

    return found


def extract_description(soup: BeautifulSoup) -> str:
    """Best-effort post description: first 1-3 prose paragraphs."""
    container = (
        soup.select_one("div.entry-content")
        or soup.select_one("article .post-content")
        or soup.select_one("article")
    )
    if not container:
        return ""
    paras = []
    for p in container.find_all("p"):
        txt = p.get_text(" ", strip=True)
        if not txt or len(txt) < 40:
            continue
        # Skip nav/footer-ish boilerplate
        if "DLPSGAME.COM" in txt.upper() and len(txt) < 200:
            continue
        paras.append(txt)
        if len(paras) >= 3:
            break
    return "\n\n".join(paras)


def extract_description_rich(soup: BeautifulSoup) -> list[list[dict]]:
    """Return the first few prose paragraphs as a list of segment-arrays.

    Each paragraph is a list of segments:
      [{"t": "Game (v01.000) : "},
       {"t": "Lets", "u": "https://downloadgameps3.net/archives/..."},
       {"t": " – "},
       {"t": "Mediafire", "u": "https://www.mediafire.com/..."},
       ...]

    Shorteners are unwrapped (so a "Lets" link goes straight to its real
    destination), and noise/internal links are dropped (the anchor's text
    is kept as plain text so the sentence still reads correctly).
    """
    container = (
        soup.select_one("div.entry-content")
        or soup.select_one("article .post-content")
        or soup.select_one("article")
    )
    if not container:
        return []

    def _is_package_internal(txt: str) -> bool:
        t = txt.strip()
        if not t:
            return True
        if re.match(r"^\s*PPSA[\-_ ]?\d{3,6}\b", t, flags=re.I):
            return True
        if re.match(r"^\s*(Game|Backport|DLC|DLCs|Update|Fix|Patch)\b", t, flags=re.I):
            return True
        if re.match(r"^\s*(By\s|By[A-Z])", t):
            return True
        kv = re.match(r"^\s*([A-Za-z][A-Za-z0-9 /]{1,40})\s*[:\-–]\s*(.+)$", t)
        if kv:
            label = kv.group(1).strip().lower()
            if label in ("voice", "screen languages", "screen language",
                        "language", "languages", "password", "pasword",
                        "note", "fw required", "firmware", "fw",
                        "size", "game size", "game size after extract",
                        "file size", "region", "edition", "release date",
                        "uploader", "thanks to", "thanks", "credits"):
                return True
        if re.match(r"^\s*(Backport\s*\d|FW\s*REQUIRED|How to install|How to Play|"
                    r"\(\s*Guide Download|Game size after Extract|Note\s*:)",
                    t, flags=re.I):
            return True
        return False

    paragraphs: list[list[dict]] = []
    for p in container.find_all("p"):
        txt_preview = p.get_text(" ", strip=True)
        if not txt_preview or len(txt_preview) < 40:
            continue
        if "DLPSGAME.COM" in txt_preview.upper() and len(txt_preview) < 200:
            continue
        if _is_package_internal(txt_preview):
            continue

        segments: list[dict] = []
        for node in p.descendants:
            if getattr(node, "name", None) == "a":
                # Anchor — emit as a linked segment if the URL is keepable
                text = node.get_text(" ", strip=True)
                if not text:
                    continue
                href = (node.get("href") or "").strip()
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    segments.append({"t": text})
                    continue
                href_abs = urljoin(BASE_URL, href)
                parsed = urlparse(href_abs)
                host = parsed.netloc.lower()
                if (parsed.scheme not in ("http", "https") or
                        host in INTERNAL_HOSTS or
                        any(_host_matches(host, n) for n in NOISE_HOSTS) or
                        any(pat in parsed.path for pat in NOISE_PATH_PATTERNS)):
                    segments.append({"t": text})
                    continue
                real = unwrap(href_abs)
                real_parsed = urlparse(real)
                real_host = real_parsed.netloc.lower()
                if (real_host in INTERNAL_HOSTS or
                        any(_host_matches(real_host, n) for n in NOISE_HOSTS) or
                        any(pat in real_parsed.path for pat in NOISE_PATH_PATTERNS)):
                    segments.append({"t": text})
                else:
                    segments.append({"t": text, "u": real})
            elif getattr(node, "name", None) is None:
                # NavigableString — emit as plain text, but only if it isn't
                # already inside an anchor (whose text we just captured above).
                if node.parent is not None and node.parent.name == "a":
                    continue
                s = str(node)
                if s:
                    segments.append({"t": s})

        # Merge adjacent plain-text segments to keep the array compact
        merged: list[dict] = []
        for seg in segments:
            if "u" not in seg and merged and "u" not in merged[-1]:
                merged[-1]["t"] += seg["t"]
            else:
                merged.append(seg)
        # Collapse runs of whitespace inside plain-text segments
        for seg in merged:
            if "u" not in seg:
                seg["t"] = re.sub(r"\s+", " ", seg["t"])

        # Strip leading/trailing whitespace on the first/last text segments
        if merged and "u" not in merged[0]:
            merged[0]["t"] = merged[0]["t"].lstrip()
            if not merged[0]["t"]:
                merged.pop(0)
        if merged and "u" not in merged[-1]:
            merged[-1]["t"] = merged[-1]["t"].rstrip()
            if not merged[-1]["t"]:
                merged.pop()

        if merged:
            paragraphs.append(merged)
        if len(paragraphs) >= 5:   # a touch more than plain description
            break
    return paragraphs


# Hosts whose links are noise (social shares, image CDNs, tracking).
# We skip them entirely.
NOISE_HOSTS = (
    "twitter.com", "x.com", "facebook.com", "reddit.com", "pinterest.com",
    "whatsapp.com", "t.me", "telegram.me", "tumblr.com",
    "blogger.googleusercontent.com", "googleusercontent.com",
    "bp.blogspot.com",      # image CDN behind blogger
    "blogspot.com",
    "i.postimg.cc", "postimg.cc",
    "ibb.co",
    "imgur.com",
    "predb.net", "predb.me",
    "youtube.com", "youtu.be",
    "disqus.com",
    "playstation.com",      # store/PR pages, not downloads
    "gbatemp.net",          # forum links
    # Note: downloadgameps3.com/.net are NOT filtered. They get classified
    # as host_key="_aggregator" and resolved by resolve_aggregators.py into
    # real host URLs.
)

# Specific URL path prefixes we treat as noise even on otherwise useful hosts
# (e.g. downloadgameps3.com hosts both real game pages AND guide/help pages).
NOISE_PATH_PATTERNS = (
    "/guide-download-game",
    "/guide-troubleshooting",
    "/guide-download-max-speed",
    "/dmca",
    "/all-guide-install",
)

# Shortener URL parsers. Each takes a URL and returns the real destination
# URL if it can decode it, otherwise None.
import base64
from urllib.parse import parse_qs


# Hosts that use the Adlinkfly URL format: /full?url=<base64>
# These all stash the destination as URL-safe base64 in the `url` query param,
# so we can decode without ever loading the ad page.
ADLINKFLY_HOSTS = (
    "shrinkearn.com",
    "clk.sh",
    "shrinkme.io",
    "za.gl",
)


def _decode_adlinkfly(url: str) -> str | None:
    host = urlparse(url).netloc.lower()
    if not any(h in host for h in ADLINKFLY_HOSTS):
        return None
    q = parse_qs(urlparse(url).query)
    raw = (q.get("url") or [None])[0]
    if not raw:
        return None
    raw = raw + "=" * (-len(raw) % 4)
    try:
        decoded = base64.urlsafe_b64decode(raw).decode("utf-8", "replace")
        if decoded.startswith(("http://", "https://")):
            return decoded
    except Exception:
        pass
    return None


def _decode_ouo(url: str) -> str | None:
    # ouo.io is a redirector with no plaintext target — can't unwrap without a fetch
    return None


URL_DECODERS = [_decode_adlinkfly, _decode_ouo]


def unwrap(url: str) -> str:
    """Recursively unwrap known shortener URLs to their real destinations."""
    for _ in range(5):  # bounded depth; shorteners-of-shorteners shouldn't happen
        next_url = None
        for decoder in URL_DECODERS:
            try:
                d = decoder(url)
            except Exception:
                d = None
            if d:
                next_url = d
                break
        if not next_url or next_url == url:
            return url
        url = next_url
    return url


def extract_images(soup: BeautifulSoup) -> tuple[str, list[str]]:
    """Pull cover art + screenshots from the post.

    Returns (cover_url, screenshots[]). Cover = first image in post body.
    Screenshots = the rest. Both deduplicated, with size-tweaked Blogger URLs
    upgraded to higher resolution.
    """
    container = (
        soup.select_one("div.entry-content")
        or soup.select_one("article .post-content")
        or soup.select_one("article")
        or soup
    )

    images: list[str] = []
    seen: set[str] = set()

    for img in container.find_all("img"):
        src = (img.get("data-src") or img.get("data-lazy-src") or img.get("src") or "").strip()
        if not src:
            srcset = img.get("srcset", "")
            if srcset:
                src = srcset.split(",")[0].strip().split(" ")[0]
        if not src or src.startswith("data:"):
            continue
        src = urljoin(BASE_URL, src)
        # Skip emoji/icons/avatars/site chrome
        if any(skip in src.lower() for skip in
               ("/wp-includes/", "/themes/", "smilies", "emoji", "avatar",
                "gravatar", "logo", "icon", "spinner")):
            continue
        # Upgrade blogger thumbnails to /s1600/ where possible
        src = re.sub(r"/s\d{2,4}(/|-c/)", r"/s1600\1", src)
        if src in seen:
            continue
        seen.add(src)
        images.append(src)

    # Some posts also link to NFO image previews (api.predb.net) as anchors;
    # promote those into screenshots too.
    for a in container.find_all("a", href=True):
        href = a["href"].strip()
        if not href:
            continue
        href = urljoin(BASE_URL, href)
        lower = href.lower()
        if not lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")):
            continue
        if href in seen:
            continue
        seen.add(href)
        images.append(href)

    cover = images[0] if images else ""
    screenshots = images[1:] if len(images) > 1 else []
    return cover, screenshots


def _process_link(href: str, seen: set[str]) -> str | None:
    """Validate + unwrap + dedupe a single href. Returns the final URL or None."""
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return None
    href = urljoin(BASE_URL, href)
    parsed = urlparse(href)
    if parsed.scheme not in ("http", "https"):
        return None
    host = parsed.netloc.lower()
    if host in INTERNAL_HOSTS:
        return None
    if any(_host_matches(host, noise) for noise in NOISE_HOSTS):
        return None
    if any(p in parsed.path for p in NOISE_PATH_PATTERNS):
        return None

    real = unwrap(href)
    if real in seen:
        return None

    real_parsed = urlparse(real)
    real_host = real_parsed.netloc.lower()
    if real_host in INTERNAL_HOSTS:
        return None
    if any(_host_matches(real_host, noise) for noise in NOISE_HOSTS):
        return None
    if any(p in real_parsed.path for p in NOISE_PATH_PATTERNS):
        return None

    seen.add(real)
    return real


def _section_of(text: str) -> str:
    """Classify a paragraph as a release-section header. See enrich_metadata.py
    for the full rationale; only paragraphs of the form
    'Game (v…) : …' / 'Backport <fw> (@x) : …' / 'DLC (@x) : …' / etc. count.
    Instructional text like 'How to install Backport' or metadata key:value
    lines like 'Game size after Extract : 148GB' do NOT.
    """
    t = text.strip()
    if not t:
        return ""
    low = t.lower()
    if re.match(r"^(?:game\s+size|game\s+after|backport\s+(?:instructions?|guide|tutorial))\b", low):
        return ""
    if re.match(r"^(?:how\s+to|guide|install\s+the|to\s+install|please\s+install)", low):
        return ""
    has_colon = ":" in t[:80]

    if low.startswith("backport"):
        rest = t[len("backport"):].lstrip()
        if has_colon and (
            rest.startswith(("(", ":"))
            or re.match(r"^\d+\.x{1,2}", rest, flags=re.I)
            or re.match(r"^v\s*\d", rest, flags=re.I)
        ):
            return "backport"
        return ""

    if low.startswith(("dlcs", "dlc")):
        kw_len = 4 if low.startswith("dlcs") else 3
        rest = t[kw_len:].lstrip()
        if rest == "" or rest.startswith(":") or rest.startswith("("):
            return "dlc"
        return ""

    if low.startswith("game"):
        rest = t[4:].lstrip()
        if has_colon and (rest.startswith(("(", ":")) or re.match(r"^v\s*\d", rest, flags=re.I)):
            return "game"
        return ""

    if low.startswith("update"):
        rest = t[6:].lstrip()
        if has_colon and (rest.startswith(("(", ":")) or re.match(r"^v\s*\d", rest, flags=re.I)):
            return "update"
        return ""

    if low.startswith(("fix", "patch")):
        kw_len = 5 if low.startswith("patch") else 3
        rest = t[kw_len:].lstrip()
        if has_colon and (rest.startswith(("(", ":")) or re.match(r"^v\s*\d", rest, flags=re.I)):
            return "fix"
        return ""

    return ""


def _parse_release_header(text: str, kind: str) -> dict:
    """Parse a section's lead-line into structured release metadata.

    Examples:
      "Game (v01.003) : ..."          → {version: 'v01.003'}
      "Game (v01.003) + DLC : ..."    → {version: 'v01.003', dlc: True}
      "Backport 4.xx (@Baderlink) :"  → {firmware: '4.xx', release_group: 'Baderlink'}
      "Backport 4.xx+ 4K Patch Fixed (@Kerrdec97) :"
                                      → {firmware: '4.xx+', release_group: 'Kerrdec97',
                                         notes: '4K Patch Fixed'}
      "Backport (9.xx to 8.xx) :"     → {firmware: '9.xx to 8.xx'}
    """
    info: dict = {}
    head = text.split(":", 1)[0].strip()

    if kind == "game":
        head = re.sub(r"^Game\s*", "", head, flags=re.I)
    elif kind == "backport":
        head = re.sub(r"^Backport\s*", "", head, flags=re.I)
    elif kind == "dlc":
        head = re.sub(r"^DLCs?\s*", "", head, flags=re.I)
    elif kind == "update":
        head = re.sub(r"^Update\s*", "", head, flags=re.I)
    elif kind == "fix":
        head = re.sub(r"^(?:Fix|Patch)\s*", "", head, flags=re.I)

    if re.search(r"\+\s*DLC\b", head, flags=re.I):
        info["dlc"] = True
        head = re.sub(r"\+\s*DLC\b", "", head, flags=re.I).strip()

    vm = re.search(r"\bv\s*([0-9]+(?:\.[0-9]+){0,4})\b", head, flags=re.I)
    if vm:
        info["version"] = "v" + vm.group(1)
        head = head[:vm.start()] + head[vm.end():]

    if kind == "backport":
        fm = re.search(
            r"(\d+\.x{1,2}\+?(?:\s*(?:to|–|-)\s*\d+\.x{1,2}\+?)?)",
            head, flags=re.I,
        )
        if fm:
            info["firmware"] = fm.group(1).strip()
            head = head[:fm.start()] + head[fm.end():]

    notes: list[str] = []
    for tok in re.findall(r"\(([^()]+)\)", head):
        tok = tok.strip()
        if not tok:
            continue
        if tok.startswith("@"):
            info["release_group"] = tok.lstrip("@")
        elif re.match(r"v\s*\d", tok, flags=re.I):
            pass
        elif re.match(r"\d+\.x", tok, flags=re.I) and kind == "backport":
            pass
        else:
            notes.append(tok)
    if notes:
        info["notes"] = "; ".join(notes)
    return info


def _is_package_header(text: str) -> dict | None:
    """Detect 'PPSA##### – REGION (extras)' package header. Returns parsed
    bits or None."""
    m = re.match(
        r"^\s*PPSA[\-_ ]?(\d{3,6})\s*[\-–—:]\s*([A-Z/]{2,15})?\s*"
        r"((?:\([^\n)]+\)\s*)*)\s*$",
        text, flags=re.I,
    )
    if not m:
        return None
    out: dict = {"ppsa": "PPSA" + m.group(1).zfill(5)}
    if m.group(2):
        out["region"] = m.group(2).strip().upper()
    fmt_tokens = []
    extras = []
    for tok in [t.strip() for t in re.findall(r"\(([^()]+)\)", m.group(3) or "")]:
        low = tok.lower()
        if low in ("exfat", "fat32", "ntfs", "fat", "split", "merged", "compressed"):
            fmt_tokens.append(tok)
        elif low == "backport":
            out["is_backport_package"] = True
        elif tok.startswith("@"):
            out["release_group"] = tok.lstrip("@")
        else:
            extras.append(tok)
    if fmt_tokens:
        out["format"] = ", ".join(fmt_tokens)
    if extras:
        out["label_extras"] = ", ".join(extras)
    out["label"] = text.strip()
    return out


def _parse_kv_line(text: str) -> tuple[str, str] | None:
    m = re.match(r"\s*([A-Za-z][A-Za-z0-9 /]{1,40})\s*[:\-–]\s*(.+?)\s*$", text)
    if not m:
        return None
    label = m.group(1).strip().lower()
    value = m.group(2).strip()
    if not value or len(value) > 400:
        return None
    return label, value


def extract_packages(soup: BeautifulSoup) -> tuple[list[dict], list[str]]:
    """Walk the post body and break it into one or more release packages.

    A package starts at a 'PPSA##### – REGION (...)' header line and runs
    until the next such header. Within each package we still capture
    sectioned releases (game/backport/dlc/update/fix).
    """
    container = (
        soup.select_one("div.entry-content")
        or soup.select_one("article .post-content")
        or soup
    )

    packages: list[dict] = []
    all_external: list[str] = []
    seen: set[str] = set()
    current_pkg: dict | None = None
    current_release: dict | None = None
    seen_first_header = False

    def start_package(h: dict) -> dict:
        pkg = {
            "label": h.get("label", ""),
            "ppsa": h.get("ppsa", ""),
            "region": h.get("region", ""),
            "format": h.get("format", ""),
            "uploader": "",
            "size": "",
            "fw_required": "",
            "note": "",
            "password": "",
            "voice_languages": "",
            "screen_languages": "",
            "releases": {},
        }
        if h.get("release_group"):
            pkg["release_group"] = h["release_group"]
        if h.get("label_extras"):
            pkg["label_extras"] = h["label_extras"]
        return pkg

    for p in container.find_all(["p", "div", "li"], recursive=True):
        if p.find(["p", "div"], recursive=False) and not p.find("a", href=True, recursive=False):
            continue
        text = p.get_text(" ", strip=True)
        if not text:
            continue

        pkg_header = _is_package_header(text)
        if pkg_header:
            if seen_first_header and current_pkg is not None:
                new_label = pkg_header.get("label", "")
                cur_label = current_pkg.get("label", "")
                same_ppsa = pkg_header.get("ppsa") == current_pkg.get("ppsa")
                new_is_simpler = ("(" not in new_label) and ("(" in cur_label)

                def _has_real_content(pkg):
                    for entries in (pkg.get("releases") or {}).values():
                        for e in entries:
                            if e.get("links"):
                                return True
                    return False

                if same_ppsa and new_is_simpler:
                    continue

                if _has_real_content(current_pkg):
                    packages.append(current_pkg)
                    current_pkg = start_package(pkg_header)
                    current_release = None
                    continue

                if pkg_header.get("region") and (not current_pkg.get("region") or
                                                 len(pkg_header["region"]) > len(current_pkg.get("region",""))):
                    current_pkg["region"] = pkg_header["region"]
                if pkg_header.get("format") and not current_pkg.get("format"):
                    current_pkg["format"] = pkg_header["format"]
                if "(" in new_label and "(" not in cur_label:
                    current_pkg["label"] = new_label
                continue
            else:
                current_pkg = start_package(pkg_header)
                seen_first_header = True
                current_release = None
                continue

        # Auto-start implicit package on first section if no PPSA header seen
        if current_pkg is None:
            sec_probe = _section_of(text)
            if sec_probe:
                current_pkg = start_package({"label": "", "ppsa": "", "region": ""})
                seen_first_header = True
            else:
                continue

        sec = _section_of(text)
        if sec:
            current_release = {"links": {}}
            current_release.update(_parse_release_header(text, sec))
            current_pkg["releases"].setdefault(sec, []).append(current_release)

        kv = _parse_kv_line(text)
        if kv:
            label, value = kv
            if label == "uploader":
                current_pkg["uploader"] = value
            elif label.startswith(("game size", "size", "file size")):
                current_pkg["size"] = value
            elif label.startswith(("fw required", "firmware", "fw")):
                current_pkg["fw_required"] = value
            elif label == "note":
                current_pkg["note"] = value
            elif label in ("password", "pasword"):
                current_pkg["password"] = value
            elif label.startswith("voice"):
                current_pkg["voice_languages"] = value
            elif label.startswith(("screen language", "screen languages",
                                   "language", "languages")):
                current_pkg["screen_languages"] = value

        if not current_pkg["uploader"]:
            m = re.match(r"^\s*By\s+([^\n]{2,80})\s*$", text)
            if m:
                current_pkg["uploader"] = m.group(1).strip()
            else:
                m = re.match(r"^\s*By([A-Z][^\n]{2,80})\s*$", text)
                if m:
                    current_pkg["uploader"] = m.group(1).strip()

        if current_release is not None:
            for a in p.find_all("a", href=True):
                real = _process_link(a["href"].strip(), seen)
                if real is None:
                    continue
                all_external.append(real)
                key = classify_host(real)
                if key:
                    current_release["links"].setdefault(key, []).append(real)

    if current_pkg is not None:
        packages.append(current_pkg)

    cleaned: list[dict] = []
    for pkg in packages:
        for kind in list(pkg.get("releases", {}).keys()):
            kept = []
            for entry in pkg["releases"][kind]:
                has_links = bool(entry.get("links"))
                has_meta = any(k in entry for k in
                               ("version", "firmware", "release_group", "notes", "dlc"))
                if has_links or has_meta:
                    kept.append(entry)
            if kept:
                pkg["releases"][kind] = kept
            else:
                del pkg["releases"][kind]
        if pkg.get("releases") or pkg.get("uploader") or pkg.get("size"):
            cleaned.append(pkg)
    return cleaned, all_external


def _flatten_releases_across_packages(packages: list[dict]) -> dict:
    flat: dict[str, list[dict]] = {}
    for pkg in packages:
        for kind, entries in pkg.get("releases", {}).items():
            flat.setdefault(kind, []).extend(entries)
    return flat


def extract_releases(soup: BeautifulSoup) -> tuple[dict, list]:
    """Back-compat wrapper: flattens packages → single releases dict."""
    packages, all_external = extract_packages(soup)
    return _flatten_releases_across_packages(packages), all_external


def extract_sectioned_links(soup: BeautifulSoup) -> dict:
    """Back-compat wrapper: flattens releases into the older sections format.

    Returns:
      {
        "game":     {"mediafire": [...], ...},   # merged across all releases
        "backport": {...},
        ...,
        "all_external_links": [...]
      }
    """
    releases, all_external = extract_releases(soup)
    flat: dict[str, dict[str, list[str]]] = {}
    for kind, entries in releases.items():
        bucket = flat.setdefault(kind, {})
        for entry in entries:
            for host, urls in entry["links"].items():
                for u in urls:
                    if u not in bucket.setdefault(host, []):
                        bucket[host].append(u)
    flat["all_external_links"] = all_external
    return flat


def extract_release_info(soup: BeautifulSoup) -> dict:
    """Pull PPSA code, region, edition, release group, version, backport info."""
    body = text_blocks(soup)
    info: dict = {}

    # PPSA code: e.g. "PPSA08804"
    m = re.search(r"\bPPSA[\-_ ]?(\d{3,6})\b", body, flags=re.I)
    if m:
        info["ppsa"] = "PPSA" + m.group(1).zfill(5)

    # Header line that follows the PPSA code, e.g.
    #   PPSA08804 – JPN (Deluxe Edition) (@DUPLEX)
    if "ppsa" in info:
        line_m = re.search(
            r"PPSA[\-_ ]?\d{3,6}\s*[\-–—:]\s*([A-Z/]{2,15})?\s*"
            r"((?:\([^\n)]+\)\s*)*)",
            body, flags=re.I,
        )
        if line_m:
            if line_m.group(1):
                info["region"] = line_m.group(1).strip().upper()
            parens = re.findall(r"\(([^()]+)\)", line_m.group(2) or "")
            edition_bits = []
            for p_ in parens:
                p_ = p_.strip()
                if p_.startswith("@"):
                    info["release_group"] = p_.lstrip("@")
                elif re.match(r"v\s*\d", p_, flags=re.I):
                    info.setdefault("version", "v" + re.search(r"\d[\d.]*", p_).group(0))
                else:
                    edition_bits.append(p_)
            if edition_bits:
                info["edition"] = ", ".join(edition_bits)

    # "Game (v01.012)" — main version
    m = re.search(r"\bGame\s*\(\s*v\s*([0-9]+(?:\.[0-9]+){0,4})\s*\)", body, flags=re.I)
    if m:
        info["version"] = "v" + m.group(1)

    # "Backport 4.xx+ (@BestPig) (Dialogue Fix V2) : ..."
    m = re.search(
        r"^Backport\s*([0-9.x+]*)?\s*((?:\([^)]+\)\s*)*)",
        body, flags=re.I | re.M,
    )
    if m:
        bp: dict = {}
        if m.group(1).strip():
            bp["firmware"] = m.group(1).strip()
        for tok in re.findall(r"\(([^()]+)\)", m.group(2) or ""):
            tok = tok.strip()
            if tok.startswith("@"):
                bp["release_group"] = tok.lstrip("@")
            else:
                bp.setdefault("notes", []).append(tok)
        if "notes" in bp:
            bp["notes"] = "; ".join(bp["notes"])
        info["backport_info"] = bp

    return info


def extract_links(soup: BeautifulSoup) -> tuple[dict[str, list[str]], list[str]]:
    """Back-compat wrapper around extract_sectioned_links.

    Returns flattened (classified_by_host, all_external_links). The new
    sectioned data is added in parse_game_post separately.
    """
    sectioned = extract_sectioned_links(soup)
    flat: dict[str, list[str]] = {}
    for sec, hosts in sectioned.items():
        if sec == "all_external_links":
            continue
        for host, urls in hosts.items():
            for u in urls:
                if u not in flat.setdefault(host, []):
                    flat[host].append(u)
    return flat, sectioned["all_external_links"]


def parse_game_post(url: str, html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    title_tag = (
        soup.select_one("h1.entry-title")
        or soup.select_one("h1.post-title")
        or soup.select_one("article h1")
        or soup.select_one("title")
    )
    title = title_tag.get_text(strip=True) if title_tag else ""
    # Some themes append site name to <title>; trim it
    title = re.sub(r"\s*[-|–]\s*Download Game.*$", "", title, flags=re.I).strip()

    meta = extract_metadata(soup)
    release = extract_release_info(soup)
    description = extract_description(soup)
    description_rich = extract_description_rich(soup)
    cover, screenshots = extract_images(soup)
    packages, all_external = extract_packages(soup)

    # Backfill: implicit packages (no PPSA line in body) need ppsa/label
    # filled from the post-level metadata.
    top_ppsa = release.get("ppsa", "") or meta.get("ppsa", "")
    top_region = meta.get("region") or release.get("region", "")
    if packages:
        for pkg in packages:
            if not pkg.get("ppsa") and top_ppsa:
                pkg["ppsa"] = top_ppsa
            if not pkg.get("region") and top_region:
                pkg["region"] = top_region
            if not pkg.get("label"):
                bits = []
                if pkg.get("ppsa"): bits.append(pkg["ppsa"])
                if pkg.get("region"): bits.append(pkg["region"])
                pkg["label"] = " – ".join(bits) if bits else ""

    releases = _flatten_releases_across_packages(packages)
    # Flattened "sections" view for back-compat
    sectioned_links: dict[str, dict[str, list[str]]] = {}
    for kind, entries in releases.items():
        bucket = sectioned_links.setdefault(kind, {})
        for entry in entries:
            for host, urls in entry["links"].items():
                for u in urls:
                    if u not in bucket.setdefault(host, []):
                        bucket[host].append(u)
    # Flat-by-host view (no section split)
    flat: dict[str, list[str]] = {}
    for hosts in sectioned_links.values():
        for host, urls in hosts.items():
            for u in urls:
                if u not in flat.setdefault(host, []):
                    flat[host].append(u)

    # Prefer the release-line region if extract_metadata didn't find one
    region = meta.get("region") or release.get("region", "")
    # Or grab from first package if neither matched
    if not region and packages and packages[0].get("region"):
        region = packages[0]["region"]

    # ---- derive top-level version / backport summary from releases ----
    game_releases = releases.get("game", [])
    bp_releases = releases.get("backport", [])
    dlc_releases = releases.get("dlc", [])

    def _ver_key(r):
        v = (r.get("version") or "").lstrip("v")
        parts = re.findall(r"\d+", v)
        return tuple(int(p) for p in parts) if parts else (-1,)

    if game_releases:
        latest = max(game_releases, key=_ver_key)
        version = latest.get("version") or release.get("version") or meta.get("version", "")
    else:
        version = release.get("version") or meta.get("version", "")
    seen_v: list = []
    for r in game_releases:
        v = r.get("version")
        if v and v not in seen_v:
            seen_v.append(v)
    all_versions = seen_v if len(seen_v) > 1 else []

    if bp_releases:
        primary_bp = {k: v for k, v in bp_releases[0].items() if k != "links"}
        backport_summary = primary_bp
        backport_count = len(bp_releases) if len(bp_releases) > 1 else 0
    else:
        backport_summary = release.get("backport_info", {})
        backport_count = 0

    # Pull top-level conveniences from the first (primary) package
    uploader = packages[0].get("uploader", "") if packages else ""
    fw_required = packages[0].get("fw_required", "") if packages else ""
    password = packages[0].get("password", "") if packages else ""
    pkg_size = packages[0].get("size", "") if packages else ""
    pkg_voice = packages[0].get("voice_languages", "") if packages else ""
    pkg_screen = packages[0].get("screen_languages", "") if packages else ""

    return {
        "title": title,
        "url": url,
        "cover": cover,
        "screenshots": screenshots,
        "ppsa": release.get("ppsa", "") or (packages[0].get("ppsa", "") if packages else ""),
        "version": version,
        "all_versions": all_versions,
        "edition": release.get("edition", ""),
        "release_group": release.get("release_group", ""),
        "uploader": uploader,
        "size": meta.get("size", "") or pkg_size,
        "region": region,
        "fw_required": fw_required,
        "password": password,
        "genre": meta.get("genre", ""),
        "release_date": meta.get("release_date", ""),
        "language": meta.get("language", ""),
        "voice_languages": meta.get("voice_languages", "") or pkg_voice,
        "screen_languages": meta.get("screen_languages", "") or pkg_screen,
        "backport": backport_summary,
        "backport_count": backport_count,
        "has_dlc": bool(dlc_releases),
        "dlc_count": len(dlc_releases),
        "description": description,
        "description_rich": description_rich,
        "packages": packages,            # NEW: per-package detail
        "package_count": len(packages),
        "releases": releases,            # flattened across packages
        "sections": sectioned_links,     # flattened back-compat view
        "links": flat,                   # flat-by-host back-compat view
        "all_external_links": all_external,
    }


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def gather_post_urls(max_pages: int | None) -> list[str]:
    print(f"Fetching page 1: {CATEGORY_URL}")
    html = fetch(CATEGORY_URL)
    if html is None:
        print("Could not load category page 1. Aborting.")
        sys.exit(1)

    detected = detect_last_page(html)
    if max_pages is not None:
        # User-specified --pages always wins (lets you force a range even if
        # pagination autodetect undercounted).
        last_page = max_pages
        print(f"--pages override: scraping pages 1..{last_page} "
              f"(autodetect said {detected})")
    else:
        last_page = detected
    print(f"Scraping pages 1..{last_page}")

    urls = extract_post_urls(html)
    print(f"  page 1: {len(urls)} posts")

    for page in range(2, last_page + 1):
        time.sleep(_request_delay())
        page_url = PAGE_URL.format(page=page)
        print(f"Fetching page {page}: {page_url}")
        html = fetch(page_url)
        if html is None:
            print("    ! unavailable, skipping")
            continue
        new = extract_post_urls(html)
        urls.extend(new)
        print(f"  page {page}: +{len(new)} (total {len(urls)})")

    # Dedup, preserve order
    seen = set()
    deduped = []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        deduped.append(u)
    return deduped


def load_existing(path: Path) -> dict:
    if not path.exists():
        return {"source": CATEGORY_URL, "total": 0, "games": []}
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError:
        return {"source": CATEGORY_URL, "total": 0, "games": []}


def save(out_path: Path, data: dict) -> None:
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def main() -> None:
    global _BACKEND
    ap = argparse.ArgumentParser(description="Scrape dlpsgame.com PS5 games to JSON.")
    ap.add_argument("--pages", type=int, default=None,
                    help="Limit to first N category pages (default: all).")
    ap.add_argument("--resume", action="store_true",
                    help="Skip game URLs already present in the output file.")
    ap.add_argument("-o", "--output", default=OUTPUT_FILE,
                    help=f"Output JSON file (default: {OUTPUT_FILE}).")
    ap.add_argument("--browser", choices=["auto", "cloudscraper", "playwright"],
                    default="auto",
                    help="HTTP backend. 'auto' tries cloudscraper, falls back to "
                         "Playwright if blocked. 'playwright' forces a real "
                         "headless browser (requires `pip install playwright` and "
                         "`playwright install chromium`).")
    args = ap.parse_args()

    if args.browser == "playwright":
        _BACKEND = "playwright_forced"
        print("Backend: Playwright (forced)")
    elif args.browser == "cloudscraper":
        _BACKEND = "cloudscraper"
        print("Backend: cloudscraper (no fallback)")
    else:
        _BACKEND = "cloudscraper"
        print("Backend: auto (cloudscraper, will fall back to Playwright if blocked)")

    out_path = Path(args.output).resolve()
    existing = load_existing(out_path) if args.resume else {
        "source": CATEGORY_URL, "total": 0, "games": []
    }
    existing_urls = {g["url"] for g in existing.get("games", [])}

    try:
        post_urls = gather_post_urls(args.pages)
        print(f"\nFound {len(post_urls)} unique game pages.\n")

        games = existing["games"] if args.resume else []
        skipped = 0

        for i, url in enumerate(post_urls, 1):
            if args.resume and url in existing_urls:
                skipped += 1
                continue
            time.sleep(_request_delay())
            print(f"[{i}/{len(post_urls)}] {url}")
            html = fetch(url)
            if html is None:
                print("    ! could not fetch, skipping")
                continue
            try:
                game = parse_game_post(url, html)
            except Exception as e:
                print(f"    ! parse error: {e}")
                continue
            games.append(game)
            link_total = sum(len(v) for v in game["links"].values())
            print(f"    -> '{game['title']}' | hosts: {list(game['links'].keys())} | {link_total} link(s)")

            save(out_path, {"source": CATEGORY_URL, "total": len(games), "games": games})

        print(f"\nDone. {len(games)} games written to {out_path}")
        if skipped:
            print(f"Skipped {skipped} already-present URLs (--resume).")
    finally:
        shutdown_backends()


if __name__ == "__main__":
    main()
