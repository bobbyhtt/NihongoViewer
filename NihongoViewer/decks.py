"""Deck & card storage for the flashcard feature.

Decks and their cards persist as one JSON file next to ``settings.json`` (same
platform config dir), so the user's study content survives restarts. This is
deliberately separate from ``config.py``: cards are user *content*, not app
*configuration*, and they grow unbounded.

Shape on disk::

    {
      "decks": [
        {"name": "MyDeck", "cards": [ {card…}, … ]},
        …
      ]
    }

The default deck (``MyDeck``) is always guaranteed to exist so the Create-card
screen and the deck dropdown are never empty. Card creation/editing lands in a
later phase — for now this module manages decks and reports their card counts.
"""

import base64
import io
import json
import re
import time
import uuid
from pathlib import Path

import platformdirs

try:
    from PIL import Image
except ImportError:  # Pillow is a core dep; degrade to no thumbnails if absent
    Image = None

APP_NAME = "NihongoViewer"
DEFAULT_DECK = "MyDeck"

_DIR = Path(platformdirs.user_config_dir(APP_NAME, appauthor=False))
_PATH = _DIR / "decks.json"


def _load_raw() -> dict:
    """Return the on-disk store, normalized and with the default deck guaranteed."""
    try:
        with _PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        data = {}

    raw = data.get("decks") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        raw = []

    decks: list[dict] = []
    seen: set[str] = set()
    for d in raw:
        if not isinstance(d, dict):
            continue
        name = str(d.get("name", "")).strip()
        if not name or name.lower() in seen:
            continue
        cards = d.get("cards")
        if not isinstance(cards, list):
            cards = []
        decks.append({"name": name, "cards": cards})
        seen.add(name.lower())

    # The default deck always exists, and sits first.
    if DEFAULT_DECK.lower() not in seen:
        decks.insert(0, {"name": DEFAULT_DECK, "cards": []})

    return {"decks": decks}


def _save_raw(data: dict) -> None:
    """Persist the store atomically (best-effort; never raises)."""
    try:
        _DIR.mkdir(parents=True, exist_ok=True)
        tmp = _PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        tmp.replace(_PATH)  # atomic on the same filesystem
    except OSError:
        pass  # a read-only config dir shouldn't take the app down


def _thumb(data_url: str, box: int = 200) -> str | None:
    """Shrink a stored image data URL to a small JPEG thumbnail data URL.

    Returns None if there's no image, Pillow is missing, or decoding fails —
    callers treat a missing thumbnail as "no image" and fall back gracefully.
    """
    if Image is None or not isinstance(data_url, str):
        return None
    match = re.match(r"data:image/\w+;base64,(.*)", data_url, re.DOTALL)
    if not match:
        return None
    try:
        raw = base64.b64decode(match.group(1))
        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((box, box))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
    except Exception:
        return None
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _covers(cards: list[dict], limit: int = 3) -> list[str]:
    """Up to `limit` thumbnails from a deck's most recent cards that have images."""
    covers: list[str] = []
    for c in reversed(cards):  # newest first
        if not isinstance(c, dict):
            continue
        thumb = _thumb(c.get("image", ""), box=220)
        if thumb:
            covers.append(thumb)
        if len(covers) >= limit:
            break
    return covers


def _summaries(decks: list[dict]) -> list[dict]:
    """Reduce full decks to {name, count, covers} for the deck grid."""
    return [
        {"name": d["name"], "count": len(d["cards"]), "covers": _covers(d["cards"])}
        for d in decks
    ]


def list_decks() -> list[dict]:
    """Return decks as ``[{name, count}]`` (default deck first)."""
    return _summaries(_load_raw()["decks"])


def add_deck(name: str) -> dict:
    """Create a new (empty) deck.

    Returns ``{"ok": True, "decks": [...]}`` with the refreshed deck summaries on
    success, or ``{"ok": False, "error": "..."}`` if the name is blank or already
    taken (case-insensitive).
    """
    name = (name or "").strip()
    if not name:
        return {"ok": False, "error": "Deck name can't be empty."}

    data = _load_raw()
    if any(d["name"].lower() == name.lower() for d in data["decks"]):
        return {"ok": False, "error": f'A deck named "{name}" already exists.'}

    data["decks"].append({"name": name, "cards": []})
    _save_raw(data)
    return {"ok": True, "decks": _summaries(data["decks"])}


