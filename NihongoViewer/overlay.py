"""In-place translation overlay (pipeline stage 4), Windows-only.

A per-pixel-transparent, click-through, always-on-top window that draws the
translated text over the source window at the detected text's location.

Why native Win32 instead of a second pywebview window: WebView2 transparency is
unreliable (this is exactly the limitation CLAUDE.md calls out). A layered window
updated via `UpdateLayeredWindow` with a premultiplied-alpha bitmap gives true
per-pixel alpha and anti-aliased text, and `WS_EX_TRANSPARENT` makes it
click-through so it never steals input from the game underneath. The text is
rendered with PIL (RGBA) and blitted straight to the layered window.

The window lives on its own thread that owns the HWND and runs a light message
pump; other threads talk to it through thread-safe `update()` / `hide()` calls.
"""

import ctypes
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

# Fonts bundled with the app (so they work without a system install).
_FONTS_DIR = Path(__file__).parent / "fonts"


def _debug(msg: str) -> None:
    """Best-effort one-line log to a temp file — only the overlay FAILURE paths
    write here, so if the overlay ever misbehaves again this file says exactly
    what failed (CreateWindowEx / UpdateLayeredWindow) instead of us guessing.
    Silent and non-fatal: logging must never take down the overlay.
    """
    try:
        import os
        import tempfile

        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} overlay: {msg}\n"
        with open(os.path.join(tempfile.gettempdir(), "nihongoviewer_overlay.log"),
                  "a", encoding="utf-8") as fh:
            fh.write(line)
    except Exception:
        pass

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

# --- Win32 constants ---------------------------------------------------------
WS_EX_LAYERED = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_POPUP = 0x80000000

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4

# We handle WM_CLOSE ourselves (swallow it) so DefWindowProc can't turn a stray
# close message into a DestroyWindow that would kill the overlay for good.
WM_CLOSE = 0x0010

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01

# Excludes the window from screen capture (mss/WGC) while keeping it visible on
# screen — so our own capture doesn't read the overlay covering the game text.
WDA_EXCLUDEFROMCAPTURE = 0x00000011

HWND_TOPMOST = -1
SWP_NOSIZE = 0x0001
SWP_NOMOVE = 0x0002
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

BI_RGB = 0
DIB_RGB_COLORS = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


