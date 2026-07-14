"""Export a deck to an Anki Deck Package (.apkg) with `genanki`.

Each NihongoViewer card becomes an Anki note with a Word / Translated word /
Sentence / Translated sentence / Note / Image field. The card image (a base64
data URL) is written out as a media file and referenced from the Image field.

`genanki` is a hard requirement of this module but imported lazily/guarded so the
rest of the app still runs if it's somehow missing — `is_available()` lets the
caller surface a friendly "install genanki" message instead of crashing.
"""

import base64
import html
import os
import re
import tempfile
import uuid
import zlib

try:
    import genanki
except ImportError:  # keep the app usable; the UI reports this to the user
    genanki = None

# A fixed model id (genanki guidance: keep it constant across exports so Anki
# treats every NihongoViewer card as the same note type). Chosen once, at random.
MODEL_ID = 1972304913

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


def is_available() -> bool:
    """True if genanki is importable (export can run)."""
    return genanki is not None


def _model():
    return genanki.Model(
        MODEL_ID,
        "NihongoViewer Card",
        fields=[
            {"name": "Word"},
            {"name": "WordTr"},
            {"name": "Sentence"},
            {"name": "SentenceTr"},
            {"name": "Note"},
            {"name": "Image"},
        ],
        templates=TEMPLATES,
        css=CSS,
    )


def _deck_id(name: str) -> int:
    """Stable positive deck id from the name, so re-exports update one Anki deck."""
    return (zlib.crc32(name.encode("utf-8")) & 0x7FFFFFFF) or 1


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


def _write_media(data_url: str, card_id: str, tmp_dir: str) -> str | None:
    """Decode a base64 image data URL to a file in `tmp_dir`; return its filename."""
    match = re.match(r"data:image/(\w+);base64,(.*)", data_url, re.DOTALL)
    if not match:
        return None
    ext, b64 = match.group(1).lower(), match.group(2)
    if ext == "jpeg":
        ext = "jpg"
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", card_id or "") or uuid.uuid4().hex[:8]
    filename = f"nv-{safe_id}.{ext}"
    try:
        with open(os.path.join(tmp_dir, filename), "wb") as fh:
            fh.write(base64.b64decode(b64))
    except (ValueError, OSError):
        return None
    return filename


def export_deck(deck_name: str, cards: list[dict], out_path: str) -> int:
    """Write `cards` to `out_path` as an .apkg. Returns the number of cards written.

    Raises RuntimeError if genanki isn't installed.
    """
    if genanki is None:
        raise RuntimeError("The 'genanki' package is required — install it with: pip install genanki")

    model = _model()
    deck = genanki.Deck(_deck_id(deck_name), deck_name)

    # Media files live in a temp dir just long enough to be zipped into the .apkg.
    with tempfile.TemporaryDirectory(prefix="nihongoviewer-apkg-") as tmp_dir:
        media_files: list[str] = []
        for card in cards:
            image_html = ""
            data_url = card.get("image") or ""
            if isinstance(data_url, str) and data_url.startswith("data:"):
                filename = _write_media(data_url, card.get("id", ""), tmp_dir)
                if filename:
                    media_files.append(os.path.join(tmp_dir, filename))
                    image_html = f'<img src="{filename}">'

            note = genanki.Note(
                model=model,
                fields=[
                    _ruby_field(card.get("word", "")),
                    _field_html(card.get("word_tr", "")),
                    _ruby_field(card.get("sentence", "")),
                    _field_html(card.get("sentence_tr", "")),
                    _field_html(card.get("note", "")),
                    image_html,  # already safe HTML (an <img> tag or empty)
                ],
                # A stable guid keyed on the card id, so re-exporting updates the
                # same Anki note instead of creating a duplicate.
                guid=genanki.guid_for(card.get("id") or f"{card.get('word', '')}|{card.get('sentence', '')}"),
            )
            deck.add_note(note)

        package = genanki.Package(deck)
        package.media_files = media_files
        package.write_to_file(out_path)

    return len(cards)
