"""Post-OCR normalization: repair katakana <-> kanji lookalike confusions.

PP-OCR (like most OCR) occasionally emits a kanji where a visually identical
katakana belongs, or vice versa — the glyphs are indistinguishable in many game
fonts:

    エ<->工   カ<->力   ニ<->二   ハ<->八   ロ<->口   タ<->夕   オ<->才   ー<->一

These are single-glyph slips inside an otherwise-correct word (``エドワード`` read
as ``工ドワ一ド``; ``協力`` read as ``協カ``; ``二人`` read as ``ニ人``), and they
wreck the translation of that one word — usually a name or loanword. We repair
them from context: a confusable glyph is snapped to the script of its immediate
neighbours, but **only when the context is unambiguous**. If it sits between a
katakana and a kanji (e.g. the long vowel in ``ビー玉``) we leave it alone rather
than risk corrupting text that was already correct.

The maps are directional and keyed by exact codepoint, so a *correct* character is
never changed: kanji ``二`` next to a kanji stays ``二`` (the katakana->kanji map
has no entry for it), while katakana ``ニ`` next to a kanji becomes ``二``.

Small-vs-large kana (``キヤ`` vs ``キャ``) is deliberately NOT handled: it's
genuinely ambiguous (``キヤノン`` = Canon uses a large ``ヤ``), so guessing there
would break correct words.
"""
import re

# kanji glyph -> the katakana it is usually a misread of.
_KANJI2KATA = {
    "工": "エ", "力": "カ", "二": "ニ", "八": "ハ",
    "口": "ロ", "夕": "タ", "才": "オ", "一": "ー",
}
# katakana glyph -> the kanji it is usually a misread of (the inverse).
_KATA2KANJI = {kata: kanji for kanji, kata in _KANJI2KATA.items()}

# Every glyph that COULD be a misread either way — the ones we reason about.
_AMBIG = set(_KANJI2KATA) | set(_KATA2KANJI)

_KATAKANA = re.compile(r"[ァ-ヿ]")          # full-width katakana block
_KANJI = re.compile(r"[一-鿿㐀-䶿]")          # CJK unified ideographs (+ ext A)


def _vote(ch: str | None) -> str | None:
    """Which script an *unambiguous* neighbour votes for (None = no vote).

    Ambiguous glyphs, hiragana, punctuation, latin and string boundaries don't
    vote — only a glyph that is *definitely* katakana or *definitely* kanji does,
    so the decision rests on solid context.
    """
    if ch is None or ch in _AMBIG:
        return None
    if _KATAKANA.match(ch):
        return "kata"
    if _KANJI.match(ch):
        return "kanji"
    return None


def fix_lookalikes(text: str) -> str:
    """Snap misread lookalike glyphs to the script their context demands."""
    if not text or not any(c in _AMBIG for c in text):
        return text

    chars = list(text)
    n = len(chars)
    i = 0
    while i < n:
        if chars[i] not in _AMBIG:
            i += 1
            continue
        # Maximal run of consecutive ambiguous glyphs [i, j); its anchors are the
        # unambiguous (or boundary) chars on either side. Runs are usually 1-2
        # chars, so a single script decision for the whole run is safe.
        j = i
        while j < n and chars[j] in _AMBIG:
            j += 1
        votes = {_vote(chars[i - 1] if i > 0 else None),
                 _vote(chars[j] if j < n else None)}
        if "kata" in votes and "kanji" not in votes:
            for k in range(i, j):
                chars[k] = _KANJI2KATA.get(chars[k], chars[k])
        elif "kanji" in votes and "kata" not in votes:
            for k in range(i, j):
                chars[k] = _KATA2KANJI.get(chars[k], chars[k])
        # else: conflicting (kata on one side, kanji on the other) or no vote at
        # all — leave the run untouched rather than guess.
        i = j
    return "".join(chars)
