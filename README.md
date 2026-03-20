# GIF Screen Recorder

A lightweight macOS menu bar app for recording any region of your screen to **GIF** or **MP4**.  
No window. No bloat. Lives quietly in your menu bar until you need it.

Made with Claude

![macOS](https://img.shields.io/badge/macOS-13%2B-black)
![Python](https://img.shields.io/badge/Python-3.12%2B-blue)
![License](https://img.shields.io/badge/license-GNUv3-green)

---

## Features

- **Menu bar app** — always one click away, no dock icon
- **Region selection** — native macOS crosshair via `screencapture -i`, exact coordinates via a passive CGEvent tap
- **GIF export** — global palette quantization with saturation boost for vivid colors
- **MP4 export** — H.264 via FFmpeg, piped directly (no SDK dependency issues)
- **Time-lapse mode** — capture as slow as 1/10 fps, play back at 10 fps
- **19 FPS presets** — from 1/10 fps up to 30 fps covering all standard rates
- **Output folder** — preset locations (Desktop, Pictures, Movies, Downloads) + remembers recent custom paths
- **Global hotkey** — ⌥Space toggles recording from anywhere
- **Multi-display** — works across all connected screens
- **Auto-reveal** — opens Finder with the saved file selected after every recording

---

## Requirements

- macOS 13 (Ventura) or later
- Python 3.12+
- Xcode Command Line Tools (`xcode-select --install`)

For MP4 export (optional):

```bash
brew install ffmpeg
```

---

## Run from source

```bash
# Install dependencies
pip3 install mss Pillow rumps pynput

# Run
python3 screen_recorder.py
```

**Permissions required on first run:**

- **Screen Recording** — System Settings → Privacy & Security → Screen Recording
- **Accessibility** — System Settings → Privacy & Security → Accessibility  
  (needed for the region coordinate tap — grant access to Terminal or your Python binary)

---

## Build a .app

```bash
# One-time: install librsvg for icon generation
brew install librsvg

# Build
bash build_mac.sh
```

This will:

1. Install all Python dependencies
2. Generate `AppIcon.icns` from the SVG source
3. Compile the Swift coord tap binary
4. Bundle everything into `dist/GIF Screen Recorder.app` using PyInstaller

Then install:

```bash
cp -r "dist/GIF Screen Recorder.app" /Applications/
```

On first launch macOS may show a security prompt — go to  
System Settings → Privacy & Security → Open Anyway.

---

## Project structure

```
├── screen_recorder.py   # Main application
├── build_mac.sh         # Build script → produces .app
├── create_icon.sh       # Generates AppIcon.icns from SVG
└── README.md
```

The Swift coord tap source is embedded inside `screen_recorder.py` as `COORD_TAP_SRC` and compiled automatically on first launch to `~/.screen_recorder_coord_tap`.

---

## How it works

### Region selection

1. A passive Swift `CGEvent` tap (`listenOnly`) is launched alongside `screencapture -i`
2. The tap signals `ready` once registered — Python waits for this before launching screencapture, ensuring the first drag is always captured
3. `screencapture -i` provides the native macOS crosshair UI
4. On mouseUp the tap prints `x,y,w,h` to stdout and exits

### GIF encoding

Frames are captured in memory as PIL Images. On stop:

- A global palette is built from up to 20 evenly sampled frames
- Saturation is boosted 30% before quantization to compensate for the 256-color limit
- `LIBIMAGEQUANT` is used if available (best quality), otherwise `MEDIANCUT`
- Frames are quantized against the shared palette with dithering disabled

### MP4 encoding

Raw RGB24 frames are piped to FFmpeg's stdin:

```
ffmpeg -f rawvideo -pix_fmt rgb24 … -vcodec libx264 -crf 18 output.mp4
```

No OpenCV, no binary SDK compatibility issues.

---

## Permissions explained

| Permission       | Why                                                                            |
| ---------------- | ------------------------------------------------------------------------------ |
| Screen Recording | Required to capture screen content via `mss` and `screencapture`               |
| Accessibility    | Required for the CGEvent tap to read mouse coordinates during region selection |

---

## Known limitations

- macOS only (uses `screencapture`, `CGEvent`, AppKit)
- MP4 export requires FFmpeg installed separately (`brew install ffmpeg`)
- GIF palette is limited to 256 colors — for photographic content MP4 gives better results
- At the first screen region selection

---

## License

GNU v3