def delete_deck(name: str) -> dict:
    """Delete a deck and all its cards.

    The default deck can't be deleted (it's re-created on load anyway, so the
    Create-card screen is never left without a deck). Returns the refreshed deck
    summaries on success.
    """
    name = (name or "").strip()
    if name.lower() == DEFAULT_DECK.lower():
        return {"ok": False, "error": f'The default deck "{DEFAULT_DECK}" can\'t be deleted.'}

    data = _load_raw()
    before = len(data["decks"])
    data["decks"] = [d for d in data["decks"] if d["name"].lower() != name.lower()]
    if len(data["decks"]) == before:
        return {"ok": False, "error": f'Deck "{name}" not found.'}
    _save_raw(data)
    return {"ok": True, "decks": _summaries(data["decks"])}


# -- cards --------------------------------------------------------------------

#: Editable text/image fields of a card (id/created are assigned on save).
#: ``reading`` is the word's hiragana reading — filled in by the caller (main.py)
#: from the furigana analyzer so cards are searchable by reading.
CARD_FIELDS = ("word", "word_tr", "sentence", "sentence_tr", "note", "image", "reading")


def _find_deck(data: dict, name: str) -> dict | None:
    name = (name or "").strip().lower()
    return next((d for d in data["decks"] if d["name"].lower() == name), None)


def _clean_card(card: dict) -> dict:
    """Keep only known fields, coerced to strings (drops anything unexpected)."""
    src = card if isinstance(card, dict) else {}
    return {k: ("" if src.get(k) is None else str(src.get(k, ""))) for k in CARD_FIELDS}


def _card_summary(card: dict, with_thumb: bool = True) -> dict:
    """The card-list shape: text + a small thumbnail (not the full image)."""
    return {
        "id": card.get("id", ""),
        "word": card.get("word", ""),
        "word_tr": card.get("word_tr", ""),
        "reading": card.get("reading", ""),
        "sentence": card.get("sentence", ""),
        "thumb": _thumb(card.get("image", ""), box=120) if with_thumb else None,
    }


def add_card(deck_name: str, card: dict) -> dict:
    """Append a card to a deck.

    Returns ``{ok, card, decks}`` (the new card's summary + refreshed deck
    counts) on success, or ``{ok: False, error}`` if the deck is missing or the
    card has neither a word nor a sentence.
    """
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return {"ok": False, "error": f'Deck "{deck_name}" not found.'}

    entry = _clean_card(card)
    if not entry["word"].strip() and not entry["sentence"].strip():
        return {"ok": False, "error": "Add a word or sentence before saving."}

    entry["id"] = uuid.uuid4().hex[:12]
    entry["created"] = int(time.time())
    deck["cards"].append(entry)
    _save_raw(data)
    return {"ok": True, "card": _card_summary(entry), "decks": _summaries(data["decks"])}


def list_cards(deck_name: str) -> list[dict]:
    """Return a deck's cards as lightweight summaries ``[{id, word, word_tr}]``."""
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return []
    return [_card_summary(c) for c in deck["cards"] if isinstance(c, dict)]


def get_deck_cards(deck_name: str) -> list[dict]:
    """Return a deck's full cards (incl. image), e.g. for Anki export."""
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return []
    return [dict(c) for c in deck["cards"] if isinstance(c, dict)]


def get_card(deck_name: str, card_id: str) -> dict | None:
    """Return the full card (incl. image) by id, or None if not found."""
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return None
    for c in deck["cards"]:
        if isinstance(c, dict) and c.get("id") == card_id:
            return dict(c)
    return None


def update_card(deck_name: str, card_id: str, card: dict) -> dict:
    """Update an existing card's fields in place (keeps its id + created time).

    Returns ``{ok, card}`` (the refreshed summary) on success, or
    ``{ok: False, error}`` if the deck/card is missing or the card is empty.
    """
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return {"ok": False, "error": f'Deck "{deck_name}" not found.'}

    entry = _clean_card(card)
    if not entry["word"].strip() and not entry["sentence"].strip():
        return {"ok": False, "error": "Add a word or sentence before saving."}

    for c in deck["cards"]:
        if isinstance(c, dict) and c.get("id") == card_id:
            c.update(entry)  # id and created are untouched
            _save_raw(data)
            return {"ok": True, "card": _card_summary(c)}
    return {"ok": False, "error": "Card not found."}


def delete_card(deck_name: str, card_id: str) -> dict:
    """Delete a card from a deck. Returns refreshed deck summaries on success."""
    data = _load_raw()
    deck = _find_deck(data, deck_name)
    if deck is None:
        return {"ok": False, "error": f'Deck "{deck_name}" not found.'}
    before = len(deck["cards"])
    deck["cards"] = [
        c for c in deck["cards"] if not (isinstance(c, dict) and c.get("id") == card_id)
    ]
    if len(deck["cards"]) == before:
        return {"ok": False, "error": "Card not found."}
    _save_raw(data)
    return {"ok": True, "decks": _summaries(data["decks"])}
