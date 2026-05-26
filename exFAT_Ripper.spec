# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PS5 exFAT Library.
# Build with:  pyinstaller PS5_exFAT_Library.spec
# or just run build_exe.bat which does everything.

import os

block_cipher = None

# Bundle the UI folder so index.html ships inside the exe.
datas = [
    ('ui', 'ui'),
    ('core/dlpsgame_ps5_scraper.py', 'core'),
    ('app.png', '.'),
]

# pywebview + cryptography sometimes need hidden imports spelled out.
# pywebview on Windows uses the Edge WebView2 (edgechromium) backend via
# .NET / clr_loader — PyInstaller frequently misses these.
hiddenimports = [
    'webview'
    'webview.platforms.winforms'
    'webview.platforms.edgechromium'
    'clr_loader'
    'clr_loader.netfx'
    'clr_loader.util'
    'cryptography'
    'cryptography.hazmat.primitives.kdf.pbkdf2'
    'cryptography.hazmat.primitives.ciphers.aead'
    'cryptography.hazmat.primitives.hashes'
    'cryptography.hazmat.backends.openssl'
    'cloudscraper'
    'bs4'
    'requests'
]

# Pull in all of pywebview's data/submodules so the winforms/edge backend
# and its bundled assets ship intact.
from PyInstaller.utils.hooks import collect_all
_pw_datas, _pw_binaries, _pw_hidden = collect_all('webview')
hiddenimports += _pw_hidden

# cloudscraper ships JS-challenge data files it needs at runtime.
try:
    _cs_datas, _cs_binaries, _cs_hidden = collect_all('cloudscraper')
    _pw_datas += _cs_datas
    _pw_binaries += _cs_binaries
    hiddenimports += _cs_hidden
except Exception:
    pass

# --- Bundle the Playwright Chromium browser ----------------------------------
# So the .exe is self-contained and works on a PC where `playwright install`
# was never run. We copy the ms-playwright browsers folder in as data, and a
# runtime hook (see runtime_hook_playwright.py) points Playwright at it.
import os as _os
from pathlib import Path as _Path

def _find_playwright_browsers():
    # Standard install locations for `playwright install chromium`.
    env = _os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    cands = []
    if env:
        cands.append(_Path(env))
    local = _os.environ.get("LOCALAPPDATA")
    if local:
        cands.append(_Path(local) / "ms-playwright")
    cands.append(_Path.home() / ".cache" / "ms-playwright")
    cands.append(_Path("/opt/pw-browsers"))
    for c in cands:
        if c and c.is_dir():
            return c
    return None

_pw_browsers = _find_playwright_browsers()
if _pw_browsers:
    # Bundle every browser folder under a known name inside the exe.
    for _child in _pw_browsers.iterdir():
        if _child.is_dir():
            _pw_datas.append((str(_child), f"ms-playwright/{_child.name}"))
    print(f"[spec] bundling Playwright browsers from: {_pw_browsers}")
else:
    print("[spec] WARNING: no ms-playwright folder found — the exe will rely "
          "on a system Chromium or an attached Brave. Run "
          "'playwright install chromium' before building for a self-contained exe.")

a = Analysis(
    ["app.py"],
    pathex=[],
    binaries=_pw_binaries,
    datas=datas + _pw_datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=["runtime_hook_playwright.py"],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="exFAT Ripper",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="app.ico" if os.path.exists("app.ico") else None,
)