# Handles are pointer-sized: declare arg/return types so ctypes doesn't truncate.
user32.GetDC.restype = wintypes.HDC
user32.GetDC.argtypes = [wintypes.HWND]
user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), wintypes.HDC, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.SelectObject.restype = wintypes.HGDIOBJ
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
gdi32.DeleteDC.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.restype = wintypes.BOOL
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindowVisible.restype = wintypes.BOOL
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.SetWindowPos.restype = wintypes.BOOL
user32.SetWindowPos.argtypes = [
    wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wintypes.UINT,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.restype = wintypes.BOOL
user32.SetWindowDisplayAffinity.argtypes = [wintypes.HWND, wintypes.DWORD]


#: Overlay font families offered in the UI — all bundled, OFL, and JP+Latin.
_BUNDLED = {
    "Noto Sans JP": str(_FONTS_DIR / "NotoSansJP.ttf"),          # variable -> Regular
    "M PLUS Rounded 1c": str(_FONTS_DIR / "MPLUSRounded1c-Regular.ttf"),
    "Shippori Mincho": str(_FONTS_DIR / "ShipporiMincho-Regular.ttf"),
}


def _resolve_font(family: str, size: int) -> ImageFont.FreeTypeFont:
    """Map a family name to a font file, with Japanese-capable fallbacks."""
    candidates = {
        **_BUNDLED,
        # System JP fonts kept only as fallbacks (not offered in the UI).
        "Meiryo": "meiryo.ttc",
        "Yu Gothic": "YuGothM.ttc",
        "MS Gothic": "msgothic.ttc",
    }
    # Always fall back to a bundled JP font so a bad/removed choice still shows
    # Japanese (never an unbundled Latin-only face).
    tries = [candidates.get(family, family), _BUNDLED["Noto Sans JP"], "meiryo.ttc"]
    for name in tries:
        try:
            font = ImageFont.truetype(name, size)
        except OSError:
            continue
        try:
            font.set_variation_by_name("Regular")  # variable fonts -> Regular weight
        except (OSError, AttributeError, ValueError):
            pass  # a static font has no named instances — that's fine
        return font
    return ImageFont.load_default()


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = (value or "#000000").lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _wrap_to_width(text: str, font, max_width: int, probe: ImageDraw.ImageDraw) -> str:
    """Wrap `text` so no line exceeds `max_width` pixels in `font`.

    Breaks at spaces when it can (English), and character-by-character when it
    can't (Japanese has no spaces) — so both scripts wrap inside a block. Blank
    lines and explicit newlines in the source are preserved.
    """
    out: list[str] = []
    for para in text.split("\n"):
        cur = ""
        last_space = -1  # index in `cur` of the last space we could break at
        for ch in para:
            if cur == "" or probe.textlength(cur + ch, font=font) <= max_width:
                cur += ch
                if ch == " ":
                    last_space = len(cur) - 1
            elif ch == " ":
                out.append(cur)      # break at this space (drop it)
                cur, last_space = "", -1
            elif last_space >= 0:
                out.append(cur[:last_space])  # break at the last space
                cur, last_space = cur[last_space + 1:] + ch, -1
            else:
                out.append(cur)      # nowhere to break (CJK) -> hard break
                cur = ch
        out.append(cur)
    return "\n".join(out)


# Scripts that may break between any two characters (no spaces): kana, kanji,
# CJK punctuation, halfwidth kana. Latin words must stay whole instead.
_CJK = re.compile("[　-〿぀-ヿㇰ-ㇿ㐀-䶿一-鿿ｦ-ﾝ]")


def _min_content_width(text: str, font, probe: ImageDraw.ImageDraw) -> int:
    """Width of the widest unit that must not be split.

    A whole word for space-delimited text (so English words never break
    mid-word); a single character for CJK tokens (which may break anywhere, so
    they never force the block wider).
    """
    widest = 0.0
    for token in text.split():
        if _CJK.search(token):
            for ch in token:
                widest = max(widest, probe.textlength(ch, font=font))
        else:
            widest = max(widest, probe.textlength(token, font=font))
    return int(widest + 0.999)


def render_text_image(text: str, style: dict, min_size: tuple[int, int] | None = None,
                      fit_box: tuple[int, int] | None = None,
                      max_w: int | None = None) -> Image.Image:
    """Render translated text + background box to an RGBA image (per-pixel alpha).

    Text uses the chosen font at its normal weight, over a **slim** background
    box (small vertical padding).

    With `fit_box=(w, h)`, the text is wrapped and the background box is grown to
    fit it at the user's chosen size: the box uses `fit_box` as its minimum (so it
    still covers the original Japanese) and stretches **right** — up to `max_w`,
    the space to the window's right edge — and **down** as the wrapped text needs.
    The font is **not** shrunk to cram inside the region (that made the size
    setting useless for small regions); the frame grows instead.
    """
    size = int(style.get("size", 18))
    text_rgb = _hex_to_rgb(style.get("text_color", "#ffffff"))
    bg_rgb = _hex_to_rgb(style.get("bg_color", "#111111"))
    bg_alpha = round(float(style.get("opacity", 80)) / 100 * 255)
    pad_x, pad_y = 8, 4       # equal padding all round -> balanced box
    spacing = 4               # gap between lines in Duo mode
    text = text or " "

    if fit_box is not None:
        return _render_fitted(text, style.get("font", "Meiryo"), size, fit_box, max_w,
                              text_rgb, bg_rgb, bg_alpha, pad_x, pad_y, spacing)

    font = _resolve_font(style.get("font", "Meiryo"), size)
    # Measure the TEXT'S ACTUAL bounding box (this accounts for the font's top/
    # bottom bearing), so we can pad and center it symmetrically instead of
    # guessing a line height — which is what left the top/bottom spacing uneven.
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    tx0, ty0, tx1, ty1 = probe.multiline_textbbox((0, 0), text, font=font, spacing=spacing)
    text_w, text_h = int(tx1 - tx0), int(ty1 - ty0)
    w = max(text_w + pad_x * 2, 1)
    h = max(text_h + pad_y * 2, 1)
    if min_size:  # cover at least the original JA text region
        w = max(w, min_size[0])
        h = max(h, min_size[1])

    img = Image.new("RGBA", (max(w, 1), max(h, 1)), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w - 1, h - 1], fill=(*bg_rgb, bg_alpha))
    # Place the measured text box so its ink is centered vertically (equal top &
    # bottom space) and left-aligned with pad_x (subtract the bbox origin so the
    # font's own bearing doesn't skew it).
    ox = pad_x - tx0
    oy = (h - text_h) // 2 - ty0
    draw.multiline_text((ox, oy), text, font=font, fill=(*text_rgb, 255), spacing=spacing)
    return img


