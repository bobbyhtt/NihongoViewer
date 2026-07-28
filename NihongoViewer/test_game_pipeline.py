"""Ad-hoc pipeline tester for a live game window.

Mirrors main.Api.process_frame (Tier-0 pixel-signature skip -> OCR -> group_lines
-> per-region translate) but runs headless in a loop and logs every NEW detected
line as JA + EN to a UTF-8 file, so we can click through a game and inspect
detection/translation quality without the GUI/overlay.

Usage (from NihongoViewer/ package dir, with the nested venv):
    .venv/Scripts/python.exe test_game_pipeline.py --match 領界 --secs 120

Writes:
    <out>/pipeline_log.txt   one block per new line (ja, en, timing, box count)
    <out>/frame_XXX.jpg      the captured frame for each new line (for OCR check)
    <out>/regions_XXX.txt    raw per-region OCR text + boxes (pre-grouping)
"""

import argparse
import ctypes
from ctypes import wintypes
import io
import time
import traceback
from pathlib import Path

import win32api
import win32con

import capture
import ocr
import translate

_u32 = ctypes.windll.user32
_VK_SPACE = 0x20
_VK_RETURN = 0x0D
_VK_DOWN = 0x28


def _post_key(hwnd: int, vk: int, *, extended: bool = False) -> None:
    """PostMessage a key down+up straight to the window's queue (no focus needed).

    Extended keys (the arrows) MUST set the extended-key lParam bit (24) or SDL
    ignores them — that was why Down+Enter first appeared not to work.
    """
    sc = _u32.MapVirtualKeyW(vk, 0)
    ext = (1 << 24) if extended else 0
    down = 1 | (sc << 16) | ext
    up = 1 | (sc << 16) | ext | (1 << 30) | (1 << 31)
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, vk, down)
    time.sleep(0.03)
    win32api.PostMessage(hwnd, win32con.WM_KEYUP, vk, up)


def _pick_first_choice(hwnd: int) -> bool:
    """Select the first option of a Ren'Py choice menu: Down (focus the first
    option) then Enter (confirm). Verified on this pygame window.

    Harmless if misfired on an ordinary dialogue screen — Down has nothing to
    focus there, and Enter just advances the line like Space. Returns False on a
    stale handle so the caller re-resolves instead of crashing.
    """
    try:
        hwnd = int(hwnd)
        _post_key(hwnd, _VK_DOWN, extended=True)
        time.sleep(0.3)
        _post_key(hwnd, _VK_RETURN)
        return True
    except Exception:
        return False


def log(fh, msg=""):
    fh.write(msg + "\n")
    fh.flush()


def _foreground(hwnd: int) -> None:
    """Best-effort bring `hwnd` to the foreground (AttachThreadInput trick).

    Needed because a background process's SetForegroundWindow is otherwise
    blocked by Windows' foreground lock. Only used for auto-advance on a
    non-adult VN the user asked us to drive unattended.
    """
    try:
        fg = _u32.GetForegroundWindow()
        if fg == hwnd:
            return
        cur = ctypes.windll.kernel32.GetCurrentThreadId()
        other = _u32.GetWindowThreadProcessId(fg, None)
        tgt = _u32.GetWindowThreadProcessId(hwnd, None)
        for t in {cur, other}:
            _u32.AttachThreadInput(t, tgt, True)
        _u32.SetForegroundWindow(hwnd)
        for t in {cur, other}:
            _u32.AttachThreadInput(t, tgt, False)
    except Exception:
        pass


