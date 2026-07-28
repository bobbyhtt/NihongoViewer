"""Translation caching wrappers (CLAUDE.md stage-3 requirement).

Two `Translator` wrappers, each transparent and swappable:

  * `FuzzyCache` — OCR output jitters frame to frame (a single misread kana
    changes the string though the on-screen text is the same), so this returns a
    cached translation when the incoming source is identical *or near-identical*
    to something translated recently.
  * `SentenceCache` — translates a block one sentence at a time so an
    accumulating (NVL) narration screen reuses the earlier sentences instead of
    re-translating (and re-generating) the whole growing paragraph every frame.

Compose them as `SentenceCache(FuzzyCache(backend))`: the block is split into
sentences, and each sentence goes through the fuzzy cache — so the fuzzy match
(and OCR-jitter reuse) now operates at sentence granularity too.
"""

from collections import OrderedDict
from difflib import SequenceMatcher

from .base import Translator
from .segment import split_sentences


def _normalize(text: str) -> str:
    """Collapse whitespace so trivial spacing differences hit the exact cache."""
    return " ".join(text.split())


class FuzzyCache(Translator):
    def __init__(self, inner: Translator, threshold: float = 0.9, capacity: int = 256) -> None:
        self._inner = inner
        self._threshold = threshold
        self._capacity = capacity
        # normalized source -> translation, most-recently-used last.
        self._cache: "OrderedDict[str, str]" = OrderedDict()

    @property
    def name(self) -> str:
        return self._inner.name

    def load(self) -> None:
        self._inner.load()

    def translate(self, text: str) -> str:
        key = _normalize(text)
        if not key:
            return ""

        # 1) Exact (normalized) hit.
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]

        # 2) Fuzzy hit against recent sources (newest first — dialogue is local).
        for cached_key in reversed(self._cache):
            if SequenceMatcher(None, key, cached_key).ratio() >= self._threshold:
                return self._cache[cached_key]

        # 3) Miss — translate and remember.
        result = self._inner.translate(text)
        self._cache[key] = result
        self._cache.move_to_end(key)
        while len(self._cache) > self._capacity:
            self._cache.popitem(last=False)
        return result


class SentenceCache(Translator):
    """Translate a block one SENTENCE at a time, joining the results.

    VN narration (NVL) screens append a sentence per click, so each frame's source
    is "previous text + one more sentence". Translating the whole block every frame
    is doubly bad:

      * **latency** — generation is autoregressive, so re-emitting a long paragraph
        costs on the order of a *minute* on CPU (a 14-sentence block measured at
        ~114 s), and it is redone every single frame;
      * **dropped text** — the block-level fuzzy match sees "paragraph" vs
        "paragraph + 1 sentence" as ~identical and returns the shorter block's
        translation, silently losing the newly added sentence.

    Splitting on sentence ends fixes both: only the newly appended sentence misses
    the inner cache (every earlier one is a hit), and every sentence is present in
    the join, so nothing is dropped. Clauses inside a sentence are never split
    (we split on 。！？, never 、), so the cross-clause context whole-line
    translation was chosen for is preserved. A single-sentence line (ordinary ADV
    dialogue) splits to one piece and behaves exactly as the bare backend.
    """

    def __init__(self, inner: Translator) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    def load(self) -> None:
        self._inner.load()

    def translate(self, text: str) -> str:
        sentences = split_sentences(text)
        # 0 or 1 sentence: nothing to gain — delegate the whole text unchanged so
        # ADV dialogue is byte-for-byte what the bare backend would produce.
        if len(sentences) <= 1:
            return self._inner.translate(text)
        parts = (self._inner.translate(s) for s in sentences)
        return " ".join(p.strip() for p in parts if p and p.strip())
