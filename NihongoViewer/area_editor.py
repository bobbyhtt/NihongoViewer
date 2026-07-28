"""On-screen editor for Area-mode rectangles (up to 4 detect+translate pairs).

A translucent, top-most native window covers the whole virtual desktop, so the
user can place the boxes anywhere on screen (area mode grabs raw screen pixels,
not a window). Each *area* is a detect box (what to OCR) paired with a translate
box (where to draw its translation). The user drags a box to move it and drags its
edge/corner handles to resize. **Add Area** spawns a new pair (max 4); **Remove
Area** (shown once there are 2+) drops the last pair. Enter/Save confirms;
Esc/Cancel aborts.

`edit(areas)` blocks (runs its own Win32 message loop on the calling thread) and
returns {"areas": [{"detect": {...}, "translate": {...}}, ...]} in absolute screen
px, or None if cancelled. `areas` in is the same shape (at least one pair).
Internally it works in window-local coordinates (0,0 = virtual-screen top-left) and
converts to/from absolute at the boundary.

Editing is modal (one at a time), so the active editor is kept in a module global
that the (shared) window-class message handlers delegate to.
"""

import win32api
import win32con
import win32gui

_CLASS = "NihongoViewerAreaEditor"
_HANDLE = 6   # half-size of a resize-handle square, px
_MIN = 40     # minimum box width/height, px
_MAX = 4      # maximum number of detect+translate area pairs

_current = None  # the active _AreaEditor


def edit(areas: list):
    """Open the fullscreen editor; block until Save/Cancel. See module doc.

    `areas` is a list of {"detect": {x,y,w,h}, "translate": {x,y,w,h}} in absolute
    screen px (at least one pair). Returns {"areas": [...]} (same shape) or None.
    """
    global _current
    _current = _AreaEditor(areas)
    try:
        return _current.run()
    finally:
        _current = None


def _delegate(method: str):
    def handler(hwnd, msg, wparam, lparam):
        if _current is None:
            return win32gui.DefWindowProc(hwnd, msg, wparam, lparam)
        return getattr(_current, method)(hwnd, msg, wparam, lparam)
    return handler


def _register_class() -> None:
    wc = win32gui.WNDCLASS()
    wc.lpszClassName = _CLASS
    wc.hCursor = win32gui.LoadCursor(0, win32con.IDC_ARROW)
    wc.lpfnWndProc = {
        win32con.WM_PAINT: _delegate("on_paint"),
        win32con.WM_LBUTTONDOWN: _delegate("on_lbdown"),
        win32con.WM_MOUSEMOVE: _delegate("on_mousemove"),
        win32con.WM_LBUTTONUP: _delegate("on_lbup"),
        win32con.WM_KEYDOWN: _delegate("on_keydown"),
    }
    try:
        win32gui.RegisterClass(wc)
    except win32gui.error:
        pass  # already registered (a previous edit session)


def _signed(lparam):
    x, y = lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
    return (x - 0x10000 if x >= 0x8000 else x,
            y - 0x10000 if y >= 0x8000 else y)


