#!/usr/bin/env python3
"""
app.py
------
exFAT Ripper — desktop app.

A native window (via pywebview) showing a browsable game library with cover
art, PPSA, title, version and download links. Two actions:

  * Scrape  — fast full page-walk of the .exFAT site (~1-2 min). Captures
              every game's metadata + LOCKED link-lock URLs.
  * Update  — same walk, but only adds games not already in the library.

Download links are NOT unlocked during the scrape. They are decrypted
offline, instantly, the moment a user opens a game — so the scrape is fast
and clicks are immediate.

The library is stored as exfat_games.json next to the app.
"""
from __future__ import annotations

import json
import sys
import threading
import traceback
from pathlib import Path

import webview

from core.scraper import scrape_library, SITE_PASSWORD
from core.decrypt import resolve_game_links


# --- Where things live ---------------------------------------------------------
#
# Two different locations matter when frozen by PyInstaller:
#   * bundled read-only assets (ui/) are unpacked to a temp dir: sys._MEIPASS
#   * the writable library file must sit NEXT TO the .exe so it persists
#     between runs — a temp dir would be wiped.

def resource_dir() -> Path:
    """Folder holding bundled, read-only assets (the ui/ folder)."""
    if getattr(sys, "frozen", False):
        # PyInstaller unpacks bundled data here at runtime.
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    return Path(__file__).parent