def _render_fitted(text, family, size, fit_box, max_w, text_rgb, bg_rgb, bg_alpha,
                   pad_x, pad_y, spacing) -> Image.Image:
    """Render `text` at the user's size, wrapped to the region width, growing DOWN.

    `fit_box` (the detected region) is the box's minimum and anchor. The text is
    drawn at the user's `size` and wrapped to the region's own **width** — so it
    stays inside the frame horizontally and, when the translation is longer than
    the region, the box grows **downward** (more lines) rather than stretching to
    the right. The box only ever widens if a single word is too long to fit the
    region's width (it can't be broken); `max_w` caps that so it never runs past
    the window edge. The font is never shrunk — the frame grows to keep the size.
    """
    block_w = max(int(fit_box[0]), pad_x * 2 + 8)
    block_h = max(int(fit_box[1]), pad_y * 2 + 8)

    font = _resolve_font(family, int(size))
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    # Wrap within the region's own width — never narrower than the longest word (so
    # an English word never breaks mid-word). We deliberately do NOT widen the wrap
    # to pack fewer lines: extra length flows downward, not rightward.
    avail_w = max(block_w - pad_x * 2, _min_content_width(text, font, probe))
    wrapped = _wrap_to_width(text, font, avail_w, probe)
    tx0, ty0, tx1, ty1 = probe.multiline_textbbox((0, 0), wrapped, font=font, spacing=spacing)
    text_w, text_h = tx1 - tx0, ty1 - ty0

    # Grow DOWN to fit every line; grow right only if a long word forced the wrap
    # past the region width (capped at the window edge so it can't run off-screen).
    w = max(block_w, int(text_w) + pad_x * 2)
    if max_w:
        w = min(w, max(int(max_w), block_w))
    h = max(block_h, int(text_h) + pad_y * 2)

    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, w - 1, h - 1], fill=(*bg_rgb, bg_alpha))
    # Top-align the text (left-aligned, anchored to the top with pad_y). When the
    # box grows downward past the region, the text stays at the top — over the
    # original Japanese — instead of drifting to the middle of the taller box.
    ox = pad_x - tx0
    oy = pad_y - ty0
    draw.multiline_text((ox, oy), wrapped, font=font, fill=(*text_rgb, 255),
                        spacing=spacing, align="left")
    return img


def compose_canvas(items: list[dict], style: dict):
    """Render each item to its own tile and composite onto one transparent canvas.

    Returns (canvas_rgba, origin_x, origin_y) in screen pixels, or (None, 0, 0)
    when there is nothing to draw. The canvas spans only the union of the tiles,
    so the space between separate text boxes stays fully transparent.
    """
    tiles = []  # (tile_image, screen_x, screen_y)
    for it in items:
        text = (it.get("text") or "").strip()
        if not text:
            continue
        tile = render_text_image(text, style, min_size=it.get("cover"),
                                 fit_box=it.get("box"), max_w=it.get("max_w"))
        tiles.append((tile, int(it["x"]), int(it["y"])))
    if not tiles:
        return None, 0, 0

    origin_x = min(x for _, x, _ in tiles)
    origin_y = min(y for _, _, y in tiles)
    width = max(x + t.width for t, x, _ in tiles) - origin_x
    height = max(y + t.height for t, _, y in tiles) - origin_y
    canvas = Image.new("RGBA", (max(width, 1), max(height, 1)), (0, 0, 0, 0))
    for tile, x, y in tiles:
        canvas.alpha_composite(tile, (x - origin_x, y - origin_y))
    return canvas, origin_x, origin_y


