"""Global (system-wide) hotkey for hiding/showing the overlay, Windows-only.

CLAUDE.md requires the hide/show hotkey to work even when the app isn't focused,
so a DOM key listener in the pywebview UI isn't enough — that only fires while our
window has focus. Instead we register a real OS-level hotkey with Win32
`RegisterHotKey`.

`RegisterHotKey(NULL, ...)` binds the hotkey to the *calling thread's* message
queue and delivers `WM_HOTKEY` there, so the registration and the message pump
have to live on the same dedicated thread. Other threads talk to that thread by
posting it messages (`PostThreadMessage`): one to rebind to a new key, one to
shut it down. The user's callback runs on the hotkey thread — keep it quick and
thread-safe (in this app it just flips an overlay flag).
"""

import ctypes
import threading
from ctypes import wintypes
from typing import Callable, Optional

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# --- Win32 constants ---------------------------------------------------------
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000  # don't auto-repeat while the combo is held

WM_HOTKEY = 0x0312
WM_APP = 0x8000
_WM_REBIND = WM_APP + 1
_WM_STOP = WM_APP + 2

_HOTKEY_ID = 1  # only one hotkey; a fixed id is fine

# Modifier tokens accepted in a hotkey spec -> their MOD_ flag.
_MODIFIERS = {
    "CTRL": MOD_CONTROL, "CONTROL": MOD_CONTROL,
    "SHIFT": MOD_SHIFT,
    "ALT": MOD_ALT,
    "WIN": MOD_WIN, "META": MOD_WIN, "CMD": MOD_WIN, "SUPER": MOD_WIN,
}

# Named (non-alphanumeric) keys -> virtual-key code.
_NAMED_KEYS = {
    "SPACE": 0x20, "ENTER": 0x0D, "RETURN": 0x0D, "TAB": 0x09,
    "ESC": 0x1B, "ESCAPE": 0x1B, "BACKSPACE": 0x08, "DELETE": 0x2E, "DEL": 0x2E,
    "INSERT": 0x2D, "INS": 0x2D, "HOME": 0x24, "END": 0x23,
    "PAGEUP": 0x21, "PGUP": 0x21, "PAGEDOWN": 0x22, "PGDN": 0x22,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    **{f"F{n}": 0x70 + (n - 1) for n in range(1, 13)},  # F1..F12
}

# Symbol keys -> virtual-key code (US layout). Both the plain and shifted glyph
# of a key map to the same VK, since the Shift modifier is captured separately.
_SYMBOL_KEYS = {
    "-": 0xBD, "_": 0xBD, "=": 0xBB, "+": 0xBB,
    "[": 0xDB, "{": 0xDB, "]": 0xDD, "}": 0xDD,
    "\\": 0xDC, "|": 0xDC, ";": 0xBA, ":": 0xBA,
    "'": 0xDE, '"': 0xDE, ",": 0xBC, "<": 0xBC,
    ".": 0xBE, ">": 0xBE, "/": 0xBF, "?": 0xBF,
    "`": 0xC0, "~": 0xC0,
    # Shifted top-row digits resolve to the digit's own key.
    "!": ord("1"), "@": ord("2"), "#": ord("3"), "$": ord("4"), "%": ord("5"),
    "^": ord("6"), "&": ord("7"), "*": ord("8"), "(": ord("9"), ")": ord("0"),
}

user32.RegisterHotKey.restype = wintypes.BOOL
user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.PostThreadMessageW.restype = wintypes.BOOL
user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def parse_hotkey(spec: str) -> Optional[tuple[int, int]]:
    """Parse "Ctrl+Shift+H" (or a bare "P") into (modifiers, vk), or None.

    Modifiers are optional — a single key with no modifier is allowed, though it
    is then captured globally and won't reach other apps. Exactly one
    non-modifier key is required.
    """
    if not spec:
        return None
    # Split on the spaced " + " our UI uses so the '+' key itself is unambiguous;
    # fall back to bare '+' for compact specs like "Ctrl+Shift+H".
    tokens = spec.split(" + ") if (" + " in spec or spec.strip() == "+") else spec.split("+")
    modifiers = 0
    key_vk: Optional[int] = None
    for raw in tokens:
        token = raw.strip().upper()
        if not token:
            continue
        if token in _MODIFIERS:
            modifiers |= _MODIFIERS[token]
        elif key_vk is not None:
            return None  # more than one non-modifier key
        elif len(token) == 1 and token.isalnum():
            key_vk = ord(token)
        elif token in _NAMED_KEYS:
            key_vk = _NAMED_KEYS[token]
        elif token in _SYMBOL_KEYS:
            key_vk = _SYMBOL_KEYS[token]
        else:
            return None  # unrecognized token
    if key_vk is None:
        return None
    return modifiers, key_vk


