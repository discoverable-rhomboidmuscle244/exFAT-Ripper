"""
core/scraper.py
---------------
Phase 1 of the library build: walk the .exFAT site's paginated grid and
capture every game's metadata + LOCKED link-lock URLs. This is the fast part
— no per-link browser unlocking. Links are decrypted offline later, on demand,
by core/decrypt.py.

Needs a Chromium-capable browser via Playwright. It will attach to a running
Brave/Chrome on the CDP debug port if one is found, otherwise launch its own
headless Chromium.
"""
from __future__ import annotations

import os
import re
import sys
import urllib.request
from pathlib import Path
from typing import Callable


def _fix_playwright_browsers_path() -> None:
    """When frozen by PyInstaller, Playwright looks for its browser inside
    the exe's temp unpack dir, where it isn't. Point it at the standard
    location where `playwright install chromium` actually puts browsers:
        Windows:  %USERPROFILE%\\AppData\\Local\\ms-playwright
        macOS:    ~/Library/Caches/ms-playwright
        Linux:    ~/.cache/ms-playwright
    Only set if the user hasn't set it themselves.
    """
    if os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        return
    candidates = []
    if sys.platform.startswith("win"):
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "ms-playwright")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:
        candidates.append(Path.home() / ".cache" / "ms-playwright")
    for c in candidates:
        if c.is_dir():
            os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(c)
            return


# Must run before playwright is imported anywhere.
_fix_playwright_browsers_path()

BASE_URL = "https://pippo26442999.github.io/.exFAT/"
SITE_PASSWORD = "pippo"
CDP_URL = os.environ.get("DLPSGAME_CDP_URL", "http://localhost:9222")

PAGE_LOAD_TIMEOUT_MS = 60_000
CARD_WAIT_TIMEOUT_MS = 30_000
NEXT_PAGE_PAUSE_MS = 1200


# --- Card extraction JS (same selectors as the proven scraper) -----------------

CARD_EXTRACT_JS = r"""
() => {
  function parseOpenDL(a) {
    const onclick = a.getAttribute('onclick') || '';
    const m = onclick.match(/openDL\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*(true|false)\s*,\s*(true|false)\s*,\s*'([^']*)'\s*\)/);
    if (!m) {
      const m2 = onclick.match(/openDL\(\s*'([^']+)'/);
      return { url: m2 ? m2[1] : (a.href || ''), credits_files: '',
               credits_backport: '', credits_dlc: '', is_dlc: false,
               is_dump: false, title: '' };
    }
    return {
      url: m[1].replace(/&amp;/g, '&'),
      credits_files: m[2], credits_backport: m[3], credits_dlc: m[4],
      is_dlc: m[6] === 'true', is_dump: m[7] === 'true', title: m[8],
    };
  }
  function sectionFromLabel(text) {
    const t = (text || '').replace(/[:\s]+$/g, '').trim().toUpperCase();
    if (!t) return 'game';
    if (t === 'STANDARD') return 'standard';
    if (t === 'BACKPORT') return 'backport';
    if (t.startsWith('BACKPORT ')) {
      return 'backport_' + t.slice(9).toLowerCase().replace(/\s+/g, '_');
    }
    if (t === 'DLCS' || t === 'DLC') return 'dlcs';
    if (t === 'DUMP') return 'dump';
    return t.toLowerCase().replace(/\s+/g, '_');
  }
  function parseCard(card) {
    const titleEl = card.querySelector('.game-title');
    const title = titleEl ? titleEl.textContent.trim() : '';
    if (!title) return null;
    const img = card.querySelector('.image-container img');
    const cover = img ? (img.currentSrc || img.src || img.getAttribute('src') || '') : '';
    const tags = Array.from(card.querySelectorAll('.tags-overlay .game-tag'))
      .map(el => el.textContent.trim()).filter(Boolean);
    const sizeEl = card.querySelector('.game-size');
    const sizeBadge = sizeEl ? sizeEl.textContent.trim() : '';
    if (sizeBadge && !tags.includes(sizeBadge)) tags.push(sizeBadge);
    const sections = [];
    let current = { section: 'game', links: [] };
    const ds = card.querySelector('.download-section');
    if (ds) {
      for (const el of Array.from(ds.children)) {
        if (el.tagName === 'P' && el.classList.contains('ver-label')) {
          if (current.links.length) sections.push(current);
          current = { section: sectionFromLabel(el.textContent), links: [] };
        } else if (el.classList && el.classList.contains('download-container')) {
          for (const a of el.querySelectorAll('a[onclick]')) {
            const info = parseOpenDL(a);
            current.links.push({
              label: (a.textContent || '').trim().toUpperCase(), ...info });
          }
        }
      }
      if (current.links.length) sections.push(current);
    }
    let credits = { files: '', backport: '', dlc: '' };
    for (const sec of sections) {
      for (const lnk of sec.links) {
        if (!credits.files    && lnk.credits_files)    credits.files    = lnk.credits_files;
        if (!credits.backport && lnk.credits_backport) credits.backport = lnk.credits_backport;
        if (!credits.dlc      && lnk.credits_dlc)      credits.dlc      = lnk.credits_dlc;
      }
    }
    return { title, cover, tags, sections, credits };
  }
  return Array.from(document.querySelectorAll('.game-card'))
    .map(parseCard).filter(c => c !== null);
}
"""


