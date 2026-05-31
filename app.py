#!/usr/bin/env python3
"""
app.py  —  exFAT Ripper v2.8
-----------------------------
Desktop app (pywebview) for browsing and managing the PS5 exFAT game library.

New in v2:
  * Local cover image caching (covers/ folder)
  * Metadata enrichment via RAWG / IGDB / Steam APIs
  * Settings persistence (settings.json)
  * Repair missing covers
  * Batch enrich metadata
  * Smart title cleaning for API matching
"""
from __future__ import annotations

import json
import os
import sys
import threading
import traceback
from pathlib import Path

import webview

from core.scraper import scrape_library, SITE_PASSWORD
from core.decrypt import resolve_game_links
from core.cover_cache import ensure_cover, repair_missing, bulk_cache_covers
from core.metadata import enrich_game, batch_enrich, clean_title
from core import settings as settings_mod


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

def resource_dir() -> Path:
    """Folder holding bundled, read-only assets (the ui/ folder)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def data_dir() -> Path:
    """Folder for writable runtime data — persists between runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


DATA_FILES = {
    "exfat": data_dir() / "exfat_games.json",
}
UI_FILE = resource_dir() / "ui" / "index.html"


def data_file_for(source: str) -> Path:
    return DATA_FILES.get(source, DATA_FILES["exfat"])


def _norm_title(t: str) -> str:
    return " ".join((t or "").lower().split())


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

