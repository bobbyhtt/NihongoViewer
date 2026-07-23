"""NihongoViewer - offline screen translation tool for Japanese games.

Entry point. Opens a native desktop window that renders the HTML control panel
and exposes an API to the UI. The full pipeline lives here:

    capture -> OCR -> translate -> in-place overlay

The UI drives it frame by frame via `process_frame`, which also returns the
detected/translated text for the debug Text panel.
"""

import os
import re
import threading
from pathlib import Path

import webview

import capture
import capture_stack
import config
import decks
import hotkey
import ocr
import translate
from translate import dictionary, furigana

# Both ML models are loaded from local directories bundled with the app — the
# MADLAD translation model from `models/madlad/` (see translate.madlad) and the
# OCR weights from `ocr/models/` — so nothing is fetched from Hugging Face at
# runtime (no huggingface_hub dependency). The only optional runtime download is
# the JMdict Read-Mode dictionary (~11 MB via stdlib urllib), and it's bundleable
# too (`dict/jmdict.sqlite`).

UI_DIR = Path(__file__).parent / "ui"
INDEX_HTML = UI_DIR / "index.html"
APP_ICON = Path(__file__).parent / "icon.ico"  # window / taskbar icon


def _safe_filename(name: str) -> str:
    """Sanitize a deck name into a safe default filename for the save dialog."""
    cleaned = re.sub(r'[<>:"/\\|?*]', "_", name or "").strip()
    return cleaned or "deck"


