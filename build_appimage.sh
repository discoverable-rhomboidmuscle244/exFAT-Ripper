#!/bin/bash
# ============================================================
#  Build exFAT Ripper into a Linux AppImage
#  Just run: chmod +x build_appimage.sh && ./build_appimage.sh
#  You need Python 3.10+ installed.
# ============================================================

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo ""
echo "============================================"
echo "   exFAT Ripper  -  Linux AppImage builder"
echo "============================================"
echo ""

# ---- 1. find Python ----
PY=""

if command -v python3 &> /dev/null; then
    PY="python3"
elif command -v python &> /dev/null; then
    PY="python"
fi

if [ -z "$PY" ]; then
    echo "[X] Python was not found."
    echo ""
    echo "    Install Python 3.10 or newer:"
    echo "        Ubuntu/Debian:  sudo apt install python3 python3-pip"
    echo "        Fedora:         sudo dnf install python3 python3-pip"
    echo "        Arch:           sudo pacman -S python python-pip"
    echo ""
    exit 1
fi

echo "[1/5] Python found (using \"$PY\")"
$PY --version
echo ""

# ---- 2. install dependencies ----
echo "[2/5] Installing dependencies..."
echo "      (first time this can take several minutes)"
echo ""
$PY -m pip install --upgrade pip
$PY -m pip install -r requirements.txt
if [ $? -ne 0 ]; then
    echo ""
    echo "[X] Failed to install dependencies."
    echo "    A common fix: run this build again (pip sometimes needs a retry)."
    exit 1
fi
echo ""

# ---- 3. install Playwright Chromium ----
echo "[3/5] Installing the Chromium engine for Playwright..."
echo ""
$PY -m playwright install chromium || {
    echo ""
    echo "[!] Playwright browser install had a problem - continuing anyway."
    echo "    The app can still work if you run it with Brave/Chrome already"
    echo "    open on the debug port. You can also retry this step later with:"
    echo "        $PY! -m playwright install chromium"
    echo ""
}

# ---- 4. build with PyInstaller ----
echo "[4/5] Building the Linux binary with PyInstaller..."
echo "      (this bundles Chromium so the binary is self-contained -"
echo "       it makes the build slow and the output large, ~200-300 MB)"
echo ""
$PY -m PyInstaller --noconfirm --clean exFAT_Ripper.spec
if [ $? -ne 0 ]; then
    echo ""
    echo "[X] Build failed. Read the PyInstaller messages above."
    exit 1
fi
echo ""

# ---- 5. package as AppImage ----
echo "[5/5] Packaging as AppImage..."
echo ""

# Download appimagetool if not present
APPIMAGETOOL="$SCRIPT_DIR/appimagetool-x86_64.AppImage"
if [ ! -f "$APPIMAGETOOL" ]; then
    echo "      Downloading appimagetool..."
    wget -q "https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage" -O "$APPIMAGETOOL"
    chmod +x "$APPIMAGETOOL"
fi

# Create AppDir structure
APPDIR="$SCRIPT_DIR/AppDir"
rm -rf "$APPDIR"
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"

# Copy the built binary
cp "$SCRIPT_DIR/dist/exFAT Ripper" "$APPDIR/usr/bin/exfat-ripper" 2>/dev/null || \
cp "$SCRIPT_DIR/dist/exFAT_Ripper" "$APPDIR/usr/bin/exfat-ripper" 2>/dev/null || \
cp "$SCRIPT_DIR/dist/exFATRipper" "$APPDIR/usr/bin/exfat-ripper" 2>/dev/null || {
    echo "[X] Could not find the built binary in dist/"
    echo "    Check what PyInstaller produced in the dist/ folder."
    exit 1
}

# Create .desktop file
cat > "$APPDIR/usr/share/applications/exfat-ripper.desktop" << 'EOF'
[Desktop Entry]
Name=exFAT Ripper
Comment=Game library scraper and launcher
Exec=exfat-ripper
Icon=exfat-ripper
Type=Application
Categories=Game;Utility;
Terminal=false
EOF

# Create a symlink for the desktop file
ln -s "usr/share/applications/exfat-ripper.desktop" "$APPDIR/exfat-ripper.desktop"

# Copy icon (fallback to a generic one if not found)
if [ -f "$SCRIPT_DIR/app.png" ]; then
    cp "$SCRIPT_DIR/app.png" "$APPDIR/usr/share/icons/hicolor/256x256/apps/exfat-ripper.png"
    ln -s "usr/share/icons/hicolor/256x256/apps/exfat-ripper.png" "$APPDIR/exfat-ripper.png"
else
    echo "      [!] No app.png found, AppImage will use default icon"
fi

# Create AppRun launcher
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PATH="$HERE/usr/bin:$PATH"
exec "$HERE/usr/bin/exfat-ripper" "$@"
EOF
chmod +x "$APPDIR/AppRun"

# Build the AppImage
ARCH=x86_64 "$APPIMAGETOOL" "$APPDIR" "$SCRIPT_DIR/dist/exFAT_Ripper-x86_64.AppImage"

echo ""
echo "============================================"
echo "  DONE."
echo ""
echo "  Your AppImage is here:"
echo "      dist/exFAT_Ripper-x86_64.AppImage"
echo ""
echo "  Make it executable and run:"
echo "      chmod +x dist/exFAT_Ripper-x86_64.AppImage"
echo "      ./dist/exFAT_Ripper-x86_64.AppImage"
echo ""
echo "  The first scrape builds the library"
echo "  (1-2 minutes); after that it opens instantly."
echo "============================================"
echo ""