#!/usr/bin/env python3
"""
GIF Screen Recorder — macOS menu bar app
-----------------------------------------
Lives in the menu bar. Select a screen region, set FPS,
choose GIF or MP4, record. Saves to your output folder
and reveals the file in Finder when done.

Requirements:
    pip install mss Pillow rumps pynput

For MP4 export:
    brew install ffmpeg

Usage:
    python3 screen_recorder.py
"""

import threading
import time
import os
import subprocess
import shutil
from datetime import datetime

import rumps
from PIL import Image
import mss
try:
    from pynput import keyboard as _kb
    PYNPUT_OK = True
except ImportError:
    PYNPUT_OK = False


# ─────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────

def _notify(title, message, reveal_path=None):
    """Reveal saved file in Finder immediately. No osascript notifications."""
    if reveal_path and os.path.exists(reveal_path):
        subprocess.Popen(["open", "-R", reveal_path])

def _alert(message):
    subprocess.run(
        ["osascript", "-e",
         f'tell application "System Events" to display alert "{message}"'],
        capture_output=True
    )

def _set_cursor(kind):
    """
    Set the system cursor using a compiled Swift one-liner.
    kind: "wait" (spinning beachball) | "arrow" (normal)
    We use NSCursor directly — no window needed.
    """
    if kind == "wait":
        script = (
            "import Cocoa;"
            "NSCursor.operationNotAllowed.push();"
            "RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))"
        )
    else:
        script = (
            "import Cocoa;"
            "NSCursor.pop();"
            "RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05))"
        )
    # swiftc one-liner is too slow; use osascript with delay cursor trick instead
    # Simplest reliable approach: set the app's cursor via NSApp activation
    pass  # cursor is set implicitly by screencapture taking over

def _find_ffmpeg():
    import sys
    bundle_dir = getattr(sys, "_MEIPASS",
                         os.path.dirname(os.path.abspath(__file__)))
    for p in [
        os.path.join(bundle_dir, "ffmpeg"),
        "/opt/homebrew/opt/ffmpeg@7/bin/ffmpeg",
        "/opt/homebrew/opt/ffmpeg@6/bin/ffmpeg",
        "/opt/homebrew/bin/ffmpeg",
        "/usr/local/opt/ffmpeg@7/bin/ffmpeg",
        "/usr/local/bin/ffmpeg",
        shutil.which("ffmpeg"),
    ]:
        if p and os.path.isfile(p):
            return p
    return None

FFMPEG_BIN = _find_ffmpeg()


# ─────────────────────────────────────────────
#  Coord tap — passive mouse coordinate reader
# ─────────────────────────────────────────────
# Compiled once at first launch. Runs alongside screencapture -i
# to read exact drag coordinates. No pixel matching needed.

COORD_TAP_SRC = r"""
import Cocoa
import Darwin

// Passive CGEvent tap — records drag (mouseDown → mouseUp) coordinates.
// Ignores clicks in first 0.8s (screencapture toolbar activation clicks).
// Flushes stdout on every print and on SIGINT for reliable pipe reading.
// Uses .listenOnly — purely passive, never intercepts events.

var downPt    = CGPoint.zero
var downValid = false

// Handle SIGINT gracefully — flush and exit cleanly
signal(SIGINT) { _ in
    fflush(stdout)
    exit(0)
}

let mask: CGEventMask =
      (1 << CGEventType.leftMouseDown.rawValue)
    | (1 << CGEventType.leftMouseUp.rawValue)

// Get primary screen height for toolbar detection
let screenH = NSScreen.main?.frame.height ?? 800.0

let cb: CGEventTapCallBack = { _, type, event, _ in
    let loc = event.location

    if type == .leftMouseDown {
        // Ignore clicks in the top 60pt — that's where screencapture's
        // toolbar/cancel button lives. A real selection drag never starts there.
        guard loc.y > 60 else {
            return Unmanaged.passRetained(event)
        }
        downPt    = loc
        downValid = true

    } else if type == .leftMouseUp && downValid {
        let up = event.location
        let x  = Int(min(downPt.x, up.x))
        let y  = Int(min(downPt.y, up.y))
        let w  = Int(abs(up.x - downPt.x))
        let h  = Int(abs(up.y - downPt.y))
        // Only emit if it was a real drag (not just a click)
        if w > 5 && h > 5 {
            print("\(x),\(y),\(w),\(h)")
            fflush(stdout)
        }
        downValid = false
    }
    return Unmanaged.passRetained(event)
}

guard let tap = CGEvent.tapCreate(
    tap: .cgSessionEventTap,
    place: .tailAppendEventTap,
    options: .listenOnly,
    eventsOfInterest: mask,
    callback: cb,
    userInfo: nil
) else {
    fputs("no_accessibility\n", stderr)
    fflush(stderr)
    exit(1)
}

let src = CFMachPortCreateRunLoopSource(kCFAllocatorDefault, tap, 0)
CFRunLoopAddSource(CFRunLoopGetMain(), src, .commonModes)
CGEvent.tapEnable(tap: tap, enable: true)
// Signal to parent that tap is registered and ready
fputs("ready\n", stderr)
fflush(stderr)
RunLoop.main.run()
"""