class _AreaEditor:
    def __init__(self, areas: list):
        # Cover the whole virtual desktop (all monitors); its origin can be negative.
        self.left = win32api.GetSystemMetrics(win32con.SM_XVIRTUALSCREEN)
        self.top = win32api.GetSystemMetrics(win32con.SM_YVIRTUALSCREEN)
        self.w = win32api.GetSystemMetrics(win32con.SM_CXVIRTUALSCREEN)
        self.h = win32api.GetSystemMetrics(win32con.SM_CYVIRTUALSCREEN)
        # Store each pair's rects in window-local coords (absolute px minus origin).
        self.areas = [{"detect": self._to_local(a["detect"]),
                       "translate": self._to_local(a["translate"])}
                      for a in areas] or [self._default_area()]
        self.drag = None       # (idx, kind, handle|'move', mx0, my0, rect0)
        self.result = None
        self.hwnd = None
        self._layout_buttons()

    def _layout_buttons(self):
        """Bottom-right button cluster (right->left: Save, Cancel, Add, Remove).

        Rects are fixed by window size, so lay them out once (hit-testing may run
        before the first paint). Add/Remove are drawn/hit only when applicable
        (see `on_paint` / `_hit`), but their rects always exist.
        """
        bw, bh, pad = 132, 34, 14
        by0, by1 = self.h - pad - bh, self.h - pad

        def col(n):  # n-th button counting from the right edge (0 = rightmost)
            x1 = self.w - pad - n * (bw + pad)
            return (x1 - bw, by0, x1, by1)

        self.btn_save = col(0)
        self.btn_cancel = col(1)
        self.btn_add = col(2)
        self.btn_remove = col(3)

    def _default_area(self) -> dict:
        """A sensible starting pair if none was supplied (shouldn't normally happen)."""
        return {
            "detect": {"x": int(self.w * 0.15), "y": int(self.h * 0.60),
                       "w": int(self.w * 0.70), "h": int(self.h * 0.20)},
            "translate": {"x": int(self.w * 0.15), "y": int(self.h * 0.82),
                          "w": int(self.w * 0.70), "h": int(self.h * 0.14)},
        }

    # -- lifecycle ------------------------------------------------------------
    def run(self):
        _register_class()
        ex = win32con.WS_EX_LAYERED | win32con.WS_EX_TOPMOST | win32con.WS_EX_TOOLWINDOW
        self.hwnd = win32gui.CreateWindowEx(
            ex, _CLASS, "NihongoViewer Area", win32con.WS_POPUP,
            self.left, self.top, self.w, self.h, 0, 0, 0, None)
        win32gui.SetLayeredWindowAttributes(self.hwnd, 0, 180, win32con.LWA_ALPHA)
        win32gui.ShowWindow(self.hwnd, win32con.SW_SHOW)
        try:
            win32gui.SetForegroundWindow(self.hwnd)  # so it receives Enter/Esc
        except Exception:
            pass
        win32gui.PumpMessages()  # blocks until _finish -> PostQuitMessage
        return self.result

    def _to_local(self, r: dict) -> dict:
        return {"x": int(r["x"]) - self.left, "y": int(r["y"]) - self.top,
                "w": int(r["w"]), "h": int(r["h"])}

    def _to_abs(self, r: dict) -> dict:
        return {"x": r["x"] + self.left, "y": r["y"] + self.top,
                "w": r["w"], "h": r["h"]}

    def _finish(self, save: bool):
        if save:
            self.result = {"areas": [
                {"detect": self._to_abs(a["detect"]),
                 "translate": self._to_abs(a["translate"])}
                for a in self.areas]}
        win32gui.DestroyWindow(self.hwnd)
        win32gui.PostQuitMessage(0)

    # -- area add / remove ----------------------------------------------------
    def _add_area(self):
        """Append a new pair, cascaded off the last so it's visible and separate."""
        if len(self.areas) >= _MAX:
            return
        off = 34

        def shifted(r):
            return {"x": min(max(0, r["x"] + off), max(0, self.w - r["w"])),
                    "y": min(max(0, r["y"] + off), max(0, self.h - r["h"])),
                    "w": r["w"], "h": r["h"]}

        last = self.areas[-1]
        self.areas.append({"detect": shifted(last["detect"]),
                           "translate": shifted(last["translate"])})

    def _remove_area(self):
        """Drop the most recently added pair (kept at 1 minimum)."""
        if len(self.areas) >= 2:
            self.areas.pop()

    # -- input ----------------------------------------------------------------
    def on_keydown(self, hwnd, msg, wparam, lparam):
        if wparam == win32con.VK_RETURN:
            self._finish(True)
        elif wparam == win32con.VK_ESCAPE:
            self._finish(False)
        return 0

    def on_lbdown(self, hwnd, msg, wparam, lparam):
        x, y = _signed(lparam)
        hit = self._hit(x, y)
        if hit == "save":
            self._finish(True)
        elif hit == "cancel":
            self._finish(False)
        elif hit == "add":
            self._add_area()
            win32gui.InvalidateRect(hwnd, None, True)
        elif hit == "remove":
            self._remove_area()
            win32gui.InvalidateRect(hwnd, None, True)
        elif hit:
            idx, kind, handle = hit
            rect0 = dict(self.areas[idx][kind])
            self.drag = (idx, kind, handle, x, y, rect0)
            win32gui.SetCapture(hwnd)
        return 0

    def on_mousemove(self, hwnd, msg, wparam, lparam):
        if not self.drag:
            return 0
        x, y = _signed(lparam)
        idx, kind, handle, mx0, my0, r0 = self.drag
        self.areas[idx][kind] = self._apply(r0, handle, x - mx0, y - my0)
        win32gui.InvalidateRect(hwnd, None, True)
        return 0

    def on_lbup(self, hwnd, msg, wparam, lparam):
        if self.drag:
            self.drag = None
            win32gui.ReleaseCapture()
        return 0

    # -- geometry -------------------------------------------------------------
    def _handles(self, r):
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        cx, cy = x + w // 2, y + h // 2
        return {"nw": (x, y), "n": (cx, y), "ne": (x + w, y), "e": (x + w, cy),
                "se": (x + w, y + h), "s": (cx, y + h), "sw": (x, y + h), "w": (x, cy)}

    @staticmethod
    def _in(box, x, y):
        return box[0] <= x <= box[2] and box[1] <= y <= box[3]

    def _hit(self, x, y):
        if self._in(self.btn_save, x, y):
            return "save"
        if self._in(self.btn_cancel, x, y):
            return "cancel"
        if len(self.areas) < _MAX and self._in(self.btn_add, x, y):
            return "add"
        if len(self.areas) >= 2 and self._in(self.btn_remove, x, y):
            return "remove"
        # Handles first (small, precise). Iterate top-most area first and the
        # translate box before the detect box, so an overlapping box on top wins.
        for idx in range(len(self.areas) - 1, -1, -1):
            for kind in ("translate", "detect"):
                for hname, (hx, hy) in self._handles(self.areas[idx][kind]).items():
                    if abs(x - hx) <= _HANDLE + 3 and abs(y - hy) <= _HANDLE + 3:
                        return (idx, kind, hname)
        for idx in range(len(self.areas) - 1, -1, -1):
            for kind in ("translate", "detect"):
                r = self.areas[idx][kind]
                if r["x"] <= x <= r["x"] + r["w"] and r["y"] <= y <= r["y"] + r["h"]:
                    return (idx, kind, "move")
        return None

    def _apply(self, r0, handle, dx, dy):
        x, y, w, h = r0["x"], r0["y"], r0["w"], r0["h"]
        left, top, right, bottom = x, y, x + w, y + h
        if handle == "move":
            left, right = left + dx, right + dx
            top, bottom = top + dy, bottom + dy
            if left < 0:
                right -= left; left = 0
            if top < 0:
                bottom -= top; top = 0
            if right > self.w:
                left -= right - self.w; right = self.w
            if bottom > self.h:
                top -= bottom - self.h; bottom = self.h
        else:
            if handle in ("nw", "w", "sw"):
                left = max(0, min(x + dx, right - _MIN))
            if handle in ("ne", "e", "se"):
                right = min(self.w, max(x + w + dx, left + _MIN))
            if handle in ("nw", "n", "ne"):
                top = max(0, min(y + dy, bottom - _MIN))
            if handle in ("sw", "s", "se"):
                bottom = min(self.h, max(y + h + dy, top + _MIN))
        return {"x": int(left), "y": int(top), "w": int(right - left), "h": int(bottom - top)}

    # -- painting -------------------------------------------------------------
    def on_paint(self, hwnd, msg, wparam, lparam):
        hdc, ps = win32gui.BeginPaint(hwnd)
        try:
            veil = win32gui.CreateSolidBrush(win32api.RGB(12, 14, 18))
            win32gui.FillRect(hdc, (0, 0, self.w, self.h), veil)
            win32gui.DeleteObject(veil)
            win32gui.SetBkMode(hdc, win32con.TRANSPARENT)
            for i, a in enumerate(self.areas, 1):
                suffix = f" {i}" if len(self.areas) > 1 else ""
                self._draw_box(hdc, a["detect"], win32api.RGB(80, 200, 90),
                               f"Detect area{suffix}")
                self._draw_box(hdc, a["translate"], win32api.RGB(90, 150, 255),
                               f"Translate box{suffix}")
            self._text(hdc, win32api.RGB(235, 235, 235),
                       "Drag a box to move · drag its edges/corners to resize",
                       (0, 10, self.w, 34), win32con.DT_CENTER)
            self._button(hdc, self.btn_save, "Save  (Enter)", win32api.RGB(40, 120, 60))
            self._button(hdc, self.btn_cancel, "Cancel  (Esc)", win32api.RGB(70, 70, 82))
            if len(self.areas) < _MAX:
                self._button(hdc, self.btn_add, "+ Add Area", win32api.RGB(55, 105, 175))
            if len(self.areas) >= 2:
                self._button(hdc, self.btn_remove, "- Remove Area", win32api.RGB(150, 60, 60))
        finally:
            win32gui.EndPaint(hwnd, ps)
        return 0

    def _draw_box(self, hdc, r, color, label):
        x, y, w, h = r["x"], r["y"], r["w"], r["h"]
        pen = win32gui.CreatePen(win32con.PS_SOLID, 2, color)
        old_pen = win32gui.SelectObject(hdc, pen)
        old_brush = win32gui.SelectObject(hdc, win32gui.GetStockObject(win32con.NULL_BRUSH))
        win32gui.Rectangle(hdc, x, y, x + w, y + h)
        win32gui.SelectObject(hdc, old_pen)
        win32gui.SelectObject(hdc, old_brush)
        win32gui.DeleteObject(pen)
        hb = win32gui.CreateSolidBrush(color)
        for hx, hy in self._handles(r).values():
            win32gui.FillRect(hdc, (hx - _HANDLE, hy - _HANDLE, hx + _HANDLE, hy + _HANDLE), hb)
        win32gui.DeleteObject(hb)
        self._text(hdc, color, label, (x + 5, y + 4, x + 240, y + 26), win32con.DT_LEFT)

    def _button(self, hdc, box, text, color):
        b = win32gui.CreateSolidBrush(color)
        win32gui.FillRect(hdc, box, b)
        win32gui.DeleteObject(b)
        self._text(hdc, win32api.RGB(255, 255, 255), text, box,
                   win32con.DT_CENTER | win32con.DT_VCENTER)

    @staticmethod
    def _text(hdc, color, text, box, align):
        win32gui.SetTextColor(hdc, color)
        win32gui.DrawText(hdc, text, -1, box, align | win32con.DT_SINGLELINE)
