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
import config
import decks
import hotkey
import ocr
import translate
from translate import furigana

# --- Bundled models (Steam / offline build) ----------------------------------
# When a `models/` folder ships next to the app (as in the Steam depot), use it
# as the HuggingFace cache and run fully offline, so no weights are ever fetched
# at runtime. A dev checkout has no such folder, so the normal first-run HF
# download still works there. Must run before huggingface_hub is imported (it is
# imported lazily inside the engines, so setting it here at import time is early
# enough).
_MODELS_DIR = Path(__file__).parent / "models"
if _MODELS_DIR.is_dir():
    os.environ.setdefault("HF_HOME", str(_MODELS_DIR))
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

UI_DIR = Path(__file__).parent / "ui"
INDEX_HTML = UI_DIR / "index.html"


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
        # Un-minimize the target window (without activating it) so it renders.
        capture.show_window(int(hwnd))
        with self._lock:
            self._capturing = True
            # Start always shows the overlay again, even if it was hidden via the
            # hotkey before the previous Stop.
            self._overlay_enabled = True
        return {"ok": True}

    def stop_capture(self) -> dict:
        # Mark stopped AND hide under the SAME lock a drawing frame uses. That
        # serializes the two: a slow in-flight frame either draws entirely before
        # this (then this hide wins, last) or sees _capturing False and skips —
        # it can never overwrite this hide (the "overlay still showing" on Stop).
        with self._lock:
            self._capturing = False
            self._hide_overlay_locked()
        capture.stop_capture_session()  # release the WGC capture
        return {"ok": True}


    # -- OCR stage ------------------------------------------------------------
    def set_ocr_engine(self, name: str) -> dict:
        """Select an OCR engine and load its weights (stage restart, not app)."""
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
        """Grab the current frame at card resolution (~960px) for a saved card.

        Reuses the running capture session's latest frame (no extra window grab),
        so it only works while capturing. Returns {ok, frame} or an error.
        """
        with self._lock:
            capturing = self._capturing
        if not capturing:
            return {"ok": False, "error": "not capturing"}
        img = capture.current_frame_image()
        if img is None:
            return {"ok": False, "error": "no frame available yet"}
        return {"ok": True, "frame": capture.to_data_url(img, max_width=self.CARD_IMAGE_WIDTH)}

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
        if not enabled:
            self._hide_overlay()  # showing again happens on the next frame

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
        if engine is None:
            return {"ok": False, "error": "OCR engine not ready"}

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

        # This one capture feeds both the preview thumbnail and OCR, so the UI
        # doesn't have to grab the window a second time (halving PrintWindow's
        # disturbance of the source game).
        frame = capture.to_data_url(img)

        try:
            result = engine.recognize(img)
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": "", "en": "", "error": f"OCR failed: {exc}"}

        ja = result.text
        if not ja.strip():
            self._hide_overlay()
            return {"ok": True, "frame": frame, "ja": "", "en": ""}

        if translator is None:
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "note": "translator loading"}

        # Group wrapped line-regions into text blocks first, then translate each
        # block as a whole. MeikiOCR emits one region per physical line, so a
        # sentence that wraps ("本当にただの" / "学生?") would otherwise be
        # translated in fragments — mangled or half-dropped. Grouping keeps a
        # genuinely separate element (a name, a far menu button) its own block, so
        # the overlay still draws a box over each. (See ocr.group_lines.)
        regions = [r for r in ocr.group_lines(result.regions) if r.text.strip()]
        try:
            pairs = [(r, translator.translate(r.text)) for r in regions]
        except Exception as exc:
            return {"ok": True, "frame": frame, "ja": ja, "en": "", "error": f"translate failed: {exc}"}
        en = "\n".join(t for _, t in pairs if t.strip())

        # Remember this frame so a live style/offset change can redraw instantly,
        # then commit the draw — all under the lock. Stop hides under the same
        # lock, so a stale in-flight frame can't interleave with (and undo) it: if
        # Stop already ran, _capturing is False here and we bail without drawing.
        with self._lock:
            if not self._capturing:
                return {"ok": True, "frame": frame, "ja": ja, "en": en, "stopped": True}
            self._last_hwnd, self._last_pairs = int(hwnd), pairs
            if self._overlay_enabled:
                self._draw_overlay(int(hwnd), pairs, style, text_mode)
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
            if not (self._capturing and self._overlay_enabled
                    and self._last_hwnd and self._last_pairs):
                return
            hwnd, pairs = self._last_hwnd, self._last_pairs
            style = {k: self._settings[k] for k in config.STYLE_KEYS}
            text_mode = self._settings["text_mode"]
            self._draw_overlay(hwnd, pairs, style, text_mode)

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


def main() -> None:
    api = Api()
    window = webview.create_window(
        title="NihongoViewer",
        url=str(INDEX_HTML),
        js_api=api,
        width=1024,
        height=940,
        min_size=(680, 600),
    )
    # Hand the window to the API so the card-capture hotkey can poke the UI.
    api._window = window
    webview.start()
    api._hotkey.close()       # unregister the global hotkeys on exit
    api._card_hotkey.close()


if __name__ == "__main__":
    main()