COORD_TAP_BIN = None

def _find_coord_tap():
    import sys
    bundle_dir = getattr(sys, "_MEIPASS",
                         os.path.dirname(os.path.abspath(__file__)))
    for p in [
        os.path.join(bundle_dir, "screen_recorder_coord_tap"),
        os.path.expanduser("~/.screen_recorder_coord_tap"),
    ]:
        if os.path.isfile(p):
            return p
    return None

def _compile_coord_tap():
    global COORD_TAP_BIN
    cached = os.path.expanduser("~/.screen_recorder_coord_tap")
    src    = cached + ".swift"
    print("Compiling coord tap (one-time)…")
    with open(src, "w") as f:
        f.write(COORD_TAP_SRC)
    r = subprocess.run(["swiftc", "-o", cached, src],
                       capture_output=True, text=True)
    try: os.remove(src)
    except Exception: pass
    if r.returncode != 0:
        print(f"coord tap compile error: {r.stderr}")
        return
    COORD_TAP_BIN = cached
    print("Coord tap ready.")

COORD_TAP_BIN = _find_coord_tap()
_coord_tap_ready = threading.Event()
if COORD_TAP_BIN:
    _coord_tap_ready.set()

def _compile_coord_tap_bg():
    _compile_coord_tap()
    _coord_tap_ready.set()



# ─────────────────────────────────────────────
#  FPS presets
# ─────────────────────────────────────────────

FPS_STEPS = [
    ("1/10 fps", 0.10),
    ("1/5 fps",  0.20),
    ("1/4 fps",  0.25),
    ("1/2 fps",  0.50),
    ("1 fps",    1.0),
    ("2 fps",    2.0),
    ("3 fps",    3.0),
    ("4 fps",    4.0),
    ("5 fps",    5.0),
    ("6 fps",    6.0),
    ("8 fps",    8.0),
    ("10 fps",  10.0),
    ("12 fps",  12.0),
    ("15 fps",  15.0),
    ("18 fps",  18.0),
    ("20 fps",  20.0),
    ("24 fps",  24.0),
    ("25 fps",  25.0),
    ("30 fps",  30.0),
]
DEFAULT_FPS_IDX = 11  # 10 fps


# ─────────────────────────────────────────────
#  Region selector — screencapture -i + Pillow crop matching
# ─────────────────────────────────────────────
#
#  Uses macOS built-in screencapture -i which provides:
#    - Native crosshair cursor
#    - Proper click interception (no click-through)
#    - Multi-screen support
#    - No AppKit/Swift/Accessibility needed
#
#  To get coordinates: take a silent full screenshot before,
#  then find the cropped region inside it using Pillow.
#  Uses a stride-based search (every `step` pixels) for speed,
#  then refines to exact pixel.

_selector_lock = threading.Lock()

def select_region():
    """
    Returns (x, y, w, h) in mss coordinates or None if cancelled.
    """
    if not _selector_lock.acquire(blocking=False):
        return None
    try:
        return _select_region_impl()
    finally:
        _selector_lock.release()

def _get_display_info():
    """
    Returns list of (display_index, logical_x, logical_y, logical_w, logical_h, scale)
    for each connected screen, using osascript to get logical bounds.
    mss display indices start at 1 (0 = all screens combined).
    """
    # Get logical screen rects via osascript
    script = """
tell application "System Events"
    set info to {}
    repeat with d in (get every desktop)
        set bounds to bounds of (first window of d)
    end repeat
end tell
"""
    # Simpler: use NSScreen via mss which already enumerates monitors
    displays = []
    with mss.mss() as sct:
        # sct.monitors[0] = all screens combined, [1..n] = individual screens
        for i, mon in enumerate(sct.monitors[1:], start=1):
            displays.append({
                "idx":   i,
                "x":     mon["left"],
                "y":     mon["top"],
                "w":     mon["width"],
                "h":     mon["height"],
            })
    return displays


def _locate_region(tmp_region_path, ref_shots=None):
    """
    Find where the cropped region PNG sits across all displays.
    ref_shots: {display_idx: path} taken BEFORE the drag (preferred).
               Falls back to fresh screenshots if None.
    Returns (x, y, w, h) in mss coords or None.
    """
    with mss.mss() as sct:
        displays = [
            {"idx": i, "x": m["left"], "y": m["top"],
             "w": m["width"], "h": m["height"]}
            for i, m in enumerate(sct.monitors[1:], start=1)
        ]
    display_shots = []
    owned_tmps = []
    for d in displays:
        if ref_shots and d["idx"] in ref_shots:
            tmp_d = ref_shots[d["idx"]]
        else:
            tmp_d = f"/tmp/_sr_d{d['idx']}.png"
            r = subprocess.run(
                ["screencapture", "-x", "-D", str(d["idx"]), tmp_d],
                capture_output=True
            )
            if r.returncode != 0 or not os.path.exists(tmp_d):
                continue
            owned_tmps.append(tmp_d)
        if os.path.exists(tmp_d):
            img = Image.open(tmp_d).convert("RGB")
            pw, ph = img.size
            scale = pw / d["w"] if d["w"] > 0 else 2.0
            display_shots.append({**d, "img": img, "scale": scale,
                                   "pw": pw, "ph": ph, "tmp": tmp_d})
    try:
        region = Image.open(tmp_region_path).convert("RGB")
        rw, rh = region.size
        if rw < 4 or rh < 4:
            return None
        try:
            import numpy as np
            _search = _search_numpy
        except ImportError:
            _search = _search_pillow
        best = None
        best_score = float("inf")
        for d in display_shots:
            result = _search(region, d)
            if result and result[4] < best_score:
                best = result[:4]
                best_score = result[4]
        return best
    except Exception as e:
        print(f"_locate_region error: {e}")
        return None
    finally:
        for p in owned_tmps:
            try: os.remove(p)
            except Exception: pass