def data_dir() -> Path:
    """Folder for the writable library file — persists between runs."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


# Each source keeps its own library file, both next to the exe.
DATA_FILES = {
    "exfat": data_dir() / "exfat_games.json",
}
UI_FILE = resource_dir() / "ui" / "index.html"


def data_file_for(source: str) -> Path:
    return DATA_FILES.get(source, DATA_FILES["exfat"])


def _norm_title(t: str) -> str:
    return " ".join((t or "").lower().split())


# --- The API object exposed to JavaScript --------------------------------------

class Api:
    """Every public method here is callable from the UI as window.pywebview.api.<name>()."""

    def __init__(self):
        self._window = None
        self._busy = False
        self._maximized = False

    def bind(self, window):
        self._window = window

    # ---- library load/save ----

    def load_library(self, source: str = "exfat") -> dict:
        """Return the stored exFAT library,
        or an empty shell if none exists yet."""
        path = data_file_for(source)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                games = data.get("games", [])
                return {"ok": True, "source": source,
                        "games": games, "total": len(games)}
            except Exception as e:
                return {"ok": False, "source": source,
                        "error": f"Could not read library: {e}",
                        "games": [], "total": 0}
        return {"ok": True, "source": source, "games": [], "total": 0}

    def _backup_existing(self, source: str) -> Path | None:
        """Before overwriting a library file, copy the current one into a
        backups/ folder next to the app, timestamped. Keeps the last 10."""
        path = data_file_for(source)
        if not path.exists():
            return None
        backups = data_dir() / "backups"
        backups.mkdir(exist_ok=True)
        stamp = __import__("datetime").datetime.now().strftime("%Y%m%d-%H%M%S")
        dest = backups / f"{path.stem}_{stamp}.json"
        # avoid clobbering if two saves land in the same second
        n = 2
        while dest.exists():
            dest = backups / f"{path.stem}_{stamp}-{n}.json"
            n += 1
        try:
            dest.write_bytes(path.read_bytes())
        except Exception:
            return None
        # prune: keep only the 10 most recent backups per source
        try:
            mine = sorted(backups.glob(f"{path.stem}_*.json"),
                          key=lambda p: p.stat().st_mtime, reverse=True)
            for old in mine[10:]:
                old.unlink(missing_ok=True)
        except Exception:
            pass
        return dest

    def _save(self, source: str, games: list[dict]) -> None:
        # Back up the previous library before replacing it.
        self._backup_existing(source)
        origin = "https://pippo26442999.github.io/.exFAT/"
        payload = {"source": origin, "total": len(games), "games": games}
        path = data_file_for(source)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(path)

    # ---- link resolution ----

    def get_links(self, game_index: int, source: str = "exfat",
                  password: str = SITE_PASSWORD) -> dict:
        """Return the download links for one game by index.

        exFAT links are link-lock encrypted -> decrypted offline here.
        Links are decrypted offline, instantly.
        """
        lib = self.load_library(source)
        games = lib.get("games", [])
        if game_index < 0 or game_index >= len(games):
            return {"ok": False, "error": "Game not found."}
        game = games[game_index]
        try:
            return {"ok": True, **resolve_game_links(game, password)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- scrape / update (background thread) ----

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

    def start_scrape(self, source: str = "exfat", mode: str = "full",
                     password: str = SITE_PASSWORD) -> dict:
        """Kick off a scrape on a background thread.
        source = 'exfat'
        mode   = 'full' (rebuild) | 'update' (add only new games)
        Progress arrives via window.onScrapeProgress; end via onScrapeDone.
        """
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

            # ---------- exFAT ----------
            self._progress("Starting browser...", 0, 0)
            scraped = scrape_library(
                password=password,
                progress=self._progress,
                headless=True,
            )
            if mode == "update" and existing:
                have = {_norm_title(g.get("title", "")) for g in existing}
                fresh = [g for g in scraped
                         if _norm_title(g.get("title", "")) not in have]
                merged = existing + fresh
                self._save(source, merged)
                self._finish(True,
                             f"Update complete — {len(fresh)} new game(s) "
                             f"added ({len(merged)} total).",
                             len(merged))
            else:
                self._save(source, scraped)
                self._finish(True,
                             f"Scrape complete — {len(scraped)} games.",
                             len(scraped))
        except Exception as e:
            self._finish(False, f"Scrape failed: {e}")
            traceback.print_exc()
        finally:
            self._busy = False

    # ---- window controls (for the custom frameless title bar) ----

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
            # pywebview tracks maximize state loosely; toggle both ways.
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

    # ---- misc ----

    def open_url(self, url: str) -> None:
        """Open a download link in the user's real browser."""
        import webbrowser
        webbrowser.open(url)

    def copy_to_clipboard(self, text: str) -> dict:
        """Best-effort clipboard copy (the UI also has its own JS fallback)."""
        try:
            if self._window:
                self._window.evaluate_js(
                    "navigator.clipboard.writeText("
                    + json.dumps(text) + ")")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ---- export / backups ----

    def export_library(self, source: str = "exfat") -> dict:
        """Export the current library to a location the user picks via a
        native Save-As dialog. Returns {ok, path} or {ok: False, error}."""
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
            # pywebview returns a str, a tuple, or None (cancelled)
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
        """Import a .json into the current source's library. The user picks
        the file via a native Open dialog. The existing library is backed up
        first (the backup system), then replaced. Returns {ok, count, ...}."""
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

            # parse + validate
            try:
                data = json.loads(src.read_text(encoding="utf-8"))
            except Exception as e:
                return {"ok": False,
                        "error": f"Not a valid JSON file: {e}"}

            # accept either {"games": [...]} or a bare [...] list
            if isinstance(data, dict) and isinstance(data.get("games"), list):
                games = data["games"]
            elif isinstance(data, list):
                games = data
            else:
                return {"ok": False,
                        "error": "This JSON doesn't look like a game "
                                 "library (no 'games' list found)."}

            if not games:
                return {"ok": False,
                        "error": "That library file has no games in it."}

            # light sanity check — entries should look like game dicts
            sample = games[0]
            if not isinstance(sample, dict) or \
               not (sample.get("title") or sample.get("name")):
                return {"ok": False,
                        "error": "The entries don't look like games "
                                 "(no title field)."}

            # _save backs up the existing library before overwriting it
            self._save(source, games)
            return {"ok": True, "count": len(games),
                    "source": source, "file": src.name}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def open_backups_folder(self) -> dict:
        """Open the backups folder in the system file manager."""
        backups = data_dir() / "backups"
        backups.mkdir(exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                __import__("os").startfile(str(backups))
            elif sys.platform == "darwin":
                __import__("subprocess").Popen(["open", str(backups)])
            else:
                __import__("subprocess").Popen(["xdg-open", str(backups)])
            return {"ok": True, "path": str(backups)}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main() -> None:
    if not UI_FILE.exists():
        raise RuntimeError(
            f"UI file not found at: {UI_FILE}\n"
            "The ui/ folder was not bundled correctly. Rebuild with "
            "build_exe.bat.")

    api = Api()
    window = webview.create_window(
        title="exFAT Ripper",
        url=str(UI_FILE),
        js_api=api,
        width=1320,
        height=880,
        min_size=(1000, 660),
        background_color="#0c0e15",
        frameless=True,
        easy_drag=False,        # we drag via the custom title bar only
    )
    api.bind(window)
    # http_server=True lets the local HTML load its own assets cleanly.
    webview.start(http_server=True)


def _log_crash(exc: BaseException) -> Path:
    """Write a startup crash to a log file next to the exe so a silent
    (console=False) failure is still diagnosable."""
    log_path = data_dir() / "exfat_app_error.log"
    try:
        with log_path.open("w", encoding="utf-8") as f:
            f.write("exFAT Ripper failed to start.\n\n")
            f.write(f"UI_FILE   = {UI_FILE}\n")
            f.write(f"DATA_FILE = {DATA_FILE}\n")
            f.write(f"frozen    = {getattr(sys, 'frozen', False)}\n\n")
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
        # Also try a last-ditch native message box so the user sees something.
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0,
                f"exFAT Ripper could not start.\n\n{e}\n\n"
                f"Details written to:\n{path}",
                "exFAT Ripper — startup error",
                0x10,  # MB_ICONERROR
            )
        except Exception:
            pass
        sys.exit(1)
