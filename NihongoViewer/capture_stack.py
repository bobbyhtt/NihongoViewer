"""Persistence for the Create-card capture stack.

The capture stack is the small ring of frames (+ their OCR/translation) the user
grabs with the global Create-card hotkey but hasn't turned into cards yet. It was
originally transient, but a user who snaps a batch of lines and closes the app
before writing the cards would lose all of them — bad UX. So it now persists, as
its own JSON next to ``settings.json`` / ``decks.json`` (same platform config
dir), and is restored on the next launch.

Shape on disk (oldest → newest, capped at ``MAX``)::

    [ {"frame": "data:image/png;base64,…", "ja": "…", "en": "…"}, … ]

Frames are stored inline as base64 data URLs, exactly like card images in
``decks.py`` — the stack is capped at ``MAX`` entries, so the file stays bounded.
Everything is best-effort and never raises: a missing/corrupt file just yields an
empty stack, and a failed write (read-only dir) is swallowed.
"""

import json
from pathlib import Path

import platformdirs

APP_NAME = "NihongoViewer"

#: Max entries kept — mirrors CAPTURE_STACK_MAX in the UI (cards.js).
MAX = 20

_DIR = Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
_PATH = _DIR / "capture_stack.json"


def _clean(entry) -> dict | None:
    """Coerce one on-disk/incoming entry to {frame, ja, en}, or None if unusable."""
    if not isinstance(entry, dict):
        return None
    frame = str(entry.get("frame", "") or "")
    if not frame:
        return None  # an entry with no image is useless in the stack
    return {"frame": frame,
            "ja": str(entry.get("ja", "") or ""),
            "en": str(entry.get("en", "") or "")}


def load() -> list[dict]:
    """Return the saved stack (oldest → newest), or [] if none/unreadable."""
    try:
        with _PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, list):
        return []
    cleaned = [c for c in (_clean(e) for e in data) if c is not None]
    return cleaned[-MAX:]  # keep the newest MAX if an older file had more


def save(stack) -> None:
    """Persist `stack` (a list of {frame, ja, en}) atomically; never raises."""
    items: list[dict] = []
    if isinstance(stack, list):
        items = [c for c in (_clean(e) for e in stack) if c is not None][-MAX:]
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(items, fh, ensure_ascii=False)
        tmp.replace(_PATH)  # atomic on the same filesystem
    except OSError:
        pass  # a read-only config dir shouldn't take the app down