# --- Badge parsing -------------------------------------------------------------

PPSA_RE = re.compile(r"^PPSA\d{4,6}$", re.I)
VERSION_RE = re.compile(r"^v\d+(\.\d+){1,3}$", re.I)
REGION_RE = re.compile(
    r"^(EUR|USA|JPN|ASIA|UK|GER|FRA|ITA|ESP|RUS|KOR|CHN|HK|MULTI|"
    r"Region Bypass|USA \+ Italian Mod|USA to EUR Languages)$", re.I)
SIZE_RE = re.compile(r"^[\d.]+\s*(GB|MB|TB)$", re.I)
BACKPORT_RE = re.compile(
    r"(BackPork|BackPort|FW\s*\d|\d\.x{1,2}|firmware|beyond|Standard)", re.I)
DLC_BADGE_RE = re.compile(r"DLCs?\s*Merged", re.I)


def parse_badges(tags: list[str]) -> dict:
    out = {"ppsa": "", "version": "", "region": "", "size": "",
           "backport_text": "", "dlcs_merged": False, "other_badges": []}
    for t in tags:
        t = (t or "").strip()
        if not t:
            continue
        if not out["ppsa"] and PPSA_RE.match(t):
            out["ppsa"] = t.upper()
        elif not out["version"] and VERSION_RE.match(t):
            out["version"] = t
        elif not out["region"] and REGION_RE.match(t):
            out["region"] = t
        elif not out["size"] and SIZE_RE.match(t.replace(" ", "")):
            out["size"] = t
        elif DLC_BADGE_RE.search(t):
            out["dlcs_merged"] = True
        elif BACKPORT_RE.search(t):
            out["backport_text"] = (out["backport_text"] + "; " + t).lstrip("; ")
        else:
            out["other_badges"].append(t)
    return out


def absolutize_cover(cover: str) -> str:
    if not cover:
        return ""
    if cover.startswith(("http://", "https://", "data:")):
        return cover
    return BASE_URL + cover.lstrip("./")


# --- Playwright plumbing -------------------------------------------------------

class Browser:
    """Owns the Playwright context. Attaches to a running browser on the CDP
    port if available, else launches headless Chromium."""

    def __init__(self, headless: bool = True):
        self._pw = None
        self._ctx = None
        self._owns_browser = False
        self._headless = headless

    def start(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        # Try CDP attach first (warmed-up Brave with Cloudflare clearance)
        try:
            with urllib.request.urlopen(CDP_URL + "/json/version", timeout=2) as r:
                r.read()
            browser = self._pw.chromium.connect_over_cdp(CDP_URL)
            self._ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            return "attached"
        except Exception:
            pass
        # Launch own headless Chromium
        from pathlib import Path
        profile = Path.home() / ".exfat_app_profile"
        profile.mkdir(exist_ok=True)
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            headless=self._headless,
            viewport={"width": 1400, "height": 900},
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-dev-shm-usage"],
        )
        self._owns_browser = True
        return "launched"

    @property
    def ctx(self):
        return self._ctx

    def stop(self):
        try:
            if self._owns_browser and self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass


def _pass_site_gate(page, password: str) -> bool:
    page.wait_for_timeout(800)
    if not page.evaluate("() => !!document.querySelector('input[type=password]')"):
        return True
    try:
        page.fill("input[type=password]", password)
    except Exception:
        return False
    clicked = page.evaluate("""
        () => {
          const t = document.querySelector('button[type=submit], input[type=submit]');
          if (t) { t.click(); return true; }
          const x = Array.from(document.querySelectorAll('button, input[type=button]'))
            .find(b => /unlock|enter|submit|go/i.test(b.textContent || b.value || ''));
          if (x) { x.click(); return true; }
          return false;
        }
    """)
    if not clicked:
        try:
            page.press("input[type=password]", "Enter")
        except Exception:
            pass
    import time
    deadline = time.time() + 15
    while time.time() < deadline:
        gone = page.evaluate("""
            () => {
              const p = document.querySelector('input[type=password]');
              if (!p) return true;
              const r = p.getBoundingClientRect();
              return r.width === 0 || r.height === 0;
            }
        """)
        if gone:
            return True
        page.wait_for_timeout(300)
    return True  # proceed; cards check will catch a real failure


def _get_page_count(page) -> int:
    try:
        info = page.text_content("#page-info") or ""
    except Exception:
        info = ""
    m = re.search(r"of\s+(\d+)", info)
    return int(m.group(1)) if m else 1


