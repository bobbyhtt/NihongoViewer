"""Translation pipeline stage — backends behind a common `Translator` ABC.

`create_translator()` returns the configured backend wrapped in the caching
layers `SentenceCache(FuzzyCache(backend))` (the near-identical-source caching
CLAUDE.md requires, plus per-sentence reuse for accumulating NVL screens), so
callers get caching for free without knowing about it.
"""

from .base import Translator
from .cache import FuzzyCache, SentenceCache

# name -> "module:ClassName" (kept as strings so `import translate` stays cheap;
# ctranslate2 is only imported when a backend is actually created).
_BACKENDS = {
    "Qwen3-4B": ("qwen", "QwenTranslator"),  # only backend — Apache-2.0, torch-free
}

#: Translation backend used by default.
DEFAULT_BACKEND = "Qwen3-4B"


def available_backends() -> list[str]:
    return list(_BACKENDS)


def create_translator(name: str = DEFAULT_BACKEND, *, cache: bool = True) -> Translator:
    """Instantiate a translation backend (weights load lazily), fuzzy-cached."""
    if name not in _BACKENDS:
        raise ValueError(f"Unknown translator {name!r}; expected one of {available_backends()}")
    import importlib

    module_name, class_name = _BACKENDS[name]
    module = importlib.import_module(f"{__name__}.{module_name}")
    backend = getattr(module, class_name)()
    return SentenceCache(FuzzyCache(backend)) if cache else backend


__all__ = [
    "Translator",
    "FuzzyCache",
    "SentenceCache",
    "available_backends",
    "create_translator",
    "DEFAULT_BACKEND",
]
