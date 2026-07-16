"""Export a deck to an Anki Deck Package (.apkg) — dependency-free.

Each NihongoViewer card becomes an Anki note with a Word / Translated word /
Sentence / Translated sentence / Note / Image field. The card image (a base64
data URL) is written out as a media file and referenced from the Image field.

This builder uses **only the Python standard library** (``sqlite3`` + ``zipfile``
+ ``json``) — no third-party package — so the Anki-export feature carries no
external license obligations and is safe to compile into a commercial build.

An ``.apkg`` is just a ZIP archive holding:
  * ``collection.anki2`` — a SQLite database (Anki schema version 11),
  * ``media``            — a JSON map ``{"0": "original-filename.jpg", ...}``,
  * ``0``, ``1``, …      — the media files themselves, named by their map index.
We reproduce that documented format directly. (A file format / database schema
is not copyrightable, so replicating it here is fine.)
"""

import base64
import hashlib
import html
import json
import re
import sqlite3
import time
import uuid
import zipfile
import zlib

# Fixed note-type (model) id: keep it constant across exports so Anki treats
# every NihongoViewer card as the same note type. Chosen once, at random.
MODEL_ID = 1972304913

# Order matters: field ordinals below (0..5) are referenced by the templates and
# by the required-fields cache (`req`).
FIELD_NAMES = ["Word", "WordTr", "Sentence", "SentenceTr", "Note", "Image"]

# Anki joins a note's fields in the `flds` column with this separator (0x1f).
_FS = "\x1f"

CSS = """
.card {
  font-family: "Noto Sans JP", "Meiryo", sans-serif;
  font-size: 20px;
  text-align: center;
  color: #111;
  background: #fff;
}
.image img { max-width: 100%; max-height: 320px; border-radius: 8px; }
.word { font-size: 28px; font-weight: 700; margin: 12px 0 4px; }
.sentence { margin: 8px 0; }
.word-tr { color: #2a8a4a; font-size: 22px; font-weight: 600; margin-top: 12px; }
.sentence-tr { margin: 8px 0; }
.note { color: #666; font-size: 16px; margin-top: 12px; }
hr#answer { margin: 16px 0; }
"""

# Word / Sentence hold Anki furigana notation (``漢字[かんじ]``), so the built-in
# ``{{furigana:…}}`` filter renders kana readings over the kanji on the card.
TEMPLATES = [{
    "name": "NihongoViewer",
    "qfmt": (
        '{{#Image}}<div class="image">{{Image}}</div>{{/Image}}'
        '{{#Word}}<div class="word">{{furigana:Word}}</div>{{/Word}}'
        '{{#Sentence}}<div class="sentence">{{furigana:Sentence}}</div>{{/Sentence}}'
    ),
    "afmt": (
        '{{FrontSide}}<hr id="answer">'
        '{{#WordTr}}<div class="word-tr">{{WordTr}}</div>{{/WordTr}}'
        '{{#SentenceTr}}<div class="sentence-tr">{{SentenceTr}}</div>{{/SentenceTr}}'
        '{{#Note}}<div class="note">{{Note}}</div>{{/Note}}'
    ),
}]

# Standard Anki LaTeX preamble/closer (unused by our cards, but part of the model).
_LATEX_PRE = (
    "\\documentclass[12pt]{article}\n"
    "\\special{papersize=3in,5in}\n"
    "\\usepackage[utf8]{inputenc}\n"
    "\\usepackage{amssymb,amsmath}\n"
    "\\pagestyle{empty}\n"
    "\\setlength{\\parindent}{0in}\n"
    "\\begin{document}\n"
)
_LATEX_POST = "\\end{document}"

# Anki collection schema, version 11 (the format .apkg import expects). Empty
# `revlog`/`graves` tables and the indexes must exist for a clean import.
_SCHEMA = """
CREATE TABLE col (
    id integer primary key, crt integer not null, mod integer not null,
    scm integer not null, ver integer not null, dty integer not null,
    usn integer not null, ls integer not null, conf text not null,
    models text not null, decks text not null, dconf text not null, tags text not null
);
CREATE TABLE notes (
    id integer primary key, guid text not null, mid integer not null,
    mod integer not null, usn integer not null, tags text not null,
    flds text not null, sfld integer not null, csum integer not null,
    flags integer not null, data text not null
);
CREATE TABLE cards (
    id integer primary key, nid integer not null, did integer not null,
    ord integer not null, mod integer not null, usn integer not null,
    type integer not null, queue integer not null, due integer not null,
    ivl integer not null, factor integer not null, reps integer not null,
    lapses integer not null, left integer not null, odue integer not null,
    odid integer not null, flags integer not null, data text not null
);
CREATE TABLE revlog (
    id integer primary key, cid integer not null, usn integer not null,
    ease integer not null, ivl integer not null, lastIvl integer not null,
    factor integer not null, time integer not null, type integer not null
);
CREATE TABLE graves (usn integer not null, oid integer not null, type integer not null);
CREATE INDEX ix_notes_usn on notes (usn);
CREATE INDEX ix_cards_usn on cards (usn);
CREATE INDEX ix_revlog_usn on revlog (usn);
CREATE INDEX ix_cards_nid on cards (nid);
CREATE INDEX ix_cards_sched on cards (did, queue, due);
CREATE INDEX ix_revlog_cid on revlog (cid);
CREATE INDEX ix_notes_csum on notes (csum);
"""