def _select_region_impl():
    """
    Fast multi-display region selection:
    1. Launch screencapture -i (user drags, native crosshair)
    2. Immediately take a full screenshot of each display
    3. Use numpy to find the crop location in O(n) time
    Falls back to pure-Pillow if numpy unavailable.
    """
    tmp_region = "/tmp/_sr_region.png"
    if os.path.exists(tmp_region):
        os.remove(tmp_region)

    # Get display layout from mss
    with mss.mss() as sct:
        displays = [
            {"idx": i, "x": m["left"], "y": m["top"],
             "w": m["width"], "h": m["height"]}
            for i, m in enumerate(sct.monitors[1:], start=1)
        ]

    if not displays:
        return None

    # Launch screencapture -i in background — user drags crosshair
    proc = subprocess.Popen(
        ["screencapture", "-i", "-x", tmp_region],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )

    # Wait for it to finish (user completes drag or cancels)
    proc.wait()

    if proc.returncode != 0 or not os.path.exists(tmp_region):
        return None

    # Take per-display screenshots NOW (after user finished dragging)
    display_shots = []
    for d in displays:
        tmp_d = f"/tmp/_sr_d{d['idx']}.png"
        r = subprocess.run(
            ["screencapture", "-x", "-D", str(d["idx"]), tmp_d],
            capture_output=True
        )
        if r.returncode == 0 and os.path.exists(tmp_d):
            img = Image.open(tmp_d).convert("RGB")
            pw, ph = img.size
            scale = pw / d["w"] if d["w"] > 0 else 2.0
            display_shots.append({**d, "img": img, "scale": scale,
                                   "pw": pw, "ph": ph, "tmp": tmp_d})

    try:
        region = Image.open(tmp_region).convert("RGB")
        rw, rh = region.size
        if rw < 4 or rh < 4:
            return None

        # Try numpy for fast search first
        try:
            import numpy as np
            _search = _search_numpy
        except ImportError:
            _search = _search_pillow

        best = None
        best_score = float("inf")
        for d in display_shots:
            result = _search(region, d)
            if result and result[4] < best_score:
                best = result[:4]
                best_score = result[4]

        return best

    except Exception as e:
        print(f"select_region error: {e}")
        return None
    finally:
        try: os.remove(tmp_region)
        except Exception: pass
        for d in display_shots:
            try: os.remove(d["tmp"])
            except Exception: pass


