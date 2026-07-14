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
        if title == "NihongoViewer":
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


def to_data_url(img: Image.Image, max_width: int = 640) -> str:
    """Encode a PIL image to a base64 PNG data URL, downscaled for preview."""
    if img.width > max_width:
        ratio = max_width / img.width
        img = img.resize((max_width, max(1, int(img.height * ratio))))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
