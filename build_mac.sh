#!/bin/bash
# ─────────────────────────────────────────────────────────────
#  build_mac.sh — builds GIF Screen Recorder into a macOS .app
#  Run from the project root (where screen_recorder.py lives)
# ─────────────────────────────────────────────────────────────
set -e

echo "📦  Installing Python dependencies…"
pip3 install --quiet mss Pillow rumps pynput pyinstaller

# ── App icon (must exist before PyInstaller runs)
if [ ! -f "AppIcon.icns" ]; then
    echo "🎨  Creating app icon…"
    bash create_icon.sh || echo "⚠️   Icon creation failed — using default icon"
fi

# ── Coord tap binary
COORD_TAP_BIN="$HOME/.screen_recorder_coord_tap"
if [ ! -f "$COORD_TAP_BIN" ]; then
    echo "🔨  Compiling coord tap…"
    python3 -c "
import sys; sys.path.insert(0, '.')
from screen_recorder import COORD_TAP_SRC
with open('/tmp/_coord_tap.swift', 'w') as f: f.write(COORD_TAP_SRC)
"
    # Compile with conservative deployment target to avoid dyld version mismatch
    swiftc \
        -target arm64-apple-macos13.0 \
        -o "$COORD_TAP_BIN" /tmp/_coord_tap.swift
    rm -f /tmp/_coord_tap.swift
    echo "✔   Coord tap compiled"
else
    echo "✔   Coord tap already compiled"
fi

# ── FFmpeg (optional — MP4 export only)
FFMPEG_PATH=""
for p in \
    "/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg" \
    "/opt/homebrew/opt/ffmpeg@6/bin/ffmpeg" \
    "/opt/homebrew/bin/ffmpeg" \
    "/usr/local/opt/ffmpeg@7/bin/ffmpeg" \
    "/usr/local/bin/ffmpeg" \
    "$(which ffmpeg 2>/dev/null)"; do
    [ -f "$p" ] && FFMPEG_PATH="$p" && break
done

# ── Always clean previous build so icon + spec are fresh
echo "🧹  Cleaning previous build…"
rm -rf build dist "GIF Screen Recorder.spec"

# ── Build args
PYINSTALLER_ARGS=(
    --onedir
    --windowed
    --name "GIF Screen Recorder"
    --collect-all rumps
    --add-binary "$COORD_TAP_BIN:."
)

[ -f "AppIcon.icns" ] && PYINSTALLER_ARGS+=(--icon AppIcon.icns)

if [ -z "$FFMPEG_PATH" ]; then
    echo "⚠️   ffmpeg not found — MP4 export disabled (brew install ffmpeg to enable)"
else
    echo "✔   FFmpeg: $("$FFMPEG_PATH" -version 2>&1 | head -1)"
    PYINSTALLER_ARGS+=(--add-binary "$FFMPEG_PATH:.")
fi

# ── Build with deployment target set to macOS 13
# This prevents the "macOS 26 required, have 16" dyld mismatch
echo "🔨  Building .app…"
MACOSX_DEPLOYMENT_TARGET=13.0 pyinstaller "${PYINSTALLER_ARGS[@]}" screen_recorder.py

# ── Patch all bundled binaries to lower deployment target
# Some Python extensions may still have a high minos — patch them all down
echo "🔧  Patching deployment targets…"
find "dist/GIF Screen Recorder.app" \
    \( -name "*.so" -o -name "*.dylib" \) \
    -exec vtool -set-build-version macos 13.0 13.0 -replace -output {} {} \; 2>/dev/null || true

# Re-sign after patching
echo "✍️   Re-signing…"
codesign --force --deep --sign - "dist/GIF Screen Recorder.app" 2>/dev/null || true

echo ""
echo "✅  Built:  dist/GIF Screen Recorder.app"
echo ""
echo "First launch — macOS will ask for two permissions:"
echo "  • Screen Recording   (required)"
echo "  • Accessibility      (required for region selection)"
echo ""
echo "If macOS blocks the app:"
echo "  System Settings → Privacy & Security → Open Anyway"
echo ""
echo "To install:"
echo "  rm -rf '/Applications/GIF Screen Recorder.app'"
echo "  cp -r 'dist/GIF Screen Recorder.app' /Applications/"
