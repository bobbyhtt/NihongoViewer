"""Window enumeration and screen capture for NihongoViewer (Windows).

Window listing/geometry use the Win32 API (pywin32). Frame capture uses
**Windows Graphics Capture (WGC)** via the `windows-capture` package.

Why WGC (and not PrintWindow or mss):
  - `PrintWindow` forces the target window to redraw, which on GPU-rendered
    (DirectX) games occasionally makes the game present a black frame to the
    real screen (a visible flash).
  - `mss` reads the composited desktop *region*, so it captures whatever is on
    screen there — overlapping windows, the whole screen for a maximized game —
    not the target window's own pixels.
  - WGC captures the target window's own composited surface through the DWM: it
    never disturbs the game (no flash) AND it ignores anything overlapping it
    (including our own overlay), so we always get just that window's content.

WGC is a *streaming* API — a background session delivers frames as the window
updates. We keep one session for the selected window and hand out the latest
frame on demand. WGC captures the window's CLIENT area, so map OCR boxes to the
screen via `client_rect` (ClientToScreen), not the full window rect.
"""

import base64
import ctypes
from ctypes import wintypes
import io
import threading
import time

import win32con
import win32gui
from PIL import Image

# Make sure GetWindowRect / capture sizes are in real pixels on high-DPI screens.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_AWARE
except Exception:
    ctypes.windll.user32.SetProcessDPIAware()

_dwmapi = ctypes.windll.dwmapi

DWMWA_CLOAKED = 14


def _is_cloaked(hwnd: int) -> bool:
    """True for hidden UWP 'ghost' windows that shouldn't be listed."""
    value = ctypes.c_int(0)
    _dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd),
        DWMWA_CLOAKED,
        ctypes.byref(value),
        ctypes.sizeof(value),
    )
    return value.value != 0


def list_windows() -> list[dict]:
    """Return visible top-level windows as [{'id': hwnd, 'title': str}, ...]."""
    windows: list[dict] = []

    def _enum(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title.strip():
            return
        if _is_cloaked(hwnd):
            return
        # Skip our own launcher window.
        if title == "Yakutori":
            return
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        if right - left <= 0 or bottom - top <= 0:
            return
        windows.append({"id": hwnd, "title": title})

    win32gui.EnumWindows(_enum, None)
    return windows


def show_window(hwnd: int) -> None:
    """Un-minimize the target window so it renders, without stealing focus.

    A minimized window has no surface for WGC to capture — restore it.
    SW_SHOWNOACTIVATE keeps it from jumping to the foreground over us.
    """
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
    except Exception:
        pass


def window_exists(hwnd: int) -> bool:
    """True while the window still exists (False once it has been closed)."""
    return bool(win32gui.IsWindow(hwnd))


def client_rect(hwnd: int) -> tuple[int, int, int, int] | None:
    """Screen rect of the window's CLIENT area as (left, top, width, height).

    WGC captures the client area, so this is the origin the captured frame is
    measured from: a captured-image pixel (x, y) maps to screen (left + x,
    top + y). Per-monitor DPI aware (see top of module), so both are physical px.
    """
    if not win32gui.IsWindow(hwnd):
        return None
    _, _, right, bottom = win32gui.GetClientRect(hwnd)  # (0, 0, w, h)
    left, top = win32gui.ClientToScreen(hwnd, (0, 0))
    return (left, top, right, bottom)


class _WgcSession:
    """A running WGC capture for one window; stores the most recent frame."""

    def __init__(self, hwnd: int) -> None:
        from windows_capture import WindowsCapture  # lazy (Windows-only, native)

        self.hwnd = hwnd
        self._latest = None  # (numpy BGRA frame, width, height)
        self._lock = threading.Lock()

        cap = WindowsCapture(
            cursor_capture=False,
            draw_border=False,
            window_hwnd=hwnd,
            minimum_update_interval=100,  # cap delivery ~10 fps; we sample ~1 fps
        )

        @cap.event
        def on_frame_arrived(frame, capture_control):
            with self._lock:
                self._latest = (frame.frame_buffer.copy(), frame.width, frame.height)

        @cap.event
        def on_closed():
            pass

        self._control = cap.start_free_threaded()

    def latest_image(self) -> Image.Image | None:
        with self._lock:
            data = self._latest
        if data is None:
            return None
        buf, w, h = data
        # buf is (H, W, 4) BGRA; take B,G,R -> R,G,B and drop alpha.
        import numpy as np

        rgb = np.ascontiguousarray(buf[:h, :w, 2::-1])
        return Image.fromarray(rgb, "RGB")

    def stop(self) -> None:
        try:
            self._control.stop()
        except Exception:
            pass


_session: _WgcSession | None = None
_session_lock = threading.Lock()


def capture_window_image(hwnd: int) -> Image.Image | None:
    """Capture the selected window's own content as a full-resolution RGB image.

    Starts/keeps a WGC session for `hwnd` (switching windows restarts it) and
    returns the latest delivered frame. Returns None if the window is gone or no
    frame has arrived yet. Shared by the preview and the OCR stage.
    """
    if not win32gui.IsWindow(hwnd):
        return None

    global _session
    with _session_lock:
        if _session is None or _session.hwnd != hwnd:
            if _session is not None:
                _session.stop()
            _session = _WgcSession(hwnd)
        session = _session

    img = session.latest_image()
    if img is None:
        # Just started — wait briefly for the first frame to arrive.
        for _ in range(25):
            time.sleep(0.02)
            img = session.latest_image()
            if img is not None:
                break
    return img


def current_frame_image() -> Image.Image | None:
    """Return the running session's latest frame at full resolution, or None.

    Unlike `capture_window_image`, this never starts a session — it only reads
    the most recent frame the active WGC session has already delivered. Used by
    the Create-card "Capture" button to grab a higher-res still than the ~640px
    preview, without disturbing the game (WGC just hands back its latest buffer).
    """
    with _session_lock:
        session = _session
    if session is None:
        return None
    return session.latest_image()


def stop_capture_session() -> None:
    """Stop the WGC session (called when capture stops)."""
    global _session
    with _session_lock:
        if _session is not None:
            _session.stop()
            _session = None


def grab_region(x: int, y: int, w: int, h: int) -> Image.Image | None:
    """Grab a screen-absolute rectangle as an RGB image (None if empty/failed).

    Area mode reads raw screen pixels — whatever is visible inside the box —
    rather than a specific window's surface, so no window has to be selected. Uses
    GDI screen capture (`ImageGrab`); coordinates are virtual-desktop pixels (the
    same space the area editor uses), so multi-monitor / negative origins work.
    Note: this captures whatever is on top there, so the translate overlay must not
    sit inside the detect box, and true exclusive-fullscreen apps may not be caught.
    """
    if w <= 0 or h <= 0:
        return None
    from PIL import ImageGrab

    try:
        img = ImageGrab.grab(bbox=(int(x), int(y), int(x) + int(w), int(y) + int(h)),
                             all_screens=True)
    except Exception:
        return None
    return img.convert("RGB") if img is not None else None


def grab_screen(around: tuple | None = None) -> Image.Image | None:
    """Grab a whole monitor as an RGB image.

    The monitor is the one containing `around=(x, y)` (e.g. the detect box's
    centre) so the shot matches where the text is; falls back to the primary
    monitor. Used for the Create-card image in area mode — the card shows the full
    screen for context while its text comes from just the detect box.
    """
    import win32api

    try:
        flag = (win32con.MONITOR_DEFAULTTONEAREST if around is not None
                else win32con.MONITOR_DEFAULTTOPRIMARY)
        hmon = win32api.MonitorFromPoint(tuple(around) if around else (0, 0), flag)
        left, top, right, bottom = win32api.GetMonitorInfo(hmon)["Monitor"]
        return grab_region(left, top, right - left, bottom - top)
    except Exception:
        w = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
        h = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
        return grab_region(0, 0, w, h)


def to_data_url(img: Image.Image, max_width: int = 640, quality: int = 82) -> str:
    """Encode a PIL image to a base64 **JPEG** data URL, downscaled for preview.

    Game frames are photographic (character art, gradients), which PNG stores
    poorly — a 960px card frame is ~0.4-1 MB as PNG but ~0.1-0.2 MB as JPEG-82,
    with no meaningful quality loss for a preview/flashcard. That matters because
    these frames are held in memory, marshaled over the UI bridge, and (for the
    capture stack + saved cards) written to disk. JPEG can't hold an alpha channel,
    so non-RGB inputs are flattened to RGB first (screen grabs have no useful
    alpha anyway).
    """
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))))
    if img.mode != "RGB":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


