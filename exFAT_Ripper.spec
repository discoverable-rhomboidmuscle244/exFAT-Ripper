# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for PS5 exFAT Library.
# Build with:  pyinstaller PS5_exFAT_Library.spec
# or run the platform-specific build script.

import sys
import os
from pathlib import Path

block_cipher = None

# ── Platform detection ─────────────────────────────────────────────────────
IS_WIN = sys.platform == 'win32'
IS_MAC = sys.platform == 'darwin'
IS_LINUX = sys.platform.startswith('linux')

# ── Data files ─────────────────────────────────────────────────────────────
datas = [
    ('ui', 'ui'),
    ('core/dlpsgame_ps5_scraper.py', 'core'),
    ('app.png', '.'),
]

# ── Hidden imports ─────────────────────────────────────────────────────────
hiddenimports = [
    'webview',
    'webview.platforms.qt',
    'cloudscraper',
    'bs4',
    'requests',
    'cryptography',
    'cryptography.hazmat.primitives.kdf.pbkdf2',
    'cryptography.hazmat.primitives.ciphers.aead',
    'cryptography.hazmat.primitives.hashes',
    'cryptography.hazmat.backends.openssl',
]

# Platform-specific webview backends
if IS_WIN:
    hiddenimports += [
        'webview.platforms.winforms',
        'webview.platforms.edgechromium',
        'clr_loader',
        'clr_loader.netfx',
        'clr_loader.util',
    ]
elif IS_MAC:
    hiddenimports += [
        'webview.platforms.cocoa',
    ]
elif IS_LINUX:
    hiddenimports += [
        'webview.platforms.gtk',
        'webview.platforms.qt',
    ]

# ── Collect all webview + cloudscraper data ────────────────────────────────
from PyInstaller.utils.hooks import collect_all

_pw_datas, _pw_binaries, _pw_hidden = collect_all('webview')
hiddenimports += _pw_hidden

try:
    _cs_datas, _cs_binaries, _cs_hidden = collect_all('cloudscraper')
    _pw_datas += _cs_datas
    _pw_binaries += _cs_binaries
    hiddenimports += _cs_hidden
except Exception:
    pass

# ── Bundle Playwright Chromium browsers ────────────────────────────────────
def _find_playwright_browsers():
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    cands = []
    if env:
        cands.append(Path(env))
    if IS_WIN:
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "ms-playwright")
    elif IS_MAC:
        cands.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    else:  # Linux
        cands.append(Path.home() / ".cache" / "ms-playwright")
        cands.append(Path("/opt/pw-browsers"))
        cands.append(Path("/usr/local/share/ms-playwright"))
    
    for c in cands:
        if c and c.is_dir():
            return c
    return None

_pw_browsers = _find_playwright_browsers()
if _pw_browsers:
    for _child in _pw_browsers.iterdir():
        if _child.is_dir():
            _pw_datas.append((str(_child), f"ms-playwright/{_child.name}"))
    print(f"[spec] bundling Playwright browsers from: {_pw_browsers}")
else:
    print("[spec] WARNING: no ms-playwright folder found — the app will rely "
          "on a system Chromium or an attached Brave. Run "
          "'playwright install chromium' before building for a self-contained app.")

# ── Analysis ───────────────────────────────────────────────────────────────
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

# ── Platform-specific build output ─────────────────────────────────────────
if IS_WIN:
    # ── Windows: single .exe ─────────────────────────────────────────────
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

elif IS_MAC:
    # ── macOS: .app bundle ───────────────────────────────────────────────
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="exFAT Ripper",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=True,  # Required for macOS app bundles
        target_arch='universal2',  # Supports both Intel & Apple Silicon
        codesign_identity=None,
        entitlements_file=None,
        icon="app.icns" if os.path.exists("app.icns") else ("app.png" if os.path.exists("app.png") else None),
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="exFAT Ripper"
    )
    app = BUNDLE(
        coll,
        name="exFAT Ripper.app",
        icon="app.icns" if os.path.exists("app.icns") else ("app.png" if os.path.exists("app.png") else None),
        bundle_identifier="com.yourname.exfat-ripper",
        info_plist={
            'CFBundleDisplayName': 'exFAT Ripper',
            'CFBundleShortVersionString': '1.0.0',
            'CFBundleVersion': '1.0.0',
            'LSMinimumSystemVersion': '10.14',
            'NSHighResolutionCapable': 'True',
            'NSRequiresAquaSystemAppearance': 'False',
        },
    )

else:
    # ── Linux: single binary ─────────────────────────────────────────────
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
        strip=True,  # Strip debug symbols on Linux to reduce size
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon="app.png" if os.path.exists("app.png") else None,
    )