class HotkeyManager:
    """Owns a background thread that keeps one global hotkey registered.

    Construct once; call `set_hotkey(spec)` to (re)bind and `close()` to stop.
    `callback` is invoked on the hotkey thread each time the combo is pressed.
    """

    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback
        self._pending: Optional[tuple[int, int]] = None   # next (mod, vk) to bind
        self._current: Optional[tuple[int, int]] = None   # currently-bound combo
        self._registered = False
        self._rebind_ok = False           # did the last RegisterHotKey succeed?
        self._rebind_done = threading.Event()
        self._thread_id: Optional[int] = None
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="hotkey", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=2)

    def set_hotkey(self, spec: str) -> dict:
        """Bind `spec` as the global hotkey, waiting for the result.

        Returns {"ok": bool, "reason": str}. `reason` is "invalid" for an
        unparseable spec or "in_use" when the OS refused it (another app already
        owns that combo). The rebind runs on the hotkey thread; we block briefly
        for its outcome so the caller (and UI) know whether it actually took.
        """
        parsed = parse_hotkey(spec)
        if parsed is None:
            # Still unbind whatever was active, but report the spec as invalid.
            with self._lock:
                self._pending = None
            self._rebind_done.clear()
            if self._thread_id is not None:
                user32.PostThreadMessageW(self._thread_id, _WM_REBIND, 0, 0)
                self._rebind_done.wait(timeout=1.0)
            return {"ok": False, "reason": "invalid"}
        with self._lock:
            self._pending = parsed
        self._rebind_done.clear()
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, _WM_REBIND, 0, 0)
            self._rebind_done.wait(timeout=1.0)
        with self._lock:
            ok = self._rebind_ok
        return {"ok": ok, "reason": "" if ok else "in_use"}

    def close(self) -> None:
        if self._thread_id is not None:
            user32.PostThreadMessageW(self._thread_id, _WM_STOP, 0, 0)

    # -- hotkey thread --------------------------------------------------------
    def _run(self) -> None:
        self._thread_id = kernel32.GetCurrentThreadId()
        # Force the OS to create this thread's message queue before anyone posts
        # to it (PostThreadMessage silently drops messages if none exists yet).
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
        self._ready.set()

        while True:
            ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):  # WM_QUIT / error
                break
            if msg.message == WM_HOTKEY:
                try:
                    self._callback()
                except Exception:
                    pass  # a failing callback must not kill the hotkey thread
            elif msg.message == _WM_REBIND:
                self._rebind()
            elif msg.message == _WM_STOP:
                break
        self._unregister()

    def _unregister(self) -> None:
        if self._registered:
            user32.UnregisterHotKey(None, _HOTKEY_ID)
            self._registered = False

    def _register(self, combo: tuple[int, int]) -> bool:
        modifiers, vk = combo
        ok = bool(user32.RegisterHotKey(None, _HOTKEY_ID, modifiers | MOD_NOREPEAT, vk))
        self._registered = ok
        return ok

    def _rebind(self) -> None:
        # A hotkey can't be re-bound to the same id without first releasing it,
        # so drop the old one, try the new one, and — if the OS refuses it (the
        # combo is already owned by another app) — restore the old binding so the
        # user is never left without a working hotkey.
        with self._lock:
            pending, previous = self._pending, self._current
        self._unregister()
        if pending is None:  # explicit unbind
            with self._lock:
                self._current, self._rebind_ok = None, True
            self._rebind_done.set()
            return
        ok = self._register(pending)
        if ok:
            with self._lock:
                self._current = pending
        elif previous is not None and previous != pending:
            self._register(previous)  # keep the previously-working hotkey alive
        with self._lock:
            self._rebind_ok = ok
        self._rebind_done.set()
