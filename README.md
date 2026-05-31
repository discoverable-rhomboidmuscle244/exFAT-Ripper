# exFAT Ripper v2.8

A premium Windows desktop launcher for the PS5 exFAT library. Browse cover art, PPSA codes, titles, versions and download links — and scrape or update the library in the background while you browse.

Built with pywebview (Python backend + HTML/CSS/JS frontend). Dark gaming-launcher aesthetic: custom frameless titlebar, animated sidebar, cover-art grid, cinematic detail panel.

---

## What's new in v2.8

- **Fixed window dragging** — the frameless titlebar can now be moved around the screen. Previously only the minimise/maximise/close buttons worked. Drag anywhere on the titlebar (except the buttons); double-click to maximise/restore.
- Offline-safe font fallbacks (Segoe UI / Consolas) so the UI renders correctly even if Google Fonts can't load.

## What's new in v2.7

This is a full rewrite of the original scraper UI. Everything below is new since the first release.

### Interface
- **Premium launcher UI** — DM Sans + JetBrains Mono fonts, deep-space dark theme, smooth hover animations, card glow effects
- **Wider detail panel** (440px) with cinematic 172px hero image, gradient overlay, game title at the bottom in 900-weight
- **Windows 11 window controls** — monochrome `— □ ✕` icons, close button turns red on hover; macOS-style dots removed
- **Tab-pill row** — `All | Native | Backports | DLCs | Updates` with live counts, plus Import / Export / Backup strip buttons
- **Library Health widget** — animated SVG ring gauge, percentage score (Excellent / Good / Fair), ✔/⚠/✖ checklist for games, covers, missing metadata, broken links, verified count
- **Storage widget** — ring gauge showing TB used/free
- **Sort cycling** — Title A–Z / Z–A / Top Rated / Largest First / Recently Added
- **Grid + list view toggle**
- **Firmware and extra filter dropdowns**

### Scraping
- **"Update Library" split button** — main button runs a smart update (new games only); dropdown expands to: Update Library / Fetch Missing Metadata / Repair Missing Covers / Rebuild Cover Cache / Full Re-Scrape / Repair Database
- **Specific status messages** per mode — "Checking for new games…", "Fetching missing metadata…", etc. instead of generic "Loading…"
- **First-time onboarding modal** — fires on first launch with an empty library, explains the scrape phases (Downloading covers → Fetching metadata → Building local cache → Verifying files), with Start / Not Now buttons
- **Live progress bar** in status bar showing label, fill, %, Workers, Speed, ETA

### Game cards
- Fixed `aspect-ratio: 3/4`, `object-fit: cover` — no more stretching
- **FW badge colour coding** — green = native/playable, purple = backport, cyan = FW version, amber = DLC
- Hover: `translateY(-3px)` + purple glow shadow
- **Hover quick-actions overlay** — favourite / open folder / re-scrape / fetch metadata without opening the detail panel
- ⚠ "No metadata" indicator on cards missing enrichment data
- Favourite star top-right (persisted in localStorage)

### Detail panel
- **Scores row** — Metacritic / OpenCritic / HowLongToBeat from enrichment
- **Rich 2-col metadata grid** — Developer, Publisher, Genres, Play Modes, Languages, Release Date with icons
- **Collapsible description** — 4–5 lines shown, fade at bottom, "Read More ↓ / Show Less ↑" toggle with smooth animation
- **Missing Metadata banner** — amber warning with Retry and Manual Match buttons; separate red warning if no API keys are configured with "Open Settings" link
- **Action row** — Verify Files + More dropdown (Open Folder / Fetch Metadata / Copy Game Info / Re-Scrape)
- **Re-Scrape** moved into the More dropdown — cleaner panel

### Files tab — complete rebuild
- **Version-grouped download cards** — builds are grouped by version number (e.g. `v01.006`), sorted newest first
- **Older versions collapse by default** with chevron toggle and "Older Version" tag
- **Never shows raw numbers** (`1 2`) — the original bug where hosts with 2–3 links rendered as bare index numbers is fixed
- **Host cards** with emoji icons (📦 Akirabox, ⚓ VikingFile, 💾 Datanodes, 🔥 MediaFire, etc.) and pretty-printed names
- Multi-part hosts show `Part 1 / Part 2 / …` rows; multi-mirror hosts show `Mirror 1 / Mirror 2 / …`
- Mirror/part count badge: `Akirabox (2 mirrors)` / `Datanodes (3 parts)`
- Open + Copy buttons per link; Open All + Copy All for multi-link hosts
- BACKPORT / DLC / UPDATE / FIX each get their own colour-coded chip, never mixed into version groups
- Null/empty links silently filtered

### Screenshots
- **Fullscreen gallery** — click any screenshot thumbnail to open
- Keyboard navigation (← → Esc), arrow buttons, background blur
- Thumbnail strip at the bottom; click to jump
- Zoom on click
- Preloads adjacent images