class Overlay:
    """Thread-owned layered overlay window. Call update()/hide() from any thread."""

    _CLASS_NAME = "NihongoViewerOverlay"

    def __init__(self) -> None:
        self._hwnd: int | None = None
        self._visible = False
        self._lock = threading.Lock()
        self._pending: tuple | None = None  # ("show", image, x, y) | ("hide",)
        self._running = False
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="overlay", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=5)

    # -- public API (thread-safe) --------------------------------------------
    def update(self, items: list[dict], style: dict) -> None:
        """Draw one box per detected region.

        `items` is a list of {"text", "x", "y", "cover"} in SCREEN pixels — one
        entry per OCR region. Each is rendered to its own tile and composited
        onto a single transparent canvas spanning their union, so every box sits
        over its own text instead of one big box covering them all.
        """
        canvas, origin_x, origin_y = compose_canvas(items, style)
        if canvas is None:
            self.hide()
            return
        with self._lock:
            self._pending = ("show", canvas, origin_x, origin_y)

    def hide(self) -> None:
        with self._lock:
            self._pending = ("hide",)

    def close(self) -> None:
        self._running = False

    # -- overlay thread -------------------------------------------------------
    def _ensure_hwnd(self) -> bool:
        """Guarantee a live overlay HWND, recreating it if it has gone away.

        The window is created once at thread start, but it can later become
        invalid (a stray WM_CLOSE that slipped through, a DWM/explorer restart,
        etc.). Nothing else recreates it, so without this check a dead HWND would
        leave the overlay gone for the whole session — draws would queue and
        no-op, and even the hide/show hotkey couldn't bring it back (only an app
        restart could). Recreating on demand makes the overlay self-heal.

        Note: `IsWindow` catches a *destroyed* window, but a layered window can
        also stop accepting `UpdateLayeredWindow` while its HWND still reports
        valid (see `_draw`). That case is handled there by `_destroy_window`,
        which nulls `_hwnd` so this method rebuilds it on the next draw.
        """
        if self._hwnd and user32.IsWindow(self._hwnd):
            return True
        self._hwnd = None
        self._visible = False  # a fresh window starts hidden
        try:
            self._create_window()
        except Exception:
            self._hwnd = None
        return bool(self._hwnd)

    def _destroy_window(self) -> None:
        """Destroy the overlay window and forget it (call on the overlay thread).

        Used when a draw fails on a still-`IsWindow`-valid HWND: nulling `_hwnd`
        makes `_ensure_hwnd` build a fresh window on the next draw instead of
        forever retrying a dead one — the fix for "overlay gone until restart".
        """
        hwnd, self._hwnd, self._visible = self._hwnd, None, False
        if hwnd:
            try:
                import win32gui

                win32gui.DestroyWindow(hwnd)
            except Exception:
                pass

    def _run(self) -> None:
        try:
            self._create_window()
        except Exception:
            self._hwnd = None  # _ensure_hwnd will retry on the first draw
        self._running = True
        self._ready.set()
        msg = wintypes.MSG()
        while self._running:
            # The ENTIRE loop body is guarded: if any step raised — a draw/GDI
            # failure, a message-pump call, anything — and the exception escaped,
            # the thread would die and the overlay would be gone for the rest of
            # the session with no way back but an app restart. Swallow everything
            # and keep looping (a brief sleep avoids a busy-spin if it's persistent).
            try:
                # Drain the Win32 message queue (the window takes no input, but a
                # topmost window should still pump so DWM keeps it composited).
                while user32.PeekMessageW(ctypes.byref(msg), 0, 0, 0, 1):
                    user32.TranslateMessage(ctypes.byref(msg))
                    user32.DispatchMessageW(ctypes.byref(msg))
                with self._lock:
                    pending, self._pending = self._pending, None
                if pending:
                    if pending[0] == "show":
                        if self._ensure_hwnd():  # revive a dead window before drawing
                            self._draw(pending[1], pending[2], pending[3])
                    elif self._visible and self._hwnd:
                        user32.ShowWindow(self._hwnd, SW_HIDE)
                        self._visible = False
                user32.MsgWaitForMultipleObjects(0, None, False, 16, 0x04FF)
            except Exception:
                time.sleep(0.05)

    def _create_window(self) -> None:
        import win32con  # noqa: F401  (ensures pywin32 DLLs are loaded)
        import win32gui

        wc = win32gui.WNDCLASS()
        wc.lpszClassName = self._CLASS_NAME
        # Handle WM_CLOSE (return 0 = swallow, don't destroy); everything else
        # falls through to DefWindowProc. Without this, a stray WM_CLOSE would make
        # DefWindowProc DestroyWindow the overlay and it would stay gone until the
        # app restarted. Keep a reference alive so the callback isn't GC'd.
        self._wndproc = {WM_CLOSE: lambda hwnd, msg, wp, lp: 0}
        wc.lpfnWndProc = self._wndproc
        try:
            win32gui.RegisterClass(wc)
        except Exception:
            pass  # already registered (first launch registers it process-wide)

        ex_style = (
            WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
            | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        )
        self._hwnd = win32gui.CreateWindowEx(
            ex_style, self._CLASS_NAME, "NihongoViewer Overlay", WS_POPUP,
            0, 0, 0, 0, 0, 0, 0, None,
        )
        if not self._hwnd:
            _debug("CreateWindowEx failed")
            return  # _ensure_hwnd sees a falsy _hwnd and retries next draw
        # Assert topmost once here rather than on every frame. Re-poking the
        # z-order each draw churns the window band and can make a windowed
        # DirectX game re-present (a black flash); WS_EX_TOPMOST keeps us on top.
        user32.SetWindowPos(self._hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                            SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
        _debug(f"overlay window created (hwnd={self._hwnd})")
        # Note: no SetWindowDisplayAffinity needed. WGC captures only the target
        # game window's own surface, so this separate overlay window is never in
        # the capture — and it stays visible in the user's screen recordings.

    def _draw(self, img: Image.Image, x: int, y: int) -> None:
        w, h = img.size
        if w <= 0 or h <= 0:  # nothing to show — and a 0-size DIB would fail
            return
        screen_dc = user32.GetDC(0)
        mem_dc = gdi32.CreateCompatibleDC(screen_dc)
        hbmp = old = None
        # try/finally so the DC + bitmap are ALWAYS released, even if a step below
        # raises. A leak here would exhaust the process GDI handle pool over time
        # and make CreateDIBSection start failing — the very fault that used to
        # kill the overlay thread; releasing every time keeps it from escalating.
        try:
            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h  # top-down so PIL's row order is preserved
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = BI_RGB

            bits = ctypes.c_void_p()
            hbmp = gdi32.CreateDIBSection(
                screen_dc, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(bits), None, 0
            )
            if not hbmp or not bits:
                return  # GDI is out of memory this frame; try again next frame
            old = gdi32.SelectObject(mem_dc, hbmp)

            # RGBA (straight alpha) -> BGRA premultiplied, as UpdateLayeredWindow wants.
            rgba = np.asarray(img, dtype=np.uint8)
            a = rgba[:, :, 3:4].astype(np.uint16)
            bgra = np.empty_like(rgba)
            bgra[:, :, 0] = (rgba[:, :, 2] * a[:, :, 0] // 255).astype(np.uint8)  # B
            bgra[:, :, 1] = (rgba[:, :, 1] * a[:, :, 0] // 255).astype(np.uint8)  # G
            bgra[:, :, 2] = (rgba[:, :, 0] * a[:, :, 0] // 255).astype(np.uint8)  # R
            bgra[:, :, 3] = rgba[:, :, 3]
            raw = bgra.tobytes()
            ctypes.memmove(bits, raw, len(raw))

            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            pt_dst = wintypes.POINT(int(x), int(y))
            pt_src = wintypes.POINT(0, 0)
            size = wintypes.SIZE(w, h)
            # Update the layered content (position, size and pixels atomically) FIRST,
            # then reveal the window — so it never briefly shows stale/empty content.
            ok = user32.UpdateLayeredWindow(
                self._hwnd, screen_dc, ctypes.byref(pt_dst), ctypes.byref(size),
                mem_dc, ctypes.byref(pt_src), 0, ctypes.byref(blend), ULW_ALPHA,
            )
            if not ok:
                # The layered window went bad while its HWND still reports valid —
                # a display-mode / DWM change (e.g. a game toggling a backlog or
                # fullscreen) can do this. `IsWindow` stays true, so `_ensure_hwnd`
                # would never rebuild it and the overlay would be gone until the app
                # restarts. Tear it down here so the next draw builds a fresh one;
                # the following capture tick (or the hide/show hotkey) then restores
                # the overlay on its own.
                _debug("UpdateLayeredWindow failed — rebuilding overlay window")
                self._destroy_window()
                return
            # Make sure the window is actually on screen. A cached `_visible` flag
            # isn't enough: a display-mode / fullscreen switch can HIDE our topmost
            # window without going through our own hide path, leaving `_visible`
            # stuck True. UpdateLayeredWindow keeps succeeding (so nothing is
            # logged), yet the overlay is invisible until restart — the other
            # "overlay gone forever" cause. Check the real window state every draw
            # and re-show (and re-assert topmost) if it was hidden out from under us.
            if not user32.IsWindowVisible(self._hwnd):
                if self._visible:
                    _debug("overlay was hidden externally — re-showing")
                user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
                user32.SetWindowPos(
                    self._hwnd, ctypes.c_void_p(HWND_TOPMOST), 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)
            self._visible = True
        finally:
            if old:
                gdi32.SelectObject(mem_dc, old)
            if hbmp:
                gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)
