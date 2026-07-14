"""Fuzzy translation cache (CLAUDE.md stage-3 requirement).

OCR output jitters frame to frame — a single misread kana changes the string
even though the on-screen text is the same. Re-translating every frame is slow,
so this wraps a `Translator` and returns a cached translation when the incoming
source is identical *or near-identical* to something translated recently.

It is itself a `Translator`, so it stays swappable and transparent to the caller.
"""

from collections import OrderedDict
from difflib import SequenceMatcher

from .base import Translator


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