# Frame-change detection (see main.Api.process_frame's tiered skipping). A small
# grayscale "signature" of a frame; its only job is to catch a screen where
# *nothing* moved (paused game, still dialogue) so the pipeline can skip
# OCR/translation, while still noticing a NEW dialogue line.
#
# We compare by counting cells that changed *significantly*, NOT by averaging.
# A dialogue line is a small, localized part of the whole window: on a mostly
# uniform background (a pale/white VN scene) a full line change moves the mean of
# a downscaled frame by only ~0.3/255 — far under any averaging threshold — so
# averaging silently skipped every new line until the user hit Stop/Start (the
# text just "wouldn't update"). A busy/animated background happened to clear the
# average threshold, which is why some games worked and white ones didn't.
# Counting cells whose grayscale moved by >_CELL_DELTA instead: a new line lights
# up ~200 cells at 128x128, while sensor jitter and a blinking cursor stay in the
# single digits — a clean, background-independent split.
_SIG_SIZE = 128              # 128x128 grayscale — fine enough to resolve a text line
_CELL_DELTA = 16             # per-cell grayscale move (0-255) that counts as "changed"
_MIN_CHANGED_CELLS = 40      # fewer changed cells than this => screen is unchanged


def frame_signature(img: Image.Image):
    """A small grayscale array summarizing `img`, for cheap frame-change tests."""
    import numpy as np

    small = img.convert("L").resize((_SIG_SIZE, _SIG_SIZE))
    return np.asarray(small, dtype=np.int16)


def signatures_match(a, b) -> bool:
    """True if two `frame_signature` arrays are near-identical (screen unchanged).

    "Near-identical" = fewer than `_MIN_CHANGED_CELLS` cells moved by more than
    `_CELL_DELTA`. Localized changes (a new dialogue line) survive this where an
    average would wash them out; see the constants above.
    """
    if a is None or b is None or a.shape != b.shape:
        return False
    import numpy as np

    changed = int((np.abs(a - b) > _CELL_DELTA).sum())
    return changed < _MIN_CHANGED_CELLS