class Api:
    """Every public method is callable from JS as window.pywebview.api.*()."""

    def __init__(self):
        self._window = None
        self._busy = False
        self._maximized = False
        self._settings: dict = {}
        self._settings_loaded = False

    def bind(self, window):
        self._window = window
        # Load settings eagerly
        self._load_settings_internal()

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _load_settings_internal(self):
        self._settings = settings_mod.load(data_dir())
        self._settings_loaded = True

    def get_settings(self) -> dict:
        if not self._settings_loaded:
            self._load_settings_internal()
        return {"ok": True, "settings": self._settings}

    def save_settings(self, new_settings: dict) -> dict:
        try:
            self._settings.update(new_settings)
            ok = settings_mod.save(data_dir(), self._settings)
            return {"ok": ok}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Library load / save
    # ------------------------------------------------------------------

    def load_library(self, source: str = "exfat") -> dict:
        path = data_file_for(source)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                games = data.get("games", [])
                # Migrate: ensure scraped_title is set
                for g in games:
                    if not g.get("scraped_title"):
                        g["scraped_title"] = g.get("title", "")
                return {"ok": True, "source": source,
                        "games": games, "total": len(games)}
            except Exception as e:
                return {"ok": False, "source": source,
                        "error": f"Could not read library: {e}",
                        "games": [], "total": 0}
        return {"ok": True, "source": source, "games": [], "total": 0}

    def _backup_existing(self, source: str) -> Path | None:
        path = data_file_for(source)
        if not path.exists():
            return None
        backups = data_dir() / "backups"
        backups.mkdir(exist_ok=True)
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backups / f"{path.stem}_{stamp}.json"
        n = 2
        while dest.exists():
            dest = backups / f"{path.stem}_{stamp}-{n}.json"
            n += 1
        try:
            dest.write_bytes(path.read_bytes())
        except Exception:
            return None
        # Prune: keep only 10 most recent
        try:
            mine = sorted(backups.glob(f"{path.stem}_*.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for old in mine[10:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
        return dest

    def _save(self, source: str, games: list[dict]) -> None:
        self._backup_existing(source)
        origin = "https://pippo26442999.github.io/.exFAT/"
        payload = {"source": origin, "total": len(games), "games": games}
        path = data_file_for(source)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------
    # Link resolution
    # ------------------------------------------------------------------

    def get_links(self, game_index: int, source: str = "exfat",
                  password: str = SITE_PASSWORD) -> dict:
        lib = self.load_library(source)
        games = lib.get("games", [])
        if game_index < 0 or game_index >= len(games):
            return {"ok": False, "error": "Game not found."}
        game = games[game_index]
        try:
            return {"ok": True, **resolve_game_links(game, password)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Progress / finish signals to JS
    # ------------------------------------------------------------------

    def _progress(self, message: str, current: int, total: int) -> None:
        if not self._window:
            return
        payload = json.dumps({"message": message,
                              "current": current, "total": total})
        try:
            self._window.evaluate_js(f"window.onScrapeProgress({payload})")
        except Exception:
            pass

    def _finish(self, ok: bool, message: str, count: int = 0) -> None:
        if not self._window:
            return
        payload = json.dumps({"ok": ok, "message": message, "count": count})
        try:
            self._window.evaluate_js(f"window.onScrapeDone({payload})")
        except Exception:
            pass

    def _event(self, name: str, payload: dict) -> None:
        if not self._window:
            return
        data = json.dumps(payload)
        try:
            self._window.evaluate_js(f"window.onAppEvent({json.dumps(name)},{data})")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Scrape
    # ------------------------------------------------------------------

    def start_scrape(self, source: str = "exfat", mode: str = "full",
                     password: str = SITE_PASSWORD) -> dict:
        if self._busy:
            return {"ok": False, "error": "A scrape is already running."}
        self._busy = True
        t = threading.Thread(target=self._scrape_worker,
                             args=(source, mode, password), daemon=True)
        t.start()
        return {"ok": True}

    def _scrape_worker(self, source: str, mode: str, password: str) -> None:
        try:
            existing = []
            path = data_file_for(source)
            if mode == "update" and path.exists():
                try:
                    existing = json.loads(
                        path.read_text(encoding="utf-8")).get("games", [])
                except Exception:
                    existing = []

            self._progress("Starting browser...", 0, 0)
            scraped = scrape_library(
                password=password,
                progress=self._progress,
                headless=True,
            )

            # Tag scraped data
            for g in scraped:
                if not g.get("scraped_title"):
                    g["scraped_title"] = g.get("title", "")

            if mode == "update" and existing:
                have = {_norm_title(g.get("title", "")) for g in existing}
                fresh = [g for g in scraped
                         if _norm_title(g.get("title", "")) not in have]
                # Merge: preserve enrichment data from existing games
                existing_by_title = {_norm_title(g.get("title", "")): g
                                     for g in existing}
                for g in scraped:
                    key = _norm_title(g.get("title", ""))
                    if key in existing_by_title:
                        old = existing_by_title[key]
                        # Keep enrichment but update scraped fields
                        if old.get("enrichment"):
                            g["enrichment"] = old["enrichment"]
                        if old.get("cover_local_path"):
                            g["cover_local_path"] = old["cover_local_path"]
                merged = existing + fresh
                games_to_save = merged
            else:
                # Full scrape: preserve existing enrichment by title
                if path.exists():
                    try:
                        old_games = json.loads(
                            path.read_text(encoding="utf-8")).get("games", [])
                        old_by_title = {_norm_title(g.get("title", "")): g
                                       for g in old_games}
                        for g in scraped:
                            key = _norm_title(g.get("title", ""))
                            if key in old_by_title:
                                old = old_by_title[key]
                                if old.get("enrichment"):
                                    g["enrichment"] = old["enrichment"]
                                if old.get("cover_local_path"):
                                    g["cover_local_path"] = old["cover_local_path"]
                    except Exception:
                        pass
                games_to_save = scraped

            self._save(source, games_to_save)

            # Auto-cache covers
            if self._settings.get("download_covers_on_scrape", True):
                self._progress("Caching cover images…", 0, len(games_to_save))
                downloaded = bulk_cache_covers(
                    games_to_save, data_dir(),
                    progress_fn=self._progress,
                )
                # Save again with cover paths
                self._save(source, games_to_save)
                self._progress(f"Covers cached: {downloaded}", 0, 0)

            # Auto-enrich metadata
            if self._settings.get("auto_enrich", True) and not self._settings.get("offline_mode"):
                self._progress("Enriching metadata…", 0, len(games_to_save))
                enriched_count = batch_enrich(
                    games_to_save, self._settings,
                    progress_fn=self._progress,
                )
                # Cover URLs may have been populated during enrichment
                if self._settings.get("download_covers_on_scrape", True):
                    bulk_cache_covers(games_to_save, data_dir())
                self._save(source, games_to_save)
                self._progress(f"Metadata enriched: {enriched_count} games", 0, 0)

            total = len(games_to_save)
            fresh_count = len(fresh) if mode == "update" and existing else total
            msg = (f"Update complete — {fresh_count} new games added ({total} total)."
                   if mode == "update" and existing
                   else f"Scrape complete — {total} games.")
            self._finish(True, msg, total)

        except Exception as e:
            self._finish(False, f"Scrape failed: {e}")
            traceback.print_exc()
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Metadata enrichment
    # ------------------------------------------------------------------

    def enrich_one(self, game_index: int, source: str = "exfat") -> dict:
        """Enrich a single game's metadata. Returns enrichment dict."""
        lib = self.load_library(source)
        games = lib.get("games", [])
        if game_index < 0 or game_index >= len(games):
            return {"ok": False, "error": "Game not found."}
        game = games[game_index]
        if self._settings.get("offline_mode"):
            return {"ok": False, "error": "Offline mode is enabled."}
        try:
            result = enrich_game(game, self._settings)
            self._save(source, games)
            return {"ok": True, "enrichment": result}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def enrich_all(self, source: str = "exfat") -> dict:
        """Enrich all games without metadata (background thread)."""
        if self._busy:
            return {"ok": False, "error": "Already busy."}
        if self._settings.get("offline_mode"):
            return {"ok": False, "error": "Offline mode is enabled."}
        self._busy = True
        t = threading.Thread(target=self._enrich_all_worker,
                             args=(source,), daemon=True)
        t.start()
        return {"ok": True}

    def _enrich_all_worker(self, source: str) -> None:
        try:
            lib = self.load_library(source)
            games = lib.get("games", [])
            count = batch_enrich(games, self._settings,
                                 progress_fn=self._progress)
            # Update covers from enrichment data
            if self._settings.get("download_covers_on_scrape", True):
                bulk_cache_covers(games, data_dir(),
                                  progress_fn=self._progress)
            self._save(source, games)
            self._finish(True, f"Metadata enriched for {count} games.", count)
        except Exception as e:
            self._finish(False, f"Enrichment failed: {e}")
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Cover cache
    # ------------------------------------------------------------------

    def repair_covers(self, source: str = "exfat") -> dict:
        """Re-download missing covers (background thread)."""
        if self._busy:
            return {"ok": False, "error": "Already busy."}
        if self._settings.get("offline_mode"):
            return {"ok": False, "error": "Offline mode is enabled."}
        self._busy = True
        t = threading.Thread(target=self._repair_covers_worker,
                             args=(source,), daemon=True)
        t.start()
        return {"ok": True}

    def _repair_covers_worker(self, source: str) -> None:
        try:
            lib = self.load_library(source)
            games = lib.get("games", [])
            count = repair_missing(games, data_dir(),
                                   progress_fn=self._progress)
            self._save(source, games)
            self._finish(True, f"Cover repair complete — {count} covers fixed.", count)
        except Exception as e:
            self._finish(False, f"Cover repair failed: {e}")
        finally:
            self._busy = False

    def rebuild_cover_cache(self, source: str = "exfat") -> dict:
        """Re-download ALL covers (background thread)."""
        if self._busy:
            return {"ok": False, "error": "Already busy."}
        if self._settings.get("offline_mode"):
            return {"ok": False, "error": "Offline mode is enabled."}
        self._busy = True
        t = threading.Thread(target=self._rebuild_covers_worker,
                             args=(source,), daemon=True)
        t.start()
        return {"ok": True}

    def _rebuild_covers_worker(self, source: str) -> None:
        try:
            lib = self.load_library(source)
            games = lib.get("games", [])
            count = bulk_cache_covers(games, data_dir(),
                                      progress_fn=self._progress)
            self._save(source, games)
            self._finish(True, f"Cover rebuild complete — {count} downloaded.", count)
        except Exception as e:
            self._finish(False, f"Cover rebuild failed: {e}")
        finally:
            self._busy = False

    def get_cover_data_url(self, local_path: str) -> dict:
        """
        Read a cached cover file and return it as a base64 data-URL so the
        webview can display it without needing http_server file access.
        """
        try:
            p = Path(local_path)
            if not p.exists():
                return {"ok": False}
            import base64
            ext = p.suffix.lower().lstrip(".")
            mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg",
                    "png": "image/png", "webp": "image/webp"}.get(ext, "image/jpeg")
            b64 = base64.b64encode(p.read_bytes()).decode()
            return {"ok": True, "data_url": f"data:{mime};base64,{b64}"}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Library utilities
    # ------------------------------------------------------------------

    def get_library_stats(self, source: str = "exfat") -> dict:
        """Return statistics about the library."""
        lib = self.load_library(source)
        games = lib.get("games", [])
        total = len(games)
        has_cover = sum(1 for g in games
                       if g.get("cover_local_path")
                       and Path(g["cover_local_path"]).exists())
        has_meta = sum(1 for g in games
                      if (g.get("enrichment") or {}).get("description"))
        backports = sum(1 for g in games if g.get("is_backport") or
                       g.get("backport_text") or g.get("backport"))
        dlcs = sum(1 for g in games if g.get("has_dlc") or g.get("is_dlc"))
        missing_covers = total - has_cover

        # Storage: sum up sizes if we can
        covers_dir = data_dir() / "covers"
        cover_size_bytes = 0
        if covers_dir.exists():
            for f in covers_dir.iterdir():
                try:
                    cover_size_bytes += f.stat().st_size
                except Exception:
                    pass

        return {
            "ok": True,
            "total": total,
            "has_cover": has_cover,
            "has_meta": has_meta,
            "missing_covers": missing_covers,
            "missing_meta": total - has_meta,
            "backports": backports,
            "dlcs": dlcs,
            "cover_cache_mb": round(cover_size_bytes / 1_048_576, 1),
        }

    def clear_cache(self) -> dict:
        """Delete all cached cover images."""
        covers_dir = data_dir() / "covers"
        if not covers_dir.exists():
            return {"ok": True, "deleted": 0}
        deleted = 0
        for f in covers_dir.iterdir():
            try:
                f.unlink()
                deleted += 1
            except Exception:
                pass
        # Clear paths in library
        for src in DATA_FILES:
            try:
                lib = self.load_library(src)
                games = lib.get("games", [])
                for g in games:
                    g["cover_local_path"] = ""
                self._save(src, games)
            except Exception:
                pass
        return {"ok": True, "deleted": deleted}

    def repair_database(self, source: str = "exfat") -> dict:
        """Check and fix library integrity issues."""
        lib = self.load_library(source)
        games = lib.get("games", [])
        fixed = 0
        for g in games:
            changed = False
            # Ensure scraped_title
            if not g.get("scraped_title"):
                g["scraped_title"] = g.get("title", "")
                changed = True
            # Fix cover paths that no longer exist
            lp = g.get("cover_local_path", "")
            if lp and not Path(lp).exists():
                g["cover_local_path"] = ""
                changed = True
            if changed:
                fixed += 1
        self._save(source, games)
        return {"ok": True, "fixed": fixed, "total": len(games)}

    def get_duplicates(self, source: str = "exfat") -> dict:
        """Find games with duplicate titles or PPSAs."""
        lib = self.load_library(source)
        games = lib.get("games", [])
        seen_titles: dict = {}
        seen_ppsas: dict = {}
        dupes = []
        for i, g in enumerate(games):
            title_key = _norm_title(g.get("title", ""))
            ppsa = (g.get("ppsa") or "").strip().upper()
            if title_key and title_key in seen_titles:
                dupes.append({"index": i, "other_index": seen_titles[title_key],
                              "reason": "duplicate_title",
                              "title": g.get("title", "")})
            else:
                seen_titles[title_key] = i
            if ppsa and ppsa in seen_ppsas:
                dupes.append({"index": i, "other_index": seen_ppsas[ppsa],
                              "reason": "duplicate_ppsa", "ppsa": ppsa,
                              "title": g.get("title", "")})
            else:
                seen_ppsas[ppsa] = i
        return {"ok": True, "duplicates": dupes}

    def get_missing_files_info(self, source: str = "exfat") -> dict:
        """Return games with missing cover or metadata."""
        lib = self.load_library(source)
        games = lib.get("games", [])
        missing_covers = []
        missing_meta = []
        for i, g in enumerate(games):
            lp = g.get("cover_local_path", "")
            if not lp or not Path(lp).exists():
                missing_covers.append({"index": i, "title": g.get("title", ""),
                                      "ppsa": g.get("ppsa", "")})
            if not (g.get("enrichment") or {}).get("description"):
                missing_meta.append({"index": i, "title": g.get("title", ""),
                                    "ppsa": g.get("ppsa", "")})
        return {
            "ok": True,
            "missing_covers": missing_covers,
            "missing_meta": missing_meta,
        }

    # ------------------------------------------------------------------
    # Export / import / backups
    # ------------------------------------------------------------------

    def export_library(self, source: str = "exfat") -> dict:
        path = data_file_for(source)
        if not path.exists():
            return {"ok": False, "error": "No library to export — scrape first."}
        try:
            stamp = __import__("datetime").datetime.now().strftime("%Y%m%d")
            suggested = f"{path.stem}_export_{stamp}.json"
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                directory=str(Path.home()),
                save_filename=suggested,
            )
            if not result:
                return {"ok": False, "error": "Export cancelled."}
            dest = result[0] if isinstance(result, (list, tuple)) else result
            dest = Path(dest)
            if dest.suffix.lower() != ".json":
                dest = dest.with_suffix(".json")
            dest.write_bytes(path.read_bytes())
            return {"ok": True, "path": str(dest)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def import_library(self, source: str = "exfat") -> dict:
        try:
            result = self._window.create_file_dialog(
                webview.OPEN_DIALOG,
                directory=str(Path.home()),
                allow_multiple=False,
                file_types=("JSON files (*.json)", "All files (*.*)"),
            )
            if not result:
                return {"ok": False, "error": "Import cancelled."}
            src = result[0] if isinstance(result, (list, tuple)) else result
            src = Path(src)
            if not src.exists():
                return {"ok": False, "error": "That file no longer exists."}
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except Exception as e:
                return {"ok": False, "error": f"Not a valid JSON file: {e}"}
            if isinstance(data, dict) and isinstance(data.get("games"), list):
                games = data["games"]
            elif isinstance(data, list):
                games = data
            else:
                return {"ok": False, "error": "No 'games' list found."}
            if not games:
                return {"ok": False, "error": "Library file has no games."}
            sample = games[0]
            if not isinstance(sample, dict) or \
               not (sample.get("title") or sample.get("name")):
                return {"ok": False, "error": "Entries don't look like games."}
            self._save(source, games)
            return {"ok": True, "count": len(games),
                    "source": source, "file": src.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_backups_folder(self) -> dict:
        backups = data_dir() / "backups"
        backups.mkdir(exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(backups))
            elif sys.platform == "darwin":
                __import__("subprocess").Popen(["open", str(backups)])
            else:
                __import__("subprocess").Popen(["xdg-open", str(backups)])
            return {"ok": True, "path": str(backups)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ------------------------------------------------------------------
    # Re-scrape one game
    # ------------------------------------------------------------------

    def rescrape_one(self, game_index: int, source: str = "exfat") -> dict:
        """Re-scrape and re-enrich a single game (background thread)."""
        if self._busy:
            return {"ok": False, "error": "Already busy."}
        self._busy = True
        t = threading.Thread(target=self._rescrape_one_worker,
                             args=(game_index, source), daemon=True)
        t.start()
        return {"ok": True}

    def _rescrape_one_worker(self, idx: int, source: str) -> None:
        try:
            lib = self.load_library(source)
            games = lib.get("games", [])
            if idx < 0 or idx >= len(games):
                self._finish(False, "Game not found.")
                return
            game = games[idx]
            # Re-enrich
            if not self._settings.get("offline_mode"):
                self._progress(f"Re-enriching {game.get('title', '')}…", 0, 1)
                # Force re-fetch by clearing existing enrichment
                game["enrichment"] = {}
                enrich_game(game, self._settings)
                # Re-cache cover
                if self._settings.get("download_covers_on_scrape", True):
                    path = ensure_cover(game, data_dir(), force=True)
                    if path:
                        game["cover_local_path"] = path
            self._save(source, games)
            self._finish(True, f"Re-scraped {game.get('title', '')}.", 1)
        except Exception as e:
            self._finish(False, f"Re-scrape failed: {e}")
        finally:
            self._busy = False

    # ------------------------------------------------------------------
    # Window controls
    # ------------------------------------------------------------------

    def win_minimize(self) -> None:
        try:
            if self._window:
                self._window.minimize()
        except Exception:
            pass

    def win_toggle_maximize(self) -> None:
        try:
            if not self._window:
                return
            if getattr(self, "_maximized", False):
                self._window.restore()
                self._maximized = False
            else:
                self._window.maximize()
                self._maximized = True
        except Exception:
            pass

    def win_close(self) -> None:
        try:
            if self._window:
                self._window.destroy()
        except Exception:
            pass

    def win_move(self, dx: int, dy: int) -> None:
        """JS-driven window drag. Moves the window by (dx, dy) pixels from its
        current position. This is the primary drag mechanism — the custom
        frameless titlebar has no OS drag handle, so the UI calls this on
        titlebar mouse-drag."""
        try:
            if not self._window:
                return
            # pywebview 5+ exposes .x / .y; guard for safety.
            x = getattr(self._window, "x", None)
            y = getattr(self._window, "y", None)
            if x is None or y is None:
                return
            self._window.move(int(x) + int(dx), int(y) + int(dy))
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Misc
    # ------------------------------------------------------------------

    def open_url(self, url: str) -> None:
        import webbrowser
        webbrowser.open(url)

    def copy_to_clipboard(self, text: str) -> dict:
        try:
            if self._window:
                self._window.evaluate_js(
                    "navigator.clipboard.writeText(" + json.dumps(text) + ")")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def get_version(self) -> dict:
        return {"ok": True, "version": "2.8.0"}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    if not UI_FILE.exists():
        raise RuntimeError(
            f"UI file not found at: {UI_FILE}\n"
            "The ui/ folder was not bundled correctly. Rebuild with build_exe.bat.")

    api = Api()
    window = webview.create_window(
        title="exFAT Ripper",
        url=str(UI_FILE),
        js_api=api,
        width=1380,
        height=920,
        min_size=(1024, 680),
        background_color="#08090f",
        frameless=True,
        easy_drag=False,
    )
    api.bind(window)
    webview.start(http_server=True)


def _log_crash(exc: BaseException) -> Path:
    log_path = data_dir() / "exfat_app_error.log"
    try:
        with log_path.open("w", encoding="utf-8") as f:
            f.write("exFAT Ripper failed to start.\n\n")
            f.write(f"UI_FILE  = {UI_FILE}\n")
            f.write(f"frozen   = {getattr(sys, 'frozen', False)}\n\n")
            f.write("".join(traceback.format_exception(
                type(exc), exc, exc.__traceback__)))
    except Exception:
        pass
    return log_path


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        path = _log_crash(e)
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"exFAT Ripper could not start.\n\n{e}\n\nDetails: {path}",
                "exFAT Ripper — startup error",
                0x10,
            )
        except Exception:
            pass
        sys.exit(1)
