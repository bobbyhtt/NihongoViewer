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
"""

import re

# Any kana or kanji (incl. halfwidth katakana). Used to spot text that has
# nothing left to translate — e.g. a name already romanized to Latin.
_JAPANESE = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")


def has_japanese(text: str) -> bool:
    """True if `text` contains any Japanese script (kana or kanji)."""
    return bool(_JAPANESE.search(text))
