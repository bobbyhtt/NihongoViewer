"""Generate furigana (kana readings placed over kanji) for card display.

A learner capturing a card can read the English, but the raw Japanese Word /
Sentence is unreadable if they don't know the kanji. This module turns a JA
string into per-kanji readings so the UI can show ruby text (振り仮名).

It reuses the fugashi/UniDic morphological analyzer already installed for name
protection (`names.py`): every token carries a kana reading, so we can place a
hiragana reading over each kanji run. Three consumers share one generator:

  * `annotate(text)` -> segment list the UI renders as HTML ``<ruby>`` (WebView2
    supports it natively);
  * `reading_text(text)` -> the full hiragana reading (stored on a card so it's
    searchable, e.g. type ``てんき`` to find 天気);
  * `to_anki(text)` -> Anki ``漢字[かんじ]`` bracket notation for the exported
    ``{{furigana:…}}`` fields.

Everything is best-effort and guarded, exactly like name protection: if
fugashi/UniDic or jaconv aren't installed the text comes back as a single plain
segment, so cards still work — just without readings.
"""

import re

from . import names  # shares the fugashi.Tagger singleton via names.get_tagger()

# Kanji, including the iteration/repetition marks (々 〆 〇 ヶ). Katakana is
# already phonetic, so furigana is only placed over kanji.
_KANJI = r"[一-鿿々〆〇ヶ]"
_HAS_KANJI = re.compile(_KANJI)


def _to_hira(kana: str) -> str:
    """Katakana -> hiragana (best-effort; passes through if jaconv is absent)."""
    try:
        import jaconv
    except Exception:
        return kana
    return jaconv.kata2hira(kana)


def _token_reading(tok) -> str | None:
    """The token's surface reading as hiragana, or None if UniDic has none.

    UniDic tags the surface (inflected) reading under ``kana``; ``pron`` is a
    close fallback. Unknown words carry ``*`` / nothing — return None so the
    caller leaves the kanji bare rather than guessing.
    """
    feat = getattr(tok, "feature", None)
    if feat is None:
        return None
    kana = getattr(feat, "kana", None) or getattr(feat, "pron", None)
    if not kana or kana == "*":
        return None
    return _to_hira(kana)


def _fit(surface: str, reading: str) -> list[dict]:
    """Align `reading` (hiragana) to the kanji runs in `surface`.

    Returns segments: ``{"base": text}`` for kana/plain runs and
    ``{"base": kanji, "ruby": reading}`` for kanji runs, so okurigana stays out
    of the ruby (食べる -> 食(た) + べる, not 食べる(たべる)). Falls back to one
    ruby over the whole surface if the reading can't be aligned (irregular
    readings, 熟字訓 like 今日=きょう split oddly, etc.)."""
    # Group the surface into alternating kanji / non-kanji runs.
    groups: list[list] = []  # [is_kanji, text]
    for ch in surface:
        is_kanji = bool(_HAS_KANJI.match(ch))
        if groups and groups[-1][0] == is_kanji:
            groups[-1][1] += ch
        else:
            groups.append([is_kanji, ch])

    # Build a regex over the reading: kanji runs capture their reading; non-kanji
    # runs are literal anchors (compared as hiragana so kana line up).
    pattern = ""
    for is_kanji, text in groups:
        pattern += "(.+?)" if is_kanji else re.escape(_to_hira(text))
    match = re.fullmatch(pattern, reading)
    if match is None:
        return [{"base": surface, "ruby": reading}]

    out: list[dict] = []
    cap = 1
    for is_kanji, text in groups:
        if is_kanji:
            out.append({"base": text, "ruby": match.group(cap)})
            cap += 1
        else:
            out.append({"base": text})
    return out


def annotate(text: str) -> list[dict]:
    """Split `text` into ruby/plain segments for the UI.

    Each segment is ``{"base": str}`` (render as-is) or
    ``{"base": str, "ruby": str}`` (render as ``<ruby>base<rt>ruby</rt>``).
    Adjacent plain segments are merged. If the analyzer is unavailable the whole
    string comes back as one plain segment.
    """
    text = text or ""
    if not text.strip():
        return [{"base": text}] if text else []

    tagger = names.get_tagger()
    if tagger is None:
        return [{"base": text}]
    try:
        tokens = list(tagger(text))
    except Exception:
        return [{"base": text}]

    raw: list[dict] = []
    for tok in tokens:
        surface = tok.surface
        if not surface:
            continue
        if _HAS_KANJI.search(surface):
            reading = _token_reading(tok)
            if reading:
                raw.extend(_fit(surface, reading))
                continue
        raw.append({"base": surface})

    # Merge neighbouring plain runs so consumers get tidy output.
    merged: list[dict] = []
    for seg in raw:
        if "ruby" not in seg and merged and "ruby" not in merged[-1]:
            merged[-1] = {"base": merged[-1]["base"] + seg["base"]}
        else:
            merged.append(seg)
    return merged or [{"base": text}]


def reading_text(text: str) -> str:
    """Full hiragana reading of `text` (kanji replaced by their kana reading).

    Used to store a searchable reading on a card. Kana and non-Japanese pass
    through unchanged; returns "" for empty/all-symbol input.
    """
    parts = [seg.get("ruby") or seg["base"] for seg in annotate(text)]
    return "".join(parts).strip()


def to_anki(text: str) -> str:
    """Render `text` as Anki furigana notation: ``漢字[かんじ]``.

    A space is inserted before each bracketed group so Anki's ``{{furigana:}}``
    parser scopes the reading to just the kanji base (and not any preceding
    kana). Text with no kanji readings comes back unchanged.
    """
    parts: list[str] = []
    for seg in annotate(text):
        ruby = seg.get("ruby")
        if ruby:
            if parts and not parts[-1].endswith(" "):
                parts.append(" ")
            parts.append(f"{seg['base']}[{ruby}]")
        else:
            parts.append(seg["base"])
    return "".join(parts).lstrip()
