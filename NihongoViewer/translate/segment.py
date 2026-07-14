"""Split source text into translation-sized segments (one sentence each).

A neural MT model fed a long, multi-sentence line tends to **drop** a clause —
e.g. "……ずいぶんと余裕のある態度ね。本当にただの学生?" comes back as just
"You're really just a student?", losing the whole first sentence (a leading "……"
makes it worse). Translating one sentence at a time avoids that and gives each
sentence a cleaner, more faithful translation.

We split on Japanese and ASCII sentence terminators, keeping the terminator with
the sentence it ends, and also honour any newlines already in the text.
"""

import re

# Zero-width split *after* each sentence-ending mark, so the mark stays attached
# to its sentence. Covers Japanese 。！？ and their ASCII cousins . ! ? — the last
# only when doubled-up punctuation collapses, which strip() below tidies.
_SENTENCE_END = re.compile(r"(?<=[。．！？!?])")

# Any kana or kanji (incl. halfwidth katakana). Used to spot segments that have
# nothing left to translate — e.g. a name already romanized to Latin.
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


def has_japanese(text: str) -> bool:
    """True if `text` contains any Japanese script (kana or kanji)."""
    return bool(_JAPANESE.search(text))


def segment(text: str) -> list[str]:
    """Return `text` as a list of trimmed sentence segments (empty parts dropped)."""
    segments: list[str] = []
    for line in (text or "").splitlines():
        for part in _SENTENCE_END.split(line):
            part = part.strip()
            if part:
                segments.append(part)
    return segments