_TAG_RE = re.compile(r"<[^>]+>")


def is_available() -> bool:
    """True if export can run.

    The builder is standard-library only, so this is always True. Kept so callers
    that gate the export on it keep working unchanged.
    """
    return True


def _field_html(text: str) -> str:
    """Escape user text for an Anki field, preserving line breaks."""
    return html.escape(text or "").replace("\n", "<br>")


def _ruby_field(text: str) -> str:
    """A JA field (Word/Sentence) as furigana bracket notation, HTML-escaped.

    Converts 天気 -> ``天気[てんき]`` so the ``{{furigana:…}}`` template filter can
    render the reading. Best-effort: if the analyzer is unavailable the plain
    text is used (no readings), so export never fails on a missing dependency.
    """
    try:
        from translate import furigana

        text = furigana.to_anki(text or "")
    except Exception:
        pass
    return _field_html(text)


def _strip_html(text: str) -> str:
    """Plain text with tags removed — used for the sort field and checksum."""
    return _TAG_RE.sub("", text or "")


def _csum(first_field: str) -> int:
    """Anki note checksum: sha1 of the stripped first field, first 8 hex as int."""
    stripped = _strip_html(first_field)
    return int(hashlib.sha1(stripped.encode("utf-8")).hexdigest()[:8], 16)


def _guid(key: str) -> str:
    """Deterministic note guid, stable across re-exports.

    Anki identifies a note across imports by its guid, so keying it on the card
    id means re-importing updates the same note instead of duplicating it.
    """
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)[:10].decode("ascii")


def _deck_id(name: str) -> int:
    """Stable deck id from the name (so re-exports update one Anki deck).

    Kept > 1 to never collide with Anki's built-in Default deck (id 1).
    """
    did = zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF
    return did if did > 1 else 2


def _decode_image(data_url: str, card_id: str) -> tuple[str, bytes] | None:
    """Decode a base64 image data URL to ``(filename, bytes)`` or None."""
    match = re.match(r"data:image/(\w+);base64,(.*)", data_url, re.DOTALL)
    if not match:
        return None
    ext, b64 = match.group(1).lower(), match.group(2)
    if ext == "jpeg":
        ext = "jpg"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", card_id or "") or uuid.uuid4().hex[:8]
    try:
        data = base64.b64decode(b64)
    except (ValueError, TypeError):
        return None
    return f"nv-{safe_id}.{ext}", data


def _models_json(now: int) -> dict:
    return {
        str(MODEL_ID): {
            "id": MODEL_ID,
            "name": "NihongoViewer Card",
            "type": 0,
            "mod": now,
            "usn": -1,
            "sortf": 0,
            "did": 1,
            "tmpls": [
                {
                    "name": t["name"], "ord": i, "qfmt": t["qfmt"], "afmt": t["afmt"],
                    "bqfmt": "", "bafmt": "", "did": None, "bfont": "", "bsize": 0,
                }
                for i, t in enumerate(TEMPLATES)
            ],
            "flds": [
                {
                    "name": name, "ord": i, "sticky": False, "rtl": False,
                    "font": "Arial", "size": 20, "media": [],
                }
                for i, name in enumerate(FIELD_NAMES)
            ],
            "css": CSS,
            "latexPre": _LATEX_PRE,
            "latexPost": _LATEX_POST,
            "latexsvg": False,
            # A card is generated when any of the fields the front references
            # (Word=0, Sentence=2, Image=5) is non-empty.
            "req": [[0, "any", [0, 2, 5]]],
            "tags": [],
            "vers": [],
        }
    }