def _advance(hwnd: int, *, no_focus: bool = True) -> bool:
    """Advance one Ren'Py dialogue line with a Space press. Returns False if the
    window handle was invalid (Ren'Py recreates its window — the caller should
    then re-resolve rather than crash; PostMessage/keybd_event on a stale handle
    raises).

    no_focus=True posts WM_KEYDOWN/UP straight to the window's message queue so
    the game advances WITHOUT taking foreground — the user can keep working in
    another window (verified on this pygame/SDL window). no_focus=False falls
    back to foregrounding + a hardware-level keystroke for windows that ignore
    synthetic messages.

    Space advances dialogue and is a no-op on menus/choices, so a stall at a
    choice screen just re-captures the same frame (deduped) rather than picking
    something at random.
    """
    hwnd = int(hwnd)
    try:
        if no_focus:
            _post_key(hwnd, _VK_SPACE)
            return True
        _foreground(hwnd)
        KEYEVENTF_KEYUP = 0x0002
        _u32.keybd_event(_VK_SPACE, 0, 0, 0)
        time.sleep(0.03)
        _u32.keybd_event(_VK_SPACE, 0, KEYEVENTF_KEYUP, 0)
        return True
    except Exception:
        return False


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--match", default="領界",
                    help="substring of the target window title")
    ap.add_argument("--hwnd", type=int, default=0,
                    help="target window handle directly (overrides --match)")
    ap.add_argument("--secs", type=float, default=90.0,
                    help="max wall-clock seconds for the capture loop")
    ap.add_argument("--max-lines", type=int, default=0,
                    help="stop after this many distinct lines (0 = no limit)")
    ap.add_argument("--stable-wait", type=float, default=0.35,
                    help="debounce: re-check a changed frame after this many "
                         "seconds; OCR only once it holds still (skips mid-typing)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="seconds between capture ticks (real app ~1 fps)")
    ap.add_argument("--speed", default="balanced", choices=list(ocr.SPEED_PRESETS))
    ap.add_argument("--auto-advance", action="store_true",
                    help="drive the VN ourselves: press Space every --advance-every s")
    ap.add_argument("--advance-every", type=float, default=2.5,
                    help="seconds between auto Space presses")
    ap.add_argument("--stall-picks", type=int, default=3,
                    help="after this many advances with no new line (a choice "
                         "menu stall), pick the first choice (Down+Enter); 0 disables")
    ap.add_argument("--focus-advance", action="store_true",
                    help="advance by foregrounding + hardware keystroke (steals "
                         "focus); default posts a no-focus message instead")
    ap.add_argument("--out", default=None, help="output dir")
    args = ap.parse_args()

    out = Path(args.out) if args.out else Path(__file__).parent / "_pipeline_out"
    out.mkdir(exist_ok=True)
    fh = io.open(out / "pipeline_log.txt", "w", encoding="utf-8")

    # -- find the window ------------------------------------------------------
    def resolve():
        """Current hwnd for the target, by title substring (Ren'Py recreates its
        window, so a fixed handle goes stale — re-resolve on demand)."""
        for w in capture.list_windows():
            if args.match in w["title"] and "File Explorer" not in w["title"]:
                return w["id"], w["title"]
        return None, None

    if args.hwnd:
        hwnd, title = args.hwnd, "(by hwnd)"
    else:
        hwnd, title = resolve()
    if hwnd is None:
        log(fh, f"NO WINDOW matching {args.match!r}. Open windows:")
        for w in capture.list_windows():
            log(fh, f"  {w['id']}  {w['title']}")
        fh.close()
        print("no window; see log")
        return
    log(fh, f"target window: {hwnd}  {title}")
    win_title = (title or "").strip()
    # Un-minimize the game so WGC has a surface (fullscreen titles minimize on
    # focus-loss). Best-effort; the user should also keep the game foreground.
    capture.show_window(int(hwnd))

    # -- load stages ----------------------------------------------------------
    t0 = time.time()
    engine = ocr.create_engine(ocr.DEFAULT_ENGINE, args.speed)
    engine.load()
    log(fh, f"OCR loaded ({ocr.DEFAULT_ENGINE}, {args.speed}) in {time.time()-t0:.1f}s")
    t0 = time.time()
    translator = translate.create_translator()
    translator.load()
    log(fh, f"translator loaded ({translator.name}) in {time.time()-t0:.1f}s")
    log(fh, "=" * 60)

    # -- capture loop (mirrors process_frame's tiered skipping) ---------------
    last_sig = None      # signature of the last frame we actually processed
    pending_sig = None   # a changed frame awaiting stability (see debounce below)
    last_ja = None
    n = 0
    last_advance = 0.0
    dead_advances = 0    # advances since the last new line — a stall == choice menu
    deadline = time.time() + args.secs
    while time.time() < deadline:
        tick = time.time()
        # Drive the VN ourselves in auto mode: a Space press every advance-every
        # seconds. Translation (~3 s) paces the loop, so this lands roughly once
        # per processed line.
        if args.auto_advance and tick - last_advance >= args.advance_every:
            if args.stall_picks and dead_advances >= args.stall_picks:
                # Space stopped advancing the screen — almost always a Ren'Py
                # choice menu, which Space can't select. Pick the first option.
                if _pick_first_choice(int(hwnd)):
                    log(fh, "** stall: picked first choice")
                dead_advances = 0
                last_advance = tick
            elif _advance(int(hwnd), no_focus=not args.focus_advance):
                last_advance = tick
                dead_advances += 1
            elif not args.hwnd:
                # Stale handle (Ren'Py recreated its window) — re-resolve so the
                # next tick advances the new window instead of crashing.
                capture.stop_capture_session()
                new_hwnd, _ = resolve()
                if new_hwnd:
                    log(fh, f"** advance: window handle changed {hwnd} -> {new_hwnd}")
                    hwnd = new_hwnd
                    last_sig = None
        # Ren'Py recreates its window (intro->game, fullscreen toggle), so the
        # handle can go stale mid-run. On a dead handle or a WGC init failure,
        # re-resolve by title instead of giving up.
        if not args.hwnd and not capture.window_exists(int(hwnd)):
            capture.stop_capture_session()
            new_hwnd, _ = resolve()
            if new_hwnd is None:
                log(fh, "!! window gone; re-resolve failed — retrying")
                time.sleep(args.interval)
                continue
            if new_hwnd != hwnd:
                log(fh, f"** window handle changed {hwnd} -> {new_hwnd}")
                hwnd = new_hwnd
                last_sig = None
        try:
            img = capture.capture_window_image(int(hwnd))
        except Exception as exc:
            log(fh, f"capture error ({exc}); re-resolving")
            capture.stop_capture_session()
            if not args.hwnd:
                new_hwnd, _ = resolve()
                if new_hwnd:
                    hwnd = new_hwnd
            time.sleep(args.interval)
            continue
        if img is None:
            time.sleep(args.interval)
            continue

        sig = capture.frame_signature(img)
        if capture.signatures_match(sig, last_sig):
            pending_sig = None
            time.sleep(args.interval)
            continue
        # Debounce: VN text types out character-by-character, so a frame that just
        # changed may be mid-animation. Require the frame to hold the SAME new
        # signature across two consecutive captures before we OCR it — otherwise we
        # grab a half-typed fragment ("俺としては、下駄") that hallucinates a bad
        # translation. A settled dialogue box passes on the next tick.
        if not capture.signatures_match(sig, pending_sig):
            pending_sig = sig
            time.sleep(args.stable_wait)
            continue
        pending_sig = None
        last_sig = sig

        try:
            t_ocr = time.time()
            result = engine.recognize(img)
            ocr_ms = (time.time() - t_ocr) * 1000
        except Exception as exc:
            log(fh, f"OCR ERROR: {exc}\n{traceback.format_exc()}")
            time.sleep(args.interval)
            continue

        ja = result.text
        if ja == last_ja:
            time.sleep(args.interval)
            continue
        if not ja.strip():
            last_ja = ja
            time.sleep(args.interval)
            continue

        # Drop any region that is just the window's own title bar (windowed-mode
        # capture includes it — finding #2), so it neither pollutes the output nor
        # burns a line slot.
        regions = [r for r in ocr.group_lines(result.regions)
                   if r.text.strip() and r.text.strip() != win_title]
        if not regions:
            last_ja = ja
            time.sleep(args.interval)
            continue

        n += 1
        dead_advances = 0    # got a new line — not stalled
        # translate region-by-region like process_frame
        pairs = []
        t_tr = time.time()
        try:
            for r in regions:
                pairs.append((r, translator.translate(r.text)))
        except Exception as exc:
            log(fh, f"TRANSLATE ERROR: {exc}\n{traceback.format_exc()}")
            last_ja = ja
            continue
        tr_ms = (time.time() - t_tr) * 1000
        last_ja = ja

        # Save crops of the detected TEXT-region boxes only (not the full scene),
        # so a garbled OCR can be checked against the actual pixels without dumping
        # the game's artwork. Raw per-region text+boxes go to a sidecar file.
        rgb = img.convert("RGB")
        for i, r in enumerate(regions):
            if not r.box:
                continue
            x0, y0, x1, y1 = r.box
            pad = 4
            crop = rgb.crop((max(0, x0 - pad), max(0, y0 - pad),
                             min(rgb.width, x1 + pad), min(rgb.height, y1 + pad)))
            try:
                crop.save(out / f"crop_{n:03d}_{i}.png")
            except Exception:
                pass
        rf = io.open(out / f"regions_{n:03d}.txt", "w", encoding="utf-8")
        for i, r in enumerate(result.regions):
            rf.write(f"[{i}] box={r.box} {r.text!r}\n")
        rf.close()

        log(fh)
        log(fh, f"### line {n}   (ocr {ocr_ms:.0f}ms, translate {tr_ms:.0f}ms, "
                f"{len(result.regions)} raw regions -> {len(regions)} groups)")
        for r, en in pairs:
            log(fh, f"  JA: {r.text!r}")
            log(fh, f"  EN: {en!r}")
            log(fh, f"  box: {r.box}")
        elapsed = time.time() - tick
        if elapsed < args.interval:
            time.sleep(args.interval - elapsed)

        if args.max_lines and n >= args.max_lines:
            log(fh, f"reached --max-lines {args.max_lines}")
            break

    log(fh, "=" * 60)
    log(fh, f"done — {n} distinct lines captured")
    fh.close()
    print(f"done - {n} lines; see {out / 'pipeline_log.txt'}")


if __name__ == "__main__":
    main()