class Api:
    """Methods here are callable from the UI as `window.pywebview.api.<name>()`."""

    def __init__(self) -> None:
        # Pipeline stages. Guarded by a lock because pywebview dispatches each
        # JS call on its own thread, so a frame can race an engine/style swap.
        self._engine: ocr.OcrEngine | None = None
        self._engine_name: str | None = None
        self._translator: translate.Translator | None = None
        self._overlay = None  # created lazily on first draw (overlay.Overlay)
        # The pywebview window, set in main() once it exists. Used to push events
        # into the UI (e.g. the global "capture into Create-card stack" hotkey).
        self._window = None
        # All persisted settings (loaded from disk; see config.DEFAULTS).
        self._settings = config.load()
        # Overlay visibility is a transient runtime flag (not persisted): the
        # overlay starts visible each launch and the hotkey toggles it.
        self._overlay_enabled = True
        # True between Start and Stop. Checked before every overlay draw so a
        # slow in-flight frame can't redraw the overlay after Stop.
        self._capturing = False
        # Last frame's draw inputs, cached so a live settings change (offset,
        # color, …) can redraw the overlay at once instead of waiting for the
        # next capture tick. Cleared whenever there's nothing on screen.
        self._last_hwnd: int | None = None
        self._last_pairs: list | None = None
        # Frame-change detection state (see process_frame's tiered skipping):
        # signature of the last *processed* frame, and the last OCR'd text. Let a
        # static screen (or a cutscene with unchanged text) skip the expensive work.
        self._last_frame_sig = None
        self._last_ocr_text: str | None = None
        # Area-mode last draw (ja, en, area) for live-restyle redraw; None in screen
        # mode (which uses _last_pairs instead).
        self._last_area_draw = None
        self._lock = threading.Lock()

        # Global hide/show hotkey — works even when our window isn't focused.
        self._hotkey = hotkey.HotkeyManager(self._toggle_overlay)
        self._hotkey.set_hotkey(self._settings["hotkey"])

        # Second global hotkey: grab the current frame + its translation into the
        # Create-card capture stack. Also works when our window isn't focused, so
        # the user can snap lines straight from the game. Each HotkeyManager runs
        # its own thread and registers hotkey id 1; ids are per-thread, so two
        # independent instances coexist fine (a same-combo clash is reported as
        # "in_use" by the OS, like any other unavailable combo).
        self._card_hotkey = hotkey.HotkeyManager(self._capture_card_hotkey)
        self._card_hotkey.set_hotkey(self._settings["card_hotkey"])

    # -- window capture -------------------------------------------------------
    def list_windows(self) -> list[dict]:
        return capture.list_windows()

    def start_capture(self, hwnd) -> dict:
        # Un-minimize the target window (without activating it) so it renders. Area
        # mode grabs the screen directly, so it may Start with no window selected —
        # release any window session left over from a previous screen-mode run.
        if hwnd:
            capture.show_window(int(hwnd))
        else:
            capture.stop_capture_session()
        with self._lock:
            self._capturing = True
            # Start always shows the overlay again, even if it was hidden via the
            # hotkey before the previous Stop.
            self._overlay_enabled = True
            # Fresh session: forget the last frame/text so the first frame is
            # always processed (never skipped as "unchanged" from a prior run).
            self._last_frame_sig = None
            self._last_ocr_text = None
        # Report the state so the UI's overlay toggle starts in sync ("Shown").
        return {"ok": True, "overlay_visible": True}

    def stop_capture(self) -> dict:
        # Mark stopped AND hide under the SAME lock a drawing frame uses. That
        # serializes the two: a slow in-flight frame either draws entirely before
        # this (then this hide wins, last) or sees _capturing False and skips —
        # it can never overwrite this hide (the "overlay still showing" on Stop).
        with self._lock:
            self._capturing = False
            self._hide_overlay_locked()
            self._last_frame_sig = None
            self._last_ocr_text = None
        capture.stop_capture_session()  # release the WGC capture
        return {"ok": True}

    def configure_area(self) -> dict:
        """Open the fullscreen editor to place the Area-mode detect/translate boxes.

        Draggable/resizable rectangles over the whole desktop (area mode grabs raw
        screen pixels, so no window is involved); blocks until the user saves
        (Enter) or cancels (Esc). Starts from the saved rectangles, or sensible
        defaults (detect = lower-middle dialogue band; translate = just below) the
        first time. Saves the result and switches to Area mode. Coordinates are
        absolute screen px.
        """
        with self._lock:
            detect = self._settings["detect_area"]
            trans = self._settings["translate_area"]
        if not detect or not trans:
            import win32api
            import win32con

            sw = win32api.GetSystemMetrics(win32con.SM_CXSCREEN)
            sh = win32api.GetSystemMetrics(win32con.SM_CYSCREEN)
            detect = detect or {"x": int(sw * 0.15), "y": int(sh * 0.60),
                                "w": int(sw * 0.70), "h": int(sh * 0.20)}
            trans = trans or {"x": int(sw * 0.15), "y": int(sh * 0.82),
                              "w": int(sw * 0.70), "h": int(sh * 0.14)}

        import area_editor  # imported lazily (Windows-only, native window)
        # Get our own window out of the way while the user drags the boxes, then
        # bring it back — whatever the outcome (save, cancel, or error).
        window = self._window
        if window is not None:
            try:
                window.minimize()
            except Exception:
                pass
        try:
            result = area_editor.edit(detect, trans)
        except Exception as exc:
            return {"ok": False, "error": f"Couldn't open the area editor: {exc}"}
        finally:
            if window is not None:
                try:
                    window.restore()
                except Exception:
                    pass
        if result is None:
            return {"ok": True, "cancelled": True}
        with self._lock:
            self._settings["capture_mode"] = "area"
            self._settings["detect_area"] = result["detect_area"]
            self._settings["translate_area"] = result["translate_area"]
            config.save(self._settings)
        return {"ok": True, **result}


    # -- OCR stage ------------------------------------------------------------
    def set_ocr_engine(self, name: str) -> dict:
        """Select an OCR engine and load its weights (stage restart, not app)."""
        # A saved config can name an engine that no longer exists (e.g. the removed
        # MeikiOCR). Fall back to the default so the stage self-heals instead of
        # showing "unavailable"; the corrected name is persisted + returned below.
        if name not in ocr.available_engines():
            name = ocr.DEFAULT_ENGINE
        with self._lock:
            speed = self._settings["ocr_speed"]
        try:
            engine = ocr.create_engine(name, speed)
            engine.load()
        except Exception as exc:
            return {"ok": False, "engine": name, "error": str(exc)}
        with self._lock:
            self._engine = engine
            self._engine_name = name
            self._settings["ocr_engine"] = name
            config.save(self._settings)
            text_mode = self._settings["text_mode"]
        return {"ok": True, "engine": name, "text_mode": text_mode}

    def set_ocr_speed(self, speed: str) -> dict:
        """Retune the OCR speed/quality tradeoff. Applied live — no weight reload."""
        if speed not in ocr.SPEED_PRESETS:
            return {"ok": False, "error": f"unknown speed {speed!r}"}
        with self._lock:
            self._settings["ocr_speed"] = speed
            config.save(self._settings)
            engine = self._engine
            # Retune under the lock so a concurrent frame can't read half-applied
            # knobs; apply_speed only flips attributes, so it's cheap to hold.
            if engine is not None:
                engine.apply_speed(speed)
        return {"ok": True, "speed": speed}

    # -- translation stage ----------------------------------------------------
    def load_translator(self) -> dict:
        """Load the (default) translation backend up front. Slow on first run."""
        try:
            translator = translate.create_translator()
            translator.load()
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        with self._lock:
            self._translator = translator
        return {"ok": True, "backend": translator.name}

    def translate_text(self, text: str) -> dict:
        """Translate one JA string to EN with MADLAD-400 (offline, fuzzy-cached).

        Used by the Create-card "Translate" buttons so a user who edits the
        captured word/sentence can re-translate it. Returns {ok, text} or an
        error (e.g. the translator hasn't finished loading yet).
        """
        text = (text or "").strip()
        if not text:
            return {"ok": True, "text": ""}
        with self._lock:
            translator = self._translator
        if translator is None:
            return {"ok": False, "error": "Translator still loading — try again in a moment."}
        try:
            out = translator.translate(text)
        except Exception as exc:
            return {"ok": False, "error": f"Translate failed: {exc}"}
        return {"ok": True, "text": out}

    def translate_word(self, text: str) -> dict:
        """Translate the Create-card Word field to a concise gloss.

        Same backend as `translate_text`, but via `Translator.translate_word`,
        which coaxes a short dictionary-style rendering instead of the padded
        sentence MADLAD emits for a bare word ("学生" -> "Students", not "Students
        are students."). Returns {ok, text} or an error.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": True, "text": ""}
        with self._lock:
            translator = self._translator
        if translator is None:
            return {"ok": False, "error": "Translator still loading — try again in a moment."}
        try:
            out = translator.translate_word(text)
        except Exception as exc:
            return {"ok": False, "error": f"Translate failed: {exc}"}
        return {"ok": True, "text": out}

    def furigana(self, text: str) -> dict:
        """Return kana-reading segments for a JA string (offline, no model).

        Used by the Create-card / My-card views to render ruby furigana over the
        Word and Sentence. ``segments`` is a list of ``{base}`` / ``{base, ruby}``
        pieces and ``reading`` is the full hiragana reading (for storage/search).
        Best-effort: if the analyzer is unavailable the whole string comes back
        as a single plain segment, so the caller just shows no readings.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": True, "segments": [], "reading": ""}
        try:
            segments = furigana.annotate(text)
            reading = furigana.reading_text(text)
        except Exception as exc:
            return {"ok": False, "error": f"Furigana failed: {exc}"}
        return {"ok": True, "segments": segments, "reading": reading}

    # -- Read Mode: per-word tokens + offline dictionary lookup ----------------
    def read_tokens(self, text: str) -> dict:
        """Split a JA string into per-word tokens for the Read Mode preview.

        Each token carries its ruby ``segments`` plus the dictionary ``query``
        (lemma), ``reading`` and ``pos`` the hover popup needs. Best-effort: an
        analyzer failure returns the string as one non-lookup token so the preview
        still renders. See ``translate.furigana.tokens``.
        """
        text = (text or "").strip()
        if not text:
            return {"ok": True, "tokens": []}
        try:
            toks = furigana.tokens(text)
        except Exception as exc:
            return {"ok": False, "error": f"Tokenize failed: {exc}"}
        return {"ok": True, "tokens": toks}

    def lookup(self, word: str, reading: str = "", pos: str = "") -> dict:
        """Offline JMdict lookup for a hovered word (see translate.dictionary).

        Returns ``{ok, entries, attribution}`` when ready, ``{ok: False,
        loading: True}`` while the index is still being built on first run, or
        ``{ok: False, error}`` if the build failed. Never blocks the UI: the
        index is built by a background thread started in ``main``.
        """
        word = (word or "").strip()
        if not word:
            return {"ok": True, "entries": []}
        state = dictionary.status()
        if state["state"] != "ready":
            if state["state"] == "error":
                return {"ok": False, "error": state["error"] or "Dictionary unavailable."}
            return {"ok": False, "loading": True}
        try:
            entries = dictionary.lookup(word, reading or None, pos or None)
        except Exception as exc:
            return {"ok": False, "error": f"Lookup failed: {exc}"}
        return {"ok": True, "entries": entries, "attribution": dictionary.ATTRIBUTION}

    def dict_status(self) -> dict:
        """Read Mode dictionary build/load state ({state, error}) for the UI."""
        return dictionary.status()

    # -- decks / cards (flashcard feature) ------------------------------------
    def list_decks(self) -> list[dict]:
        """Return the user's decks as [{name, count}] for the UI to list."""
        return decks.list_decks()

    def create_deck(self, name: str) -> dict:
        """Create a new empty deck; returns the refreshed deck list or an error."""
        return decks.add_deck(name)

    @staticmethod
    def _with_reading(card: dict) -> dict:
        """Attach the word's hiragana reading (for search) before storing a card."""
        card = dict(card or {})
        try:
            card["reading"] = furigana.reading_text(card.get("word", ""))
        except Exception:
            card["reading"] = ""  # best-effort — never block a save on the analyzer
        return card

    def save_card(self, deck: str, card: dict) -> dict:
        """Save a card to `deck`; returns the new card summary + refreshed counts."""
        return decks.add_card(deck, self._with_reading(card))

    def list_cards(self, deck: str) -> list[dict]:
        """Return a deck's cards as [{id, word, word_tr}] for the card list."""
        return decks.list_cards(deck)

    def get_card(self, deck: str, card_id: str) -> dict | None:
        """Return one full card (incl. image) for the detail view, or None."""
        return decks.get_card(deck, card_id)

    def update_card(self, deck: str, card_id: str, card: dict) -> dict:
        """Update an existing card; returns the refreshed summary or an error."""
        return decks.update_card(deck, card_id, self._with_reading(card))

    def delete_deck(self, name: str) -> dict:
        """Delete a deck (and its cards); returns the refreshed deck list."""
        return decks.delete_deck(name)

    def delete_card(self, deck: str, card_id: str) -> dict:
        """Delete a card from a deck; returns the refreshed deck list."""
        return decks.delete_card(deck, card_id)

    def export_deck(self, deck_name: str) -> dict:
        """Export a deck to an .apkg the user picks via a native Save dialog.

        Returns {ok, path, count} on success, {ok: False, cancelled: True} if the
        user closes the dialog, or {ok: False, error} on failure.
        """
        import anki_export

        if not anki_export.is_available():
            return {"ok": False, "error": "Anki export is unavailable in this build."}

        cards = decks.get_deck_cards(deck_name)
        if not cards:
            return {"ok": False, "error": "This deck has no cards to export."}

        window = webview.windows[0] if webview.windows else None
        if window is None:
            return {"ok": False, "error": "No window is available for the save dialog."}

        # Prefer the newer FileDialog enum; fall back to the (deprecated) constant.
        save_dialog = getattr(webview, "FileDialog", None)
        save_dialog = save_dialog.SAVE if save_dialog is not None else webview.SAVE_DIALOG
        result = window.create_file_dialog(
            save_dialog,
            save_filename=_safe_filename(deck_name) + ".apkg",
            file_types=("Anki Deck Package (*.apkg)", "All files (*.*)"),
        )
        if not result:
            return {"ok": False, "cancelled": True}

        # create_file_dialog returns a str (SAVE) or a sequence, depending on the
        # pywebview backend — normalize both.
        path = result if isinstance(result, str) else result[0]
        if not path.lower().endswith(".apkg"):
            path += ".apkg"

        try:
            count = anki_export.export_deck(deck_name, cards, path)
        except Exception as exc:
            return {"ok": False, "error": f"Export failed: {exc}"}
        return {"ok": True, "path": path, "count": count}

    def open_licenses(self) -> dict:
        """Open the bundled third-party licenses file with the OS default app.

        Looks next to the app first (where the file ships in a build), then falls
        back to the repo root during development. Returns {ok} or {ok, error}.
        """
        app_dir = Path(__file__).parent
        candidates = [
            app_dir / "THIRD_PARTY_LICENSES.md",
            app_dir / "THIRD-PARTY-NOTICES.txt",
            app_dir.parent / "THIRD_PARTY_LICENSES.md",
            app_dir.parent / "THIRD-PARTY-NOTICES.txt",
        ]
        for path in candidates:
            if path.exists():
                try:
                    os.startfile(str(path))  # Windows: open with the default handler
                except OSError as exc:
                    return {"ok": False, "error": f"Couldn't open the file: {exc}"}
                return {"ok": True, "path": str(path)}
        return {"ok": False, "error": "Licenses file was not found."}

    #: Max width of a card's captured image — larger than the ~640px preview so
    #: the expanded (lightbox) view is crisp, but still modest for storage.
    CARD_IMAGE_WIDTH = 960

    def capture_card_image(self) -> dict:
        """Grab the card image at card resolution (~960px) for a saved card.

        Screen mode reuses the running window session's latest frame. Area mode
        grabs the WHOLE screen (the monitor holding the detect box) so the card
        shows the full context — the card's *text* still comes from just the detect
        box (via the UI's last capture). Only works while capturing. {ok, frame}.
        """
        with self._lock:
            capturing = self._capturing
            mode = self._settings["capture_mode"]
            detect_area = self._settings["detect_area"]
        if not capturing:
            return {"ok": False, "error": "not capturing"}
        if mode == "area":
            around = None
            if detect_area:
                around = (detect_area["x"] + detect_area["w"] // 2,
                          detect_area["y"] + detect_area["h"] // 2)
            img = capture.grab_screen(around)
        else:
            img = capture.current_frame_image()
        if img is None:
            return {"ok": False, "error": "no frame available yet"}
        return {"ok": True, "frame": capture.to_data_url(img, max_width=self.CARD_IMAGE_WIDTH)}

    # -- capture stack persistence --------------------------------------------
    def load_capture_stack(self) -> list[dict]:
        """Return the persisted Create-card capture stack (oldest → newest).

        Restored by the UI on launch so a user who snapped a batch of frames and
        closed the app before writing the cards doesn't lose them. See
        ``capture_stack``.
        """
        return capture_stack.load()

    def save_capture_stack(self, stack: list) -> dict:
        """Persist the Create-card capture stack (the UI calls this on each change)."""
        capture_stack.save(stack)
        return {"ok": True}

    # -- settings / overlay stage ---------------------------------------------
    def get_settings(self) -> dict:
        """Return the current persisted settings so the UI can seed its controls."""
        with self._lock:
            return dict(self._settings)

    def update_settings(self, patch: dict) -> dict:
        """Merge `patch` into settings, apply live, and persist to disk.

        Called by the UI on every settings change. Only known keys are kept.
        Style changes take effect on the next drawn frame. A hotkey change is
        re-bound immediately, and its success is reported back under "hotkey" so
        the UI can tell the user if the combo was rejected (e.g. already in use).
        """
        patch = {k: v for k, v in (patch or {}).items() if k in config.DEFAULTS}
        # The two global hotkeys are handled apart from the other settings: we must
        # not persist one until the OS actually accepts it, or a rejected combo
        # would be saved and leave the next launch with a dead hotkey.
        new_hotkey = patch.pop("hotkey", None)
        new_card_hotkey = patch.pop("card_hotkey", None)
        with self._lock:
            self._settings.update(patch)

        hotkey_result = self._rebind_hotkey(self._hotkey, "hotkey", new_hotkey)
        card_hotkey_result = self._rebind_hotkey(
            self._card_hotkey, "card_hotkey", new_card_hotkey)

        with self._lock:
            config.save(self._settings)

        # Apply overlay-affecting changes (offset, colors, opacity, font, mode)
        # to what's already on screen, without waiting for the next capture tick.
        if patch:
            self._refresh_overlay()

        result = {"ok": True}
        if hotkey_result is not None:
            result["hotkey"] = hotkey_result
        if card_hotkey_result is not None:
            result["card_hotkey"] = card_hotkey_result
        return result

    def _rebind_hotkey(self, manager, key: str, new_spec):
        """(Re)bind one global hotkey and persist it only if the OS accepts it.

        Returns the manager's result dict (so the UI can report a rejected combo),
        or None when there was nothing to change. Only rebinds when the spec
        actually differs — routine style saves resend the current combo unchanged,
        and rebinding blocks on the OS round-trip.
        """
        if new_spec is None:
            return None
        with self._lock:
            if new_spec == self._settings.get(key):
                return None
        result = manager.set_hotkey(new_spec)
        if result["ok"]:
            with self._lock:
                self._settings[key] = new_spec
        return result

    def _toggle_overlay(self) -> None:
        """Flip overlay visibility (global-hotkey callback; runs off-UI-thread).

        Only does anything while capturing — when no window is being captured
        there's no overlay to hide/show, so the hotkey is a deliberate no-op
        (and doesn't silently flip the flag for the next capture session).
        """
        with self._lock:
            if not self._capturing:
                return
            self._overlay_enabled = not self._overlay_enabled
            enabled = self._overlay_enabled
        self._apply_overlay_enabled(enabled)

    def set_overlay_visible(self, visible: bool) -> dict:
        """Show/hide the overlay from the SETTING-panel toggle (mirrors the hotkey).

        Only meaningful while capturing; returns the effective state so the toggle
        reflects reality (it stays off when nothing is being captured).
        """
        with self._lock:
            if not self._capturing:
                return {"ok": True, "visible": False, "capturing": False}
            self._overlay_enabled = bool(visible)
            enabled = self._overlay_enabled
        self._apply_overlay_enabled(enabled, notify_ui=False)
        return {"ok": True, "visible": enabled, "capturing": True}

    def _apply_overlay_enabled(self, enabled: bool, *, notify_ui: bool = True) -> None:
        """Realize a new overlay-visible state: redraw or hide, and sync the UI.

        `notify_ui` pokes the SETTING-panel toggle — wanted when the change came
        from the global hotkey (the UI didn't initiate it), skipped when the UI
        toggle itself called us (it already updates from the return value).
        """
        if enabled:
            self._refresh_overlay()  # show again immediately from the last frame
        else:
            self._hide_overlay()
        if notify_ui:
            self._notify_overlay_state(enabled)

    def _notify_overlay_state(self, enabled: bool) -> None:
        """Poke the UI so the overlay toggle reflects a hotkey-driven change."""
        with self._lock:
            window = self._window
        if window is None:
            return
        flag = "true" if enabled else "false"
        try:
            window.evaluate_js(
                "window.NihongoViewer && window.NihongoViewer.onOverlayToggled"
                f" && window.NihongoViewer.onOverlayToggled({flag})"
            )
        except Exception:
            pass  # a failed UI poke must not kill the hotkey thread

    def _capture_card_hotkey(self) -> None:
        """Push the current frame + translation into the Create-card stack.

        Global-hotkey callback (runs off the UI thread). We don't build the
        capture here — the UI already holds the latest frame/text from the
        capture loop — so we just poke the UI to snapshot it. A no-op when not
        capturing (nothing to grab). `evaluate_js` marshals to the UI thread, so
        it's safe to call from the hotkey thread.
        """
        with self._lock:
            if not self._capturing:
                return
            window = self._window
        if window is None:
            return
        try:
            window.evaluate_js(
                "window.NihongoViewer && window.NihongoViewer.onHotkeyCapture"
                " && window.NihongoViewer.onHotkeyCapture()"
            )
        except Exception:
            pass  # a failed UI poke must not kill the hotkey thread

    # -- the pipeline ---------------------------------------------------------
    def process_frame(self, hwnd) -> dict:
        """Capture -> OCR -> translate -> draw overlay. Returns {ja, en}."""
        with self._lock:
            engine = self._engine
            translator = self._translator
            style = {k: self._settings[k] for k in config.STYLE_KEYS}
            text_mode = self._settings["text_mode"]
            mode = self._settings["capture_mode"]
            detect_area = self._settings["detect_area"]
            translate_area = self._settings["translate_area"]
        if engine is None:
            return {"ok": False, "error": "OCR engine not ready"}

        # Area mode grabs raw screen pixels inside the detect box (no window needed)
        # and draws the combined translation in the translate box — a separate path.
        if mode == "area" and detect_area and translate_area:
            return self._process_area(engine, translator, style, text_mode,
                                      detect_area, translate_area)

        # Screen mode: capture the whole target window.
        img = capture.capture_window_image(int(hwnd))
        if img is None:
            # Distinguish a closed window (stop for good) from a transient miss.
            if not capture.window_exists(int(hwnd)):
                with self._lock:
                    self._capturing = False
                self._hide_overlay()
                capture.stop_capture_session()
                return {"ok": False, "closed": True,
                        "error": "The target window was closed"}
            return {"ok": False, "error": "Could not capture this window"}

        # Tier 0 — did anything change? A static screen (paused game, still dialogue,
        # menu) matches the last processed frame, so skip OCR AND translation and
        # leave the overlay as-is. A moving background (a cutscene) fails this on
        # purpose and falls to the Tier-2 text check.
        sig = capture.frame_signature(img)
        with self._lock:
            last_sig = self._last_frame_sig
        if capture.signatures_match(sig, last_sig):
            return {"ok": True, "unchanged": True}
        with self._lock:
            self._last_frame_sig = sig  # this frame becomes the new reference

        # This one capture feeds both the preview thumbnail and OCR, so the UI
        # doesn't have to grab the window a second time (halving PrintWindow's
        # disturbance of the source game).
        frame = capture.to_data_url(img)

        try:
            result = engine.recognize(img)
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": "", "en": "", "error": f"OCR failed: {exc}"}

        ja = result.text
        # Tier 2 — did the TEXT change? The frame moved (Tier 0 passed), but if the
        # OCR'd text is identical (a cutscene playing behind a steady subtitle) the
        # translation is unchanged too, so skip it and keep the overlay. Covers the
        # empty case as well: empty == empty means the screen is still blank.
        #
        # TODO(region-hashing): during a cutscene this still runs OCR (~200 ms) every
        # frame just to conclude the text is unchanged. A future tier could hash only
        # the previous frame's text-region boxes and skip OCR when they're steady,
        # forcing a full OCR every ~1-2 s to catch new text appearing elsewhere.
        # Deferred: OCR isn't the bottleneck (translation is, and it's cached here).
        with self._lock:
            if ja == self._last_ocr_text:
                return {"ok": True, "unchanged": True, "frame": frame}

        if not ja.strip():
            self._hide_overlay()
            with self._lock:
                self._last_ocr_text = ja
            return {"ok": True, "frame": frame, "ja": "", "en": ""}

        if translator is None:
            # Don't record the text — retry translating it once the model loads.
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "note": "translator loading"}

        # Group wrapped line-regions into text blocks first, then translate each
        # block as a whole. The OCR engine emits one region per physical line, so a
        # sentence that wraps ("本当にただの" / "学生?") would otherwise be
        # translated in fragments — mangled or half-dropped. Grouping keeps a
        # genuinely separate element (a name, a far menu button) its own block, so
        # the overlay still draws a box over each. (See ocr.group_lines.)
        regions = [r for r in ocr.group_lines(result.regions) if r.text.strip()]
        if not regions:  # OCR text but nothing boxed to draw — clear the overlay
            self._hide_overlay()
            with self._lock:
                self._last_ocr_text = ja
            return {"ok": True, "frame": frame, "ja": ja, "en": ""}

        # Translate region-by-region and redraw after EACH, so a text-heavy screen
        # fills in progressively (the first line appears in ~1 s) instead of leaving
        # a stale overlay until the whole screen finishes. Each region's own
        # sentences are still batched inside translate(). Each redraw re-checks
        # _capturing under the lock, so a Stop mid-screen halts cleanly.
        pairs: list = []
        try:
            for r in regions:
                pairs.append((r, translator.translate(r.text)))
                with self._lock:
                    if not self._capturing:
                        return {"ok": True, "frame": frame, "ja": ja, "en": "", "stopped": True}
                    self._last_hwnd, self._last_pairs = int(hwnd), list(pairs)
                    self._last_area_draw = None  # screen mode — not area mode
                    if self._overlay_enabled:
                        self._draw_overlay(int(hwnd), pairs, style, text_mode)
                    else:
                        self._hide_overlay_locked()
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "error": f"translate failed: {exc}"}
        en = "\n".join(t for _, t in pairs if t.strip())
        with self._lock:
            self._last_ocr_text = ja  # Tier-2 reference — full screen now drawn
        return {"ok": True, "frame": frame, "ja": ja, "en": en}

    def _process_area(self, engine, translator, style, text_mode,
                      detect_area, translate_area) -> dict:
        """Area mode: grab the detect box off the screen, translate it, draw in box.

        Same tiered skipping as screen mode (Tier 0 hash, Tier 2 text), but the
        source is a raw screen-region grab (no window) and the whole detect box is
        translated as one block into the fixed translate box at absolute coords.
        """
        img = capture.grab_region(detect_area["x"], detect_area["y"],
                                  detect_area["w"], detect_area["h"])
        if img is None:
            return {"ok": False, "error": "Couldn't grab the detect area — check its size."}

        sig = capture.frame_signature(img)  # Tier 0 — detect box unchanged?
        with self._lock:
            last_sig = self._last_frame_sig
        if capture.signatures_match(sig, last_sig):
            return {"ok": True, "unchanged": True}
        with self._lock:
            self._last_frame_sig = sig

        frame = capture.to_data_url(img)
        try:
            result = engine.recognize(img)
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": "", "en": "", "error": f"OCR failed: {exc}"}

        ja = result.text
        with self._lock:  # Tier 2 — text unchanged?
            if ja == self._last_ocr_text:
                return {"ok": True, "unchanged": True, "frame": frame}
        if not ja.strip():
            self._hide_overlay()
            with self._lock:
                self._last_ocr_text = ja
            return {"ok": True, "frame": frame, "ja": "", "en": ""}
        if translator is None:
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "note": "translator loading"}

        regions = [r for r in ocr.group_lines(result.regions) if r.text.strip()]
        if not regions:
            self._hide_overlay()
            with self._lock:
                self._last_ocr_text = ja
            return {"ok": True, "frame": frame, "ja": ja, "en": ""}
        combined = "\n".join(r.text for r in regions)
        try:
            en = translator.translate(combined)
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "error": f"translate failed: {exc}"}
        with self._lock:
            if not self._capturing:
                return {"ok": True, "frame": frame, "ja": ja, "en": en, "stopped": True}
            self._last_hwnd, self._last_pairs = None, None  # area mode: no window
            self._last_area_draw = (combined, en, translate_area)
            self._last_ocr_text = ja
            if self._overlay_enabled:
                self._draw_area_overlay(combined, en, translate_area, style, text_mode)
            else:
                self._hide_overlay_locked()
        return {"ok": True, "frame": frame, "ja": ja, "en": en}

    # -- overlay helpers ------------------------------------------------------
    def _ensure_overlay(self):
        if self._overlay is None:
            import overlay  # imported lazily (Windows-only, native window)

            self._overlay = overlay.Overlay()
        return self._overlay

    def _hide_overlay_locked(self) -> None:
        """Drop the cached frame and hide the overlay. Caller MUST hold self._lock.

        Kept lock-held so Stop can hide inside the very lock a drawing frame uses,
        preventing the overlay's async show/hide from interleaving.
        """
        self._last_pairs = None
        self._last_area_draw = None
        if self._overlay is not None:
            self._overlay.hide()

    def _hide_overlay(self) -> None:
        # Drop the cached frame too, so a live style change can't redraw a stale
        # overlay while nothing is on screen.
        with self._lock:
            self._hide_overlay_locked()

    def _refresh_overlay(self) -> None:
        """Redraw the overlay from the last frame with the current settings.

        Lets a live settings change (offset, color, opacity, …) take effect at
        once instead of on the next capture tick. No-op if nothing is showing.
        """
        with self._lock:
            # Draw under the lock (like process_frame / stop) so a concurrent Stop
            # can't be undone. No-op once capture stopped or nothing is on screen.
            if not (self._capturing and self._overlay_enabled):
                return
            style = {k: self._settings[k] for k in config.STYLE_KEYS}
            text_mode = self._settings["text_mode"]
            if self._last_pairs and self._last_hwnd:  # screen mode — in-place boxes
                self._draw_overlay(self._last_hwnd, self._last_pairs, style, text_mode)
            elif self._last_area_draw:  # area mode — one box in the translate area
                ja, en, area = self._last_area_draw
                self._draw_area_overlay(ja, en, area, style, text_mode)

    @staticmethod
    def _overlay_text(ja: str, en: str, text_mode: str) -> str:
        """Text drawn in an overlay box: EN alone, or JA over EN in Duo mode."""
        ja, en = ja.strip(), en.strip()
        if text_mode == "duo" and ja:
            return f"{ja}\n{en}"
        return en

    def _draw_overlay(self, hwnd: int, pairs: list, style: dict, text_mode: str) -> None:
        """Draw one overlay box per translated region, over its own location.

        Caller MUST hold self._lock (so the draw can't interleave with a Stop).
        """
        # WGC frames are the client area, so map boxes from the client origin.
        rect = capture.client_rect(hwnd)
        if rect is None:
            return
        left, top, win_w, win_h = rect
        # Y offset is inverted so positive moves the overlay UP (screen Y grows
        # downward, but "+ = up" is the more intuitive control).
        offx, offy = int(style.get("offset_x", 0)), -int(style.get("offset_y", 0))

        # Regions with a known box get an in-place box each, anchored at the JA
        # text's top-left but sized to fit only the translated text (no need to
        # blanket the original Japanese).
        items = []
        for region, en in pairs:
            if not en.strip():
                continue
            if region.box:
                x0, y0, x1, y1 = region.box
                items.append({
                    "text": self._overlay_text(region.text, en, text_mode),
                    "x": left + x0 + offx,
                    "y": top + y0 + offy,
                    # Keep the user's font size: the overlay wraps the translation
                    # and grows its box right/down to fit (see render_text_image).
                    # "box" is the minimum (covers the original text); "max_w" is
                    # how far it may stretch right — the room from this box's left
                    # to the window's right edge — so a long line wraps before it
                    # runs past the window instead of shrinking the text.
                    "box": (x1 - x0, y1 - y0),
                    "max_w": max(int(win_w - (x0 + offx) - 8), x1 - x0),
                })

        # No boxes at all: one subtitle-style band lower-center of the window.
        if not items:
            lines = [self._overlay_text(r.text, en, text_mode)
                     for r, en in pairs if en.strip()]
            if not lines:
                self._hide_overlay_locked()  # caller holds the lock
                return
            items.append({
                "text": "\n".join(lines),
                "x": left + int(win_w * 0.12) + offx,
                "y": top + int(win_h * 0.78) + offy,
                "cover": None,
            })

        self._ensure_overlay().update(items, style)

    def _draw_area_overlay(self, ja: str, en: str, area: dict,
                           style: dict, text_mode: str) -> None:
        """Draw the combined translation into the user's fixed translate box.

        Area mode: one box at the `translate_area` rectangle (absolute screen px),
        wrapping the translation to that box's width. Caller MUST hold self._lock.
        """
        offx, offy = int(style.get("offset_x", 0)), -int(style.get("offset_y", 0))
        text = self._overlay_text(ja, en, text_mode)
        if not text.strip():
            self._hide_overlay_locked()
            return
        tx, ty, tw, th = int(area["x"]), int(area["y"]), int(area["w"]), int(area["h"])
        items = [{
            "text": text,
            "x": tx + offx,
            "y": ty + offy,
            # The translate box IS the width limit here (the user sized it), so wrap
            # to it and grow down within — never stretch past the box to the right.
            "box": (tw, th),
            "max_w": tw,
        }]
        self._ensure_overlay().update(items, style)


def main() -> None:
    api = Api()
    window = webview.create_window(
        title="Yakutori",
        url=str(INDEX_HTML),
        js_api=api,
        width=1024,
        height=940,
        min_size=(680, 600),
    )
    # Hand the window to the API so the card-capture hotkey can poke the UI.
    api._window = window
    # Build/open the Read Mode dictionary index up front on a background thread, so
    # the first word hover is instant instead of waiting on the one-time build.
    threading.Thread(target=dictionary.ensure, daemon=True).start()
    # Window / taskbar icon (best-effort — an older backend may ignore it).
    start_kwargs = {"icon": str(APP_ICON)} if APP_ICON.exists() else {}
    webview.start(**start_kwargs)
    api._hotkey.close()       # unregister the global hotkeys on exit
    api._card_hotkey.close()


if __name__ == "__main__":
    main()