def _decks_json(deck_id: int, deck_name: str, now: int) -> dict:
    default = {
        "id": 1, "name": "Default", "mod": now, "usn": -1,
        "lrnToday": [0, 0], "revToday": [0, 0], "newToday": [0, 0], "timeToday": [0, 0],
        "collapsed": False, "browserCollapsed": False, "desc": "", "dyn": 0,
        "conf": 1, "extendRev": 50, "extendNew": 10,
    }
    deck = dict(default, id=deck_id, name=deck_name)
    return {"1": default, str(deck_id): deck}


def _dconf_json(now: int) -> dict:
    return {
        "1": {
            "id": 1, "name": "Default", "mod": now, "usn": -1,
            "maxTaken": 60, "autoplay": True, "timer": 0, "replayq": True,
            "new": {
                "bury": True, "delays": [1, 10], "initialFactor": 2500,
                "ints": [1, 4, 7], "order": 1, "perDay": 20, "separate": True,
            },
            "rev": {
                "bury": True, "ease4": 1.3, "fuzz": 0.05, "ivlFct": 1,
                "maxIvl": 36500, "minSpace": 1, "perDay": 200, "hardFactor": 1.2,
            },
            "lapse": {
                "delays": [10], "leechAction": 0, "leechFails": 8,
                "minInt": 1, "mult": 0,
            },
            "dyn": False,
        }
    }


def _conf_json() -> dict:
    return {
        "nextPos": 1, "estTimes": True, "activeDecks": [1], "sortType": "noteFld",
        "timeLim": 0, "sortBackwards": False, "addToCur": True, "curDeck": 1,
        "newBury": True, "newSpread": 0, "dueCounts": True,
        "curModel": str(MODEL_ID), "collapseTime": 1200,
    }


def _build_collection(deck_name: str, cards: list[dict]) -> tuple[bytes, list[tuple[str, bytes]]]:
    """Build the ``collection.anki2`` SQLite bytes and the media list."""
    now = int(time.time())
    now_ms = int(time.time() * 1000)
    deck_id = _deck_id(deck_name)

    media: list[tuple[str, bytes]] = []
    note_rows: list[tuple] = []
    card_rows: list[tuple] = []

    for idx, card in enumerate(cards):
        image_html = ""
        data_url = card.get("image") or ""
        if isinstance(data_url, str) and data_url.startswith("data:"):
            decoded = _decode_image(data_url, card.get("id", ""))
            if decoded:
                filename, data = decoded
                media.append((filename, data))
                image_html = f'<img src="{filename}">'

        fields = [
            _ruby_field(card.get("word", "")),
            _field_html(card.get("word_tr", "")),
            _ruby_field(card.get("sentence", "")),
            _field_html(card.get("sentence_tr", "")),
            _field_html(card.get("note", "")),
            image_html,  # already safe HTML (an <img> tag or empty)
        ]
        guid = _guid(card.get("id") or f"{card.get('word', '')}|{card.get('sentence', '')}")

        # Unique, ascending ids within each table (ms epoch + row index).
        row_id = now_ms + idx
        note_rows.append((
            row_id, guid, MODEL_ID, now, -1, "",
            _FS.join(fields), _strip_html(fields[0]), _csum(fields[0]), 0, "",
        ))
        card_rows.append((
            row_id, row_id, deck_id, 0, now, -1,
            0, 0, idx, 0, 0, 0, 0, 0, 0, 0, 0, "",
        ))

    db = sqlite3.connect(":memory:")
    try:
        db.executescript(_SCHEMA)
        db.execute(
            "INSERT INTO col VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                1, now, now_ms, now_ms, 11, 0, 0, 0,
                json.dumps(_conf_json()),
                json.dumps(_models_json(now)),
                json.dumps(_decks_json(deck_id, deck_name, now)),
                json.dumps(_dconf_json(now)),
                "{}",
            ),
        )
        db.executemany("INSERT INTO notes VALUES (?,?,?,?,?,?,?,?,?,?,?)", note_rows)
        db.executemany(
            "INSERT INTO cards VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", card_rows
        )
        db.commit()
        return db.serialize(), media
    finally:
        db.close()


def export_deck(deck_name: str, cards: list[dict], out_path: str) -> int:
    """Write `cards` to `out_path` as an .apkg. Returns the number of cards written."""
    db_bytes, media = _build_collection(deck_name, cards)

    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("collection.anki2", db_bytes)
        media_map: dict[str, str] = {}
        for i, (filename, data) in enumerate(media):
            zf.writestr(str(i), data)
            media_map[str(i)] = filename
        zf.writestr("media", json.dumps(media_map))

    return len(cards)
