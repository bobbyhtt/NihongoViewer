"""Settings persistence.

All user settings live in one JSON file in the platform-appropriate config
directory (via `platformdirs`, per CLAUDE.md) so they survive restarts. The set
of keys is fixed by `DEFAULTS`; unknown keys in a saved file are ignored and
missing keys fall back to their default, so an older/newer config never crashes
the app.

The file is the single source of truth for the *initial* value of every control
— the UI reads it on launch via `Api.get_settings()` and writes it back on every
change via `Api.update_settings()`.
"""

import json
from pathlib import Path

import platformdirs

APP_NAME = "NihongoViewer"

#: Every persisted setting and its default. Keep this in sync with the UI.
DEFAULTS: dict = {
    "ocr_engine": "RapidOCR",       # active OCR engine (see ocr.available_engines())
    "ocr_speed": "balanced",        # OCR speed/quality: "fast" | "balanced" | "accurate"
    "hotkey": "Alt+V",              # global hide/show overlay hotkey
    "card_hotkey": "Alt+C",         # global "capture into Create-card stack" hotkey
    "font": "Noto Sans JP",         # overlay font family (bundled, JP+Latin)
    "size": 18,                     # overlay font size (pt)
    "text_color": "#ffffff",        # overlay text color
    "bg_color": "#111111",          # overlay text-background color
    "opacity": 80,                  # background opacity 0-100
    "offset_x": 0,                  # overlay X offset (px)
    "offset_y": 0,                  # overlay Y offset (px)
    "text_mode": "single",          # "single" (EN only) | "duo" (JA over EN)
    "furigana_show": True,          # show kana readings over kanji in the card UI
    # Capture mode: "screen" reads the whole window (in-place overlay per region);
    # "area" reads only `detect_area` and shows the combined translation in
    # `translate_area`. Both rects are {x, y, w, h} in window CLIENT-area pixels
    # (so they follow the window), or null until configured.
    "capture_mode": "screen",       # "screen" | "area"
    "detect_area": None,            # {x,y,w,h} client-px region to OCR (area mode)
    "translate_area": None,         # {x,y,w,h} client-px box to draw into (area mode)
}
# Note: overlay visibility (the hide/show toggle) is intentionally NOT persisted —
# it's a transient runtime state, so the overlay starts visible every launch.

#: Style keys the overlay renderer consumes (a subset of DEFAULTS).
STYLE_KEYS = ("font", "size", "text_color", "bg_color", "opacity", "offset_x", "offset_y")

_CONFIG_DIR = Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
_CONFIG_PATH = _CONFIG_DIR / "settings.json"


def path() -> Path:
    """Absolute path of the settings file (may not exist yet)."""
    return _CONFIG_PATH


def load() -> dict:
    """Return the saved settings merged over the defaults (never raises)."""
    data = dict(DEFAULTS)
    try:
        with _CONFIG_PATH.open("r", encoding="utf-8") as fh:
            saved = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return data
    if isinstance(saved, dict):
        data.update({k: saved[k] for k in DEFAULTS if k in saved})
    return data


def save(settings: dict) -> None:
    """Persist the known keys of `settings` atomically (best-effort, never raises)."""
    data = {k: settings.get(k, DEFAULTS[k]) for k in DEFAULTS}
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2)
        tmp.replace(_CONFIG_PATH)  # atomic on the same filesystem
    except OSError:
        pass  # a read-only config dir shouldn't take the app down