### Metadata enrichment
- **RAWG → IGDB → Steam fallback chain** with confidence scoring
- Enrichment data stored in `game["enrichment"]` sub-dict — scraped fields (PPSA, version, firmware, credits, links) are never overwritten
- **API key warning** — reactive; disappears the moment you type a key in Settings (no save required), re-checks on every navigation back to the library
- **Fix All Missing Metadata banner** — appears at the top of the Missing Metadata view with a one-click "Fetch All Missing Metadata" button and count of affected games

### Settings page
- RAWG API key, IGDB Client ID + Token
- Cover cache controls (download on scrape, prefer cached, custom folder, repair, rebuild, clear)
- Metadata enrichment toggles (auto-enrich, offline mode, enrich all now)
- Database repair and update scrape

### Credits renderer — fixed
- `{"files":"SiESPTA","backport":"BestPig","dlc":""}` objects now render as `Files: SiESPTA · Backport: BestPig`
- Handles string, array, array-of-objects (with role/name fields), and plain objects
- Empty fields silently dropped — no more `[object Object]` or raw JSON dumps

### Close / support modal
- Every window-close attempt shows a lightweight support overlay (Ko-fi link + Close App)
- Blurred background, fade-in animation

### Bug fixes
- **App hang on launch** (v2.6 regression) — `saveSettings` had a mismatched paren/brace that caused a JS parse error, making the entire script fail silently on startup. Fixed and a full duplicate-function audit run.
- **Duplicate `startScrape` definition** — the stale single-line version was never removed when the multi-line version was added. Removed.
- JS brace balance verified: 427 opens / 427 closes, zero duplicate function names.

---

## Metadata API keys

Metadata (descriptions, ratings, genres, Metacritic, screenshots) comes from external APIs. All are free tier.

| Provider | Free limit | Get key |
|---|---|---|
| RAWG | 20,000 req/month | [rawg.io/apidocs](https://rawg.io/apidocs) |
| IGDB | Unlimited (Twitch auth) | [api.igdb.com](https://api.igdb.com) |

Paste keys into **Settings → API Keys**. The app falls back RAWG → IGDB → Steam (no key needed for Steam).

---

## Building the .exe

You need **Python 3.10+** (tick "Add to PATH" in the installer).

```
build_exe.bat
```

Installs dependencies, downloads the Chromium engine, builds with PyInstaller. Output:

```
dist\PS5 exFAT Library.exe
```

Move/copy the `.exe` anywhere — it keeps its `exfat_games.json` next to itself.

## Running without building

```
python -m pip install -r requirements.txt
python -m playwright install chromium
run_app.bat
```

---

## How it works

Scraping is split into two phases:

**Phase 1 — Page walk** (~1–2 min, once): reads every game's cover, title, PPSA, version, badges, and captures each download button's *encrypted* link. Future updates only walk pages you haven't seen.

**Phase 2 — Link unlock** (instant, on demand): locked links are decrypted offline the moment you open a game. No browser, no waiting.

The old approach opened a browser tab per link (~30–40 min). This app never does that.

---

## Browser requirement

The exFAT source sits behind a password gate and Cloudflare/GitHub Pages JavaScript — a real browser engine is needed:

- `build_exe.bat` bundles a private Chromium for Playwright automatically
- If you run **Brave/Chrome/Edge with remote debugging on port 9222**, the app attaches to that instead — useful if a CAPTCHA appears

After scraping, everything is offline.

---

## Files

```
app.py                        backend (pywebview + Python API)
core/scraper.py               exFAT fast page-walk
core/decrypt.py               offline AES-GCM link-lock decryption
core/cover_cache.py           local cover image caching
core/metadata.py              RAWG / IGDB / Steam enrichment
core/settings.py              settings persistence
core/dlps_scraper.py          dlpsgame scraping
core/dlpsgame_ps5_scraper.py  dlpsgame scraper
core/dlps_links.py            dlpsgame link grouping
ui/index.html                 full launcher UI (HTML/CSS/JS)
requirements.txt              Python dependencies
exFAT_Ripper.spec             PyInstaller build recipe
runtime_hook_playwright.py    points Playwright at bundled Chromium
build_exe.bat                 one-click build
run_app.bat                   run without building
```

---

## Notes

- First launch of an unsigned `.exe` may show a Windows SmartScreen warning — "More info" → "Run anyway". Normal for unsigned apps.
- The packaged `.exe` is large (bundled Chromium + Python runtime). Expected.
- If scraping fails with "grid never appeared" the site is showing a CAPTCHA. Open the site in Brave/Chrome, solve it, leave it open, scrape again.
- This app scrapes a site hosting pirated content. Make sure your use complies with local law.

---

## Credits

Special thanks to **pippo26442999** — creator and maintainer of the PS5 exFAT library. Without pippo's work there would be nothing to scrape.

Built by [kerrdec97](https://github.com/kerrdec97) · [Discord](https://discord.gg/qFJw7pcpX) · [Ko-fi](https://ko-fi.com/deckerr9746220)