def _make_probes(region_arr, rw, rh, probe_size):
    """
    Build 4 probe samples from distinct positions in the region:
    top-left, top-right, bottom-left, center.
    Returns list of (row_offset, col_offset, numpy_array).
    Using multiple probes eliminates false positives from repeated patterns.
    """
    ps = probe_size
    probes = []
    candidates = [
        (0,            0),               # top-left
        (0,            max(0, rw-ps)),   # top-right
        (max(0,rh-ps), 0),               # bottom-left
        (max(0,(rh-ps)//2), max(0,(rw-ps)//2)),  # center
    ]
    seen = set()
    for ro, co in candidates:
        key = (ro, co)
        if key not in seen:
            seen.add(key)
            probes.append((ro, co, region_arr[ro:ro+ps, co:co+ps]))
    return probes


def _search_numpy(region, d):
    """
    Multi-probe search using numpy.
    Stage 1 — coarse scan with a single small probe to collect candidates.
    Stage 2 — verify each candidate against ALL probes; pick lowest total diff.
    This eliminates false positives when the corner pattern repeats on screen.
    """
    import numpy as np
    full  = d["img"]
    scale = d["scale"]
    fw, fh = full.size
    rw, rh = region.size

    if rw > fw or rh > fh:
        return None

    full_arr   = np.array(full,   dtype=np.int16)
    region_arr = np.array(region, dtype=np.int16)

    coarse     = max(2, int(scale))
    probe_size = min(32, rw // 2, rh // 2)
    probes     = _make_probes(region_arr, rw, rh, probe_size)
    ps         = probe_size

    # Stage 1: coarse scan using first probe only (fast)
    primary_ro, primary_co, primary_sample = probes[0]
    coarse_threshold = ps * ps * 3 * 6   # per-channel tolerance
    candidates = []   # list of (diff, fx, fy)

    for fy in range(0, fh - rh + 1, coarse):
        for fx in range(0, fw - rw + 1, coarse):
            patch = full_arr[fy + primary_ro : fy + primary_ro + ps,
                             fx + primary_co : fx + primary_co + ps]
            diff = int(np.abs(primary_sample - patch).sum())
            if diff < coarse_threshold:
                candidates.append((diff, fx, fy))

    if not candidates:
        # Relax threshold and keep best single match
        best_diff = float("inf")
        best_fx = best_fy = 0
        for fy in range(0, fh - rh + 1, coarse):
            for fx in range(0, fw - rw + 1, coarse):
                patch = full_arr[fy + primary_ro : fy + primary_ro + ps,
                                 fx + primary_co : fx + primary_co + ps]
                diff = int(np.abs(primary_sample - patch).sum())
                if diff < best_diff:
                    best_diff = diff
                    best_fx, best_fy = fx, fy
        candidates = [(best_diff, best_fx, best_fy)]

    # Stage 2: verify candidates with ALL probes, refine to pixel resolution
    best_total = float("inf")
    best_fx = best_fy = 0

    for _, cx, cy in sorted(candidates)[:20]:   # cap at 20 candidates
        # Refine within ±coarse around each candidate
        for fy in range(max(0, cy - coarse), min(fh - rh + 1, cy + coarse + 1)):
            for fx in range(max(0, cx - coarse), min(fw - rw + 1, cx + coarse + 1)):
                total = 0
                for ro, co, sample in probes:
                    patch = full_arr[fy + ro : fy + ro + ps,
                                     fx + co : fx + co + ps]
                    total += int(np.abs(sample - patch).sum())
                if total < best_total:
                    best_total = total
                    best_fx, best_fy = fx, fy

    lx = d["x"] + int(round(best_fx / scale))
    ly = d["y"] + int(round(best_fy / scale))
    lw = max(1, int(round(rw / scale)))
    lh = max(1, int(round(rh / scale)))
    return (lx, ly, lw, lh, best_total)


def _search_pillow(region, d):
    """
    Multi-probe search using Pillow — fallback when numpy unavailable.
    Same two-stage logic as _search_numpy but uses tobytes() comparisons.
    """
    full  = d["img"]
    scale = d["scale"]
    fw, fh = full.size
    rw, rh = region.size
    if rw > fw or rh > fh:
        return None

    coarse     = max(4, int(scale) * 2)
    probe_size = min(32, rw // 2, rh // 2)
    ps         = probe_size

    # Four probe positions
    probe_specs = [
        (0,            0),
        (0,            max(0, rw - ps)),
        (max(0,rh-ps), 0),
        (max(0,(rh-ps)//2), max(0,(rw-ps)//2)),
    ]
    seen, probes = set(), []
    for ro, co in probe_specs:
        if (ro, co) not in seen:
            seen.add((ro, co))
            sample = region.crop((co, ro, co+ps, ro+ps)).tobytes()
            probes.append((ro, co, sample))

    primary_ro, primary_co, primary_bytes = probes[0]
    threshold = ps * ps * 3 * 6
    candidates = []

    for fy in range(0, fh - rh + 1, coarse):
        for fx in range(0, fw - rw + 1, coarse):
            patch = full.crop((fx+primary_co, fy+primary_ro,
                               fx+primary_co+ps, fy+primary_ro+ps)).tobytes()
            diff = sum(abs(a-b) for a, b in zip(primary_bytes, patch))
            if diff < threshold:
                candidates.append((diff, fx, fy))

    if not candidates:
        best_diff = float("inf")
        best_fx = best_fy = 0
        for fy in range(0, fh - rh + 1, coarse):
            for fx in range(0, fw - rw + 1, coarse):
                patch = full.crop((fx+primary_co, fy+primary_ro,
                                   fx+primary_co+ps, fy+primary_ro+ps)).tobytes()
                diff = sum(abs(a-b) for a, b in zip(primary_bytes, patch))
                if diff < best_diff:
                    best_diff = diff
                    best_fx, best_fy = fx, fy
        candidates = [(best_diff, best_fx, best_fy)]

    best_total = float("inf")
    best_fx = best_fy = 0
    for _, cx, cy in sorted(candidates)[:20]:
        for fy in range(max(0, cy-coarse), min(fh-rh+1, cy+coarse+1)):
            for fx in range(max(0, cx-coarse), min(fw-rw+1, cx+coarse+1)):
                total = 0
                for ro, co, sample in probes:
                    patch = full.crop((fx+co, fy+ro,
                                       fx+co+ps, fy+ro+ps)).tobytes()
                    total += sum(abs(a-b) for a, b in zip(sample, patch))
                if total < best_total:
                    best_total = total
                    best_fx, best_fy = fx, fy

    lx = d["x"] + int(round(best_fx / scale))
    ly = d["y"] + int(round(best_fy / scale))
    lw = max(1, int(round(rw / scale)))
    lh = max(1, int(round(rh / scale)))
    return (lx, ly, lw, lh, best_total)

# ─────────────────────────────────────────────
#  Recorder
# ─────────────────────────────────────────────

class Recorder:
    def __init__(self, region, fps):
        self.region = region
        self.fps    = fps
        self.frames = []
        self._stop_event = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        self._thread.join()

    def _capture_loop(self):
        x, y, w, h = self.region
        monitor  = {"left": x, "top": y, "width": w, "height": h}
        interval = 1.0 / self.fps
        with mss.mss() as sct:
            while not self._stop_event.is_set():
                t0  = time.perf_counter()
                raw = sct.grab(monitor)
                img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
                self.frames.append(img)
                wait = interval - (time.perf_counter() - t0)
                if wait > 0:
                    deadline = time.perf_counter() + wait
                    while not self._stop_event.is_set():
                        remaining = deadline - time.perf_counter()
                        if remaining <= 0:
                            break
                        time.sleep(min(0.05, remaining))

    def save_gif(self, path, playback_fps=None):
        if not self.frames:
            return False
        pb_fps      = playback_fps or self.fps
        duration_ms = max(20, int(1000 / pb_fps))

        # Boost saturation before quantization so vivid colors survive
        # the 256-color palette reduction without going muddy.
        # Factor 1.3 is subtle but makes reds/greens/blues noticeably richer.
        from PIL import ImageEnhance
        def _enhance(frame):
            return ImageEnhance.Color(frame).enhance(1.3)

        def _quantize(frame, palette_img):
            return _enhance(frame).quantize(palette=palette_img, dither=0)

        n = len(self.frames)
        sample_indices = [int(i * n / min(n, 20)) for i in range(min(n, 20))]
        # Build palette from enhanced frames so it covers the boosted color space
        sample_frames  = [_enhance(self.frames[i]) for i in sample_indices]

        composite_w = sample_frames[0].width * len(sample_frames)
        composite   = Image.new("RGB", (composite_w, sample_frames[0].height))
        for i, f in enumerate(sample_frames):
            composite.paste(f, (i * f.width, 0))
        # LIBIMAGEQUANT gives best quality if available, otherwise MEDIANCUT
        try:
            palette_img = composite.quantize(
                colors=256, method=Image.Quantize.LIBIMAGEQUANT)
        except Exception:
            palette_img = composite.quantize(
                colors=256, method=Image.Quantize.MEDIANCUT)

        quantized = [_quantize(f, palette_img) for f in self.frames]
        quantized[0].save(
            path, save_all=True, append_images=quantized[1:],
            loop=0, duration=duration_ms, optimize=False,
        )
        return True

    def save_mp4(self, path, playback_fps=None):
        if not self.frames or not FFMPEG_BIN:
            return False
        pb_fps = max(1.0, playback_fps or self.fps)
        w, h   = self.frames[0].size
        w = w if w % 2 == 0 else w - 1
        h = h if h % 2 == 0 else h - 1
        cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo", "-vcodec", "rawvideo",
            "-pix_fmt", "rgb24", "-s", f"{w}x{h}",
            "-r", str(pb_fps), "-i", "pipe:0",
            "-vcodec", "libx264", "-pix_fmt", "yuv420p",
            "-preset", "fast", "-crf", "18",
            path,
        ]
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.PIPE,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE)
            for frame in self.frames:
                img = frame.resize((w, h)) if frame.size != (w, h) else frame
                proc.stdin.write(img.tobytes())
            proc.stdin.close()
            proc.wait()
            return proc.returncode == 0 and os.path.getsize(path) > 0
        except Exception as e:
            print(f"FFmpeg error: {e}")
            return False


# ─────────────────────────────────────────────
#  Menu bar app
# ─────────────────────────────────────────────

class ScreenRecorderApp(rumps.App):

    def __init__(self):
        super().__init__("●", quit_button=None)
        self.region      = None
        self.recorder    = None
        self.recording   = False
        self.fps_idx     = DEFAULT_FPS_IDX
        self.fmt         = "gif"
        self.output_dir    = os.path.expanduser("~/Desktop")
        self._start_time   = 0
        self._tick_timer   = None
        self._stopping     = False
        self._recent_paths = self._load_recent_paths()
        self._build_menu()
        self._start_hotkey()
        # Compile coord tap in background at launch if not cached
        if not COORD_TAP_BIN:
            threading.Thread(target=_compile_coord_tap_bg,
                             daemon=True).start()



    def _build_menu(self):
        # ── Output submenu
        output_menu = rumps.MenuItem("Output: Desktop")
        self._output_menu  = output_menu
        self._output_items = {}
        self._rebuild_output_menu()

        # ── Format submenu
        fmt_menu = rumps.MenuItem("Format: GIF")
        self._gif_item = rumps.MenuItem("GIF",         callback=self._set_fmt_gif)
        self._mp4_item = rumps.MenuItem("MP4 / H.264", callback=self._set_fmt_mp4)
        self._gif_item.state = 1
        fmt_menu.add(self._gif_item)
        fmt_menu.add(self._mp4_item)
        self._fmt_menu = fmt_menu

        # ── Speed submenu
        self._fps_items = []
        fps_label = FPS_STEPS[self.fps_idx][0]
        fps_menu = rumps.MenuItem(f"Speed: {fps_label}")
        for i, (label, _) in enumerate(FPS_STEPS):
            item = rumps.MenuItem(label, callback=self._set_fps)
            item._fps_idx = i
            item.state = 1 if i == self.fps_idx else 0
            fps_menu.add(item)
            self._fps_items.append(item)
        self._fps_menu = fps_menu

        # ── Region
        self._region_item = rumps.MenuItem(
            "Select Region…", callback=self._select_region)
        self._region_status = rumps.MenuItem("  No region selected")
        self._region_status.set_callback(None)

        # ── Record — hotkey shown natively via NSMenuItem
        self._record_item = rumps.MenuItem(
            "⏺  Record",
            callback=self._toggle_record)
        # Set native macOS shortcut display (⌥Space) on the NSMenuItem
        # This shows the key right-aligned in the system font, exactly like
        # built-in macOS menu items
        try:
            import AppKit
            ns_item = self._record_item._menuitem
            # ⌥ = option modifier, space = key equivalent
            ns_item.setKeyEquivalent_(" ")
            ns_item.setKeyEquivalentModifierMask_(
                AppKit.NSEventModifierFlagOption)
        except Exception:
            # Fallback: append hint manually if AppKit not available
            self._record_item.title = "⏺  Record"
        self._status_item = rumps.MenuItem("  Ready")
        self._status_item.set_callback(None)

        self.menu = [
            output_menu,
            fmt_menu,
            fps_menu,
            rumps.separator,
            self._region_item,
            self._region_status,
            rumps.separator,
            self._record_item,
            rumps.separator,
            rumps.MenuItem("Quit", callback=self._quit),
        ]

    # ── Summary / hotkey ─────────────────────

    def _start_hotkey(self):
        if not PYNPUT_OK:
            print("pynput not available — ⌥Space hotkey disabled")
            return

        self._alt_down = False

        def on_press(key):
            try:
                # macOS alt keys
                if key in (_kb.Key.alt, _kb.Key.alt_l, _kb.Key.alt_r):
                    self._alt_down = True
                    return
                # Space while alt is held
                if self._alt_down and key == _kb.Key.space:
                    print("⌥Space hotkey fired")
                    rumps.Timer(
                        lambda t: (self._toggle_record(None), t.stop()),
                        0.01
                    ).start()
                    return
                # Also handle char-based space (some pynput versions)
                if self._alt_down and hasattr(key, 'char') and key.char == ' ':
                    print("⌥Space hotkey fired (char)")
                    rumps.Timer(
                        lambda t: (self._toggle_record(None), t.stop()),
                        0.01
                    ).start()
            except Exception as e:
                print(f"hotkey on_press error: {e}")

        def on_release(key):
            try:
                if key in (_kb.Key.alt, _kb.Key.alt_l, _kb.Key.alt_r):
                    self._alt_down = False
            except Exception:
                pass

        self._hotkey_listener = _kb.Listener(
            on_press=on_press,
            on_release=on_release,
            daemon=True
        )
        self._hotkey_listener.start()
        print(f"⌥Space hotkey listener started: {self._hotkey_listener.running}")

    # ── Region ───────────────────────────────

    _SPINNER = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]

    def _select_region(self, _):
        if self.recording:
            return
        threading.Thread(target=self._do_select_region, daemon=True).start()

    def _do_select_region(self):
        """
        Region selection using a passive CGEvent coord tap + screencapture -i.
        The tap listens for mouseDown/mouseUp to get exact drag coordinates.
        screencapture -i provides the native crosshair UI.
        """
        tmp_region = "/tmp/_sr_region_main.png"
        if os.path.exists(tmp_region):
            os.remove(tmp_region)

        self._region_status.title = "  Select area on screen…"

        # Spin the menu bar icon while we wait for coord tap + screencapture to launch
        launch_spinner_stop = threading.Event()
        def _launch_spin():
            i = 0
            while not launch_spinner_stop.is_set():
                self.title = self._SPINNER[i % len(self._SPINNER)]
                i += 1
                time.sleep(0.08)
            self.title = "●"
        launch_spin_thread = threading.Thread(target=_launch_spin, daemon=True)
        launch_spin_thread.start()

        _coord_tap_ready.wait(timeout=15)
        tap_proc = None

        if COORD_TAP_BIN and os.path.isfile(COORD_TAP_BIN):
            tap_proc = subprocess.Popen(
                [COORD_TAP_BIN],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            # Wait for tap to signal it's registered before launching screencapture
            # This ensures the first drag is always captured
            if tap_proc:
                import select as _select
                ready = False
                deadline = time.time() + 3.0
                while time.time() < deadline:
                    r, _, _ = _select.select([tap_proc.stderr], [], [], 0.05)
                    if r:
                        line = tap_proc.stderr.readline().decode(errors="replace")
                        if "ready" in line:
                            ready = True
                            break
                        elif "no_accessibility" in line:
                            break
                if not ready:
                    print("Coord tap: ready signal not received, proceeding anyway")

        # Stop launch spinner — screencapture is now showing its crosshair UI
        launch_spinner_stop.set()
        launch_spin_thread.join(timeout=1)

        sc_proc = subprocess.Popen(
            ["screencapture", "-i", "-x", tmp_region],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        sc_proc.wait()

        tap_coords = None
        if tap_proc:
            import signal as _signal
            # Send SIGINT for clean flush, wait briefly, then force kill
            try:
                tap_proc.send_signal(_signal.SIGINT)
            except Exception:
                pass
            try:
                stdout, stderr = tap_proc.communicate(timeout=4)
            except subprocess.TimeoutExpired:
                tap_proc.kill()
                stdout, stderr = tap_proc.communicate()
            except Exception as e:
                print(f"Coord tap communicate error: {e}")
                stdout, stderr = b"", b""
                try: tap_proc.kill()
                except Exception: pass

            err = stderr.decode(errors="replace")
            out = stdout.decode(errors="replace").strip()
            print(f"Coord tap stdout: {repr(out)}")
            print(f"Coord tap stderr: {repr(err)}")
            if "no_accessibility" in err:
                print("Coord tap: needs Accessibility — System Settings → Privacy → Accessibility")
            elif out:
                lines = [l for l in out.split("\n") if l.strip()]
                for line in reversed(lines):
                    try:
                        parts = [int(v) for v in line.strip().split(",")]
                        if len(parts) == 4 and parts[2] > 5 and parts[3] > 5:
                            tap_coords = tuple(parts)
                            print(f"Coord tap result: {tap_coords}")
                            break
                    except ValueError:
                        continue
            else:
                print("Coord tap: no output — drag may have been missed")

        if sc_proc.returncode != 0 or not os.path.exists(tmp_region):
            self._region_item.title = "Select Region…"
            self._region_status.title = "  No region selected"
            return

        if tap_coords:
            x, y, w, h = tap_coords
            self.region = (x, y, w, h)
            self._region_item.title = "Select Region…"
            self._region_status.title = f"  {w}×{h} at ({x}, {y})"
            try: os.remove(tmp_region)
            except Exception: pass
            return

        # Coord tap ran but produced no coordinates
        # (drag too fast, ignore window too wide, or tap timing issue)
        try: os.remove(tmp_region)
        except Exception: pass
        self._region_item.title = "Select Region…"
        self._region_status.title = "  No region selected (try again)"
        print("Coord tap: no coords captured — try selecting again")

    # ── FPS ──────────────────────────────────

    def _set_fps(self, sender):
        for item in self._fps_items:
            item.state = 0
        sender.state = 1
        self.fps_idx = sender._fps_idx
        self._fps_menu.title = f"Speed: {FPS_STEPS[self.fps_idx][0]}"

    def _current_fps(self):
        return FPS_STEPS[self.fps_idx][1]

    # ── Format ───────────────────────────────

    def _set_fmt_gif(self, _):
        self.fmt = "gif"
        self._gif_item.state = 1
        self._mp4_item.state = 0
        self._fmt_menu.title = "Format: GIF"

    def _set_fmt_mp4(self, _):
        if not FFMPEG_BIN:
            _alert("FFmpeg not found.\nInstall with: brew install ffmpeg")
            return
        self.fmt = "mp4"
        self._mp4_item.state = 1
        self._gif_item.state = 0
        self._fmt_menu.title = "Format: MP4 / H.264"

    # ── Output folder ─────────────────────────

    _PRESET_DIRS = [
        ("Desktop",   "~/Desktop"),
        ("Pictures",  "~/Pictures"),
        ("Movies",    "~/Movies"),
        ("Downloads", "~/Downloads"),
    ]
    _RECENT_FILE = os.path.expanduser("~/.screen_recorder_recents")
    _MAX_RECENT  = 3

    def _load_recent_paths(self):
        try:
            if os.path.exists(self._RECENT_FILE):
                with open(self._RECENT_FILE) as f:
                    paths = [l.strip() for l in f.readlines() if l.strip()]
                return [p for p in paths if os.path.isdir(p)]
        except Exception:
            pass
        return []

    def _save_recent_paths(self):
        try:
            with open(self._RECENT_FILE, "w") as f:
                f.write("\n".join(self._recent_paths))
        except Exception:
            pass

    def _set_output_dir(self, path):
        """Set output dir, update recents, rebuild menu."""
        path = path.rstrip("/")
        self.output_dir = path
        # Add to recents if not a preset
        preset_paths = {os.path.expanduser(p) for _, p in self._PRESET_DIRS}
        if path not in preset_paths:
            if path in self._recent_paths:
                self._recent_paths.remove(path)
            self._recent_paths.insert(0, path)
            self._recent_paths = self._recent_paths[:self._MAX_RECENT]
            self._save_recent_paths()
        self._rebuild_output_menu()

    def _rebuild_output_menu(self):
        """Rebuild the output submenu with presets + recents + Choose…"""
        menu = self._output_menu
        # Clear existing items
        for key in list(menu.keys()):
            del menu[key]

        current = self.output_dir.rstrip("/")

        # Preset locations
        for name, rel in self._PRESET_DIRS:
            path = os.path.expanduser(rel)
            item = rumps.MenuItem(name, callback=self._make_output_cb(path))
            item.state = 1 if path == current else 0
            menu.add(item)

        # Recent custom paths
        preset_paths = {os.path.expanduser(p) for _, p in self._PRESET_DIRS}
        recents = [p for p in self._recent_paths if p not in preset_paths]
        if recents:
            menu.add(rumps.separator)
            for path in recents:
                name = os.path.basename(path) or path
                item = rumps.MenuItem(name, callback=self._make_output_cb(path))
                item.state = 1 if path == current else 0
                menu.add(item)

        menu.add(rumps.separator)
        menu.add(rumps.MenuItem("Choose…", callback=self._choose_custom_dir))

        # Update submenu title
        name = os.path.basename(current) or current
        menu.title = f"Output: {name}"

    def _make_output_cb(self, path):
        def cb(_):
            self._set_output_dir(path)
        return cb

    def _choose_custom_dir(self, _):
        threading.Thread(target=self._do_choose_custom_dir, daemon=True).start()

    def _do_choose_custom_dir(self):
        script = 'set f to choose folder with prompt "Choose output folder:"\nreturn POSIX path of f'
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120
        )
        if r.returncode != 0:
            return  # user cancelled
        path = r.stdout.strip().rstrip("/")
        if path and os.path.isdir(path):
            self._set_output_dir(path)

    def _choose_output_dir(self, _):
        # Legacy — kept for compatibility, calls custom chooser
        self._choose_custom_dir(None)

    # ── Record ───────────────────────────────

    def _toggle_record(self, _):
        if not self.recording:
            self._start_recording()
        else:
            if hasattr(self, '_stopping') and self._stopping:
                return
            self._stopping = True
            self._record_item.title = "  Stopping…"
            self.title = "●"
            # Use a one-shot rumps timer to safely leave the menu callback
            # before doing any blocking work — avoids trace trap
            rumps.Timer(self._stop_recording_safe, 0.05).start()

    def _start_recording(self):
        print(f"_start_recording: self.region = {self.region}")
        if not self.region:
            threading.Thread(
                target=lambda: _alert("Please select a region first."),
                daemon=True).start()
            return
        self.recording   = True
        self.recorder    = Recorder(self.region, self._current_fps())
        self._start_time = time.time()
        self.recorder.start()
        self.title = "■"
        self._record_item.title = "■  Stop"
        self._tick()

    def _tick(self):
        if not self.recording:
            return
        elapsed = int(time.time() - self._start_time)
        m, s = divmod(elapsed, 60)
        n = len(self.recorder.frames) if self.recorder else 0
        self._status_item.title = f"  Recording {m:02d}:{s:02d}  ({n} frames)"
        self._tick_timer = threading.Timer(0.5, self._tick)
        self._tick_timer.daemon = True
        self._tick_timer.start()

    def _stop_recording_safe(self, timer=None):
        # Called via rumps.Timer — safely off the menu callback stack
        if timer:
            timer.stop()
        if self._tick_timer:
            self._tick_timer.cancel()
        self.recording = False
        self._record_item.title = "⏺  Record"
        self._status_item.title = "  Stopping…"
        # Do blocking work in a thread now that we're off the callback
        threading.Thread(target=self._finish_stop, daemon=True).start()

    def _finish_stop(self):
        self.recorder.stop()
        self._stopping = False

        n = len(self.recorder.frames)
        self._status_item.title = f"  Captured {n} frames — encoding…"

        if n == 0:
            _alert("No frames captured.")
            self.recorder = None
            return

        capture_fps  = self._current_fps()
        playback_fps = 10.0 if capture_fps < 1 else capture_fps

        filename = f"recording_{datetime.now().strftime('%Y%m%d_%H%M%S')}.{self.fmt}"
        path = os.path.join(self.output_dir, filename)

        self._encode(path, playback_fps, n)

    def _encode(self, path, playback_fps, n):
        ok = (self.recorder.save_gif(path, playback_fps=playback_fps)
              if self.fmt == "gif" else
              self.recorder.save_mp4(path, playback_fps=playback_fps))
        self.recorder = None
        if ok:
            size_kb = os.path.getsize(path) // 1024
            self._status_item.title = f"  Saved — {size_kb} KB"
            subprocess.Popen(["open", "-R", path])
        else:
            self._status_item.title = "  Error saving file"
            _alert("Could not save file.\nFor MP4, check ffmpeg is installed.")

    def _quit(self, _):
        if self.recording:
            self._stop_recording_safe()
        rumps.quit_application()


# ─────────────────────────────────────────────
#  Entry point
# ─────────────────────────────────────────────

if __name__ == "__main__":
    ScreenRecorderApp().run()