def _current_page(page) -> int:
    try:
        info = page.text_content("#page-info") or ""
    except Exception:
        info = ""
    m = re.search(r"Page\s+(\d+)", info)
    return int(m.group(1)) if m else 1


def _go_to_page_one(page):
    import time
    for _ in range(20):
        if _current_page(page) == 1:
            return
        clicked = page.evaluate("""
            () => { const b=document.querySelector('#prev-page');
                    if(!b||b.disabled) return false; b.click(); return true; }
        """)
        if not clicked:
            return
        page.wait_for_timeout(NEXT_PAGE_PAUSE_MS)


def _next_page(page) -> bool:
    clicked = page.evaluate("""
        () => { const b=document.querySelector('#next-page');
                if(!b||b.disabled) return false; b.click(); return true; }
    """)
    if clicked:
        page.wait_for_timeout(NEXT_PAGE_PAUSE_MS)
    return clicked


# --- The fast scrape -----------------------------------------------------------

def _section_kind(name: str) -> str:
    n = (name or "").lower()
    if n in ("standard", "game", ""):
        return "game"
    if n.startswith("backport"):
        return "backport"
    if n in ("dlcs", "dlc"):
        return "dlc"
    if n == "update":
        return "update"
    if n in ("fix", "patch", "fix_patch"):
        return "fix"
    return "other"


def card_to_game(card: dict) -> dict:
    """Convert a scraped card into a stored game dict. Keeps the LOCKED URLs;
    resolved URLs are filled in later, on demand, by core/decrypt.py."""
    meta = parse_badges(card["tags"])
    raw_sections: dict[str, list[dict]] = {}
    dlc_count = 0
    for sec in card["sections"]:
        sec_name = sec["section"]
        recs = []
        for lnk in sec["links"]:
            recs.append({
                "label": lnk["label"],
                "locked_url": lnk["url"],
                "resolved_url": "",      # filled on demand
                "host": "",
                "is_dlc": lnk.get("is_dlc", False),
                "is_dump": lnk.get("is_dump", False),
            })
        raw_sections[sec_name] = recs
        if _section_kind(sec_name) == "dlc" and recs:
            dlc_count += 1
    return {
        "source": "exfat",
        "title": card["title"],
        "url": BASE_URL,
        "cover": absolutize_cover(card.get("cover", "")),
        "ppsa": meta["ppsa"],
        "version": meta["version"],
        "region": meta["region"],
        "size": meta["size"],
        "backport_text": meta["backport_text"],
        "dlcs_merged": meta["dlcs_merged"],
        "has_dlc": dlc_count > 0 or meta["dlcs_merged"],
        "dlc_count": dlc_count,
        "other_badges": meta["other_badges"],
        "tags_raw": card["tags"],
        "credits": card.get("credits", {}),
        "exfat_raw_sections": raw_sections,
    }


def scrape_library(password: str = SITE_PASSWORD,
                    progress: Callable[[str, int, int], None] | None = None,
                    headless: bool = True,
                    max_pages: int = 0) -> list[dict]:
    """Walk the whole site and return a list of game dicts (with locked URLs).

    progress(message, current, total) is called as pages are scanned, so the
    UI can show a progress bar. current/total are page numbers.
    """
    def report(msg, cur=0, tot=0):
        if progress:
            try:
                progress(msg, cur, tot)
            except Exception:
                pass

    browser = Browser(headless=headless)
    games: list[dict] = []
    seen: set[str] = set()
    try:
        mode = browser.start()
        report(f"Browser ready ({mode}).", 0, 0)
        page = browser.ctx.new_page()
        try:
            report("Loading site...", 0, 0)
            page.goto(BASE_URL, wait_until="domcontentloaded",
                      timeout=PAGE_LOAD_TIMEOUT_MS)
            if not _pass_site_gate(page, password):
                raise RuntimeError("Could not pass the site password gate.")
            try:
                page.wait_for_selector(".game-card", timeout=CARD_WAIT_TIMEOUT_MS)
            except Exception:
                raise RuntimeError(
                    "Game grid never appeared — the site may be showing a "
                    "Cloudflare/CAPTCHA challenge. Open it once in Brave, "
                    "solve it, then retry.")
            _go_to_page_one(page)
            total = _get_page_count(page)
            if max_pages:
                total = min(total, max_pages)
            for p_idx in range(1, total + 1):
                cards = page.evaluate(CARD_EXTRACT_JS)
                added = 0
                for c in cards:
                    key = c["title"].strip().lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    games.append(card_to_game(c))
                    added += 1
                report(f"Page {p_idx}/{total} — {len(games)} games found",
                       p_idx, total)
                if p_idx < total and not _next_page(page):
                    break
        finally:
            try:
                page.close()
            except Exception:
                pass
    finally:
        browser.stop()
    return games
