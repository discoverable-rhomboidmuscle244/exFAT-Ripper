# PS5 exFAT Library — desktop app

A native Windows app that browses the .exFAT PS5 library with cover art,
PPSA codes, titles, versions and download links — and can scrape or update
the library itself.

It's built as a real desktop app: a custom frameless title bar, a sidebar,
the cover-art grid as the centerpiece, and a status bar with live scrape
progress — a dark gaming-launcher style, not a browser window.

## Why it's fast

Scraping is split into two parts:

* **The page-walk** (slow part avoided): the app reads every game's cover,
  title, PPSA, version and badges from the site grid. It also captures each
  download button's *locked* (encrypted) link. This takes ~1-2 minutes for
  the whole catalogue.
* **Link unlocking** (instant, on demand): the locked links are decrypted
  offline — no browser, no waiting — the moment you open a game. So the
  scrape is quick and clicking a game shows its links immediately.

The old approach opened a browser tab for every single link (~30-40 min).
This app never does that.

## Building the .exe

You need **Python 3.10+** installed (tick "Add to PATH" in the installer).

Then just double-click:

```
build_exe.bat
```

It installs the dependencies, downloads the Chromium engine, and builds the
app with PyInstaller. When it finishes you'll have:

```
dist\PS5 exFAT Library.exe
```

Double-click that to run. You can move/copy that .exe anywhere; it keeps its
`exfat_games.json` library file next to itself.

## Running without building (for testing)

If you just want to try it without making an .exe:

```
python -m pip install -r requirements.txt
python -m playwright install chromium
run_app.bat
```

## Using the app

The sidebar has two sources — **exFAT** and **dlpsgame** — each its own
library. Click either to switch; the grid, search and Scrape/Update buttons
all act on the source that's currently selected.

* **Scrape All** — builds the selected source's library from scratch.
* **Update** — walks the site but only adds games you don't already have.
* **Search box** — filter by title or PPSA.
* **Firmware / DLC filters** — narrow the grid.
* **Click any game** — opens a detail panel; links resolve instantly.
  Each link has **Open** (in your browser) and **Copy**.

**Multi-part downloads.** dlpsgame releases are often split into many parts
on one host (sometimes 20+). Instead of a long wall of identical host names,
the detail panel collapses them into a single row — e.g.
`mediafire — 22 parts`. It has **Copy all** / **Open all** buttons and a ▾
to expand the full numbered list (Part 1, Part 2, …). Hosts with only a few
links still show inline as normal.

Each source saves its own file beside the app — `exfat_games.json` and
`ps5_games.json` — so both libraries persist between runs.

## The browser requirement (please read)

The **exFAT** source sits behind a password gate and Cloudflare/GitHub
Pages JavaScript. Getting past that needs a real browser engine:

* The build step downloads a private Chromium for Playwright; the app uses
  it automatically and headlessly for the ~1-2 minute page-walk.
* If you already run **Brave/Chrome/Edge with remote debugging on port
  9222**, the app attaches to that instead (useful if the site ever shows a
  CAPTCHA — solve it once in that browser and the app sails through).

The **dlpsgame** source uses `cloudscraper` instead — a pure-Python
Cloudflare solver, no browser needed at all. If dlpsgame rate-limits you,
the scraper backs off automatically and you can resume with Update.

After scraping, everything is offline: browsing and link resolution need no
browser.

## Files

```
app.py                          the app (pywebview window + Python backend)
core/scraper.py                 exFAT fast page-walk — Phase 1
core/decrypt.py                 offline link-lock decryption — instant links
core/dlps_scraper.py            dlpsgame scraping (wraps the script below)
core/dlpsgame_ps5_scraper.py    your dlpsgame scraper (used by the app)
core/dlps_links.py              dlpsgame link grouping for the detail panel
ui/index.html                   the library interface
requirements.txt                Python dependencies
PS5_exFAT_Library.spec          PyInstaller build recipe
runtime_hook_playwright.py      points Playwright at the bundled Chromium
build_exe.bat                   one-click: build the .exe
run_app.bat                     run without building (testing)
```

## Notes & limitations

* First launch of an unsigned .exe may trigger a Windows SmartScreen
  warning — "More info" then "Run anyway". That's normal for unsigned apps;
  it isn't a virus warning.
* The packaged .exe is fairly large (the bundled Chromium and Python
  runtime). That's expected for this kind of app.
* If a scrape fails with a message about the grid never appearing, the site
  is likely showing a CAPTCHA. Open the site once in Brave/Chrome, solve it,
  leave that browser open, and scrape again.
* The site hosts pirated content. Make sure your use complies with local
  law and the site's terms.
