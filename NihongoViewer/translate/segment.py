"""Source-text test used before translation: does this still contain Japanese?

This module used to also split a line into one sentence per model call. That was
a workaround for MADLAD-400: fed a long multi-sentence line it would **drop** a
clause — "……ずいぶんと余裕のある態度ね。本当にただの学生?" came back as just
"You're really just a student?", losing the whole first sentence. Splitting first
avoided that, at the cost of denying the model any cross-sentence context.

Qwen3 handles a multi-sentence line directly and translates it *better* with the
surrounding sentences visible (pronouns and register carry across them), so the
splitting is gone and the whole line goes to the model as one unit. See
`translate.qwen`.

`split_sentences` still lives here, but as a shared utility rather than the old
pre-translate split: it is used by `translate.cache.SentenceCache` to translate
and cache an accumulating (NVL) screen one sentence at a time, and by
`translate.qwen`'s derail-recovery fallback. It splits only on 。！？ sentence
ends — never on the 、 clause comma — so a sentence's clauses still reach the
model together and the cross-clause context is kept.
"""

import re

# Any kana or kanji (incl. halfwidth katakana). Used to spot text that has
# nothing left to translate — e.g. a name already romanized to Latin.
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")

# Sentence boundaries: split *after* a Japanese/ASCII sentence-final mark, keeping
# the mark with its sentence so tone (…! …?) survives. A run of marks ("！？") stays
# with the sentence. The ellipsis "…"/"……" is deliberately NOT a boundary — it is
# mid-utterance, not a sentence end.
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])(?![。！？!?])")


def has_japanese(text: str) -> bool:
    """True if `text` contains any Japanese script (kana or kanji)."""
    return bool(_JAPANESE.search(text))


def split_sentences(text: str) -> list[str]:
    """Split a line into sentences on 。！？ boundaries (marks kept, blanks dropped).

    A line with no sentence-final mark returns as a single element, so the caller
    can treat "one sentence" and "unsplit line" uniformly.
    """
    return [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
