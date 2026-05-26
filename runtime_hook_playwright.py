"""
runtime_hook_playwright.py
--------------------------
PyInstaller runtime hook. Runs the instant the frozen exe starts, before any
app code. Points Playwright at the Chromium that was bundled into the exe
(unpacked to sys._MEIPASS/ms-playwright), so the app is self-contained.

If the user has set PLAYWRIGHT_BROWSERS_PATH themselves, that's respected.
"""
import os
import sys

if getattr(sys, "frozen", False):
    _bundled = os.path.join(getattr(sys, "_MEIPASS", ""), "ms-playwright")
    if os.path.isdir(_bundled) and not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = _bundled
