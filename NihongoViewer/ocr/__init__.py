"""OCR pipeline stage — selectable engines behind a common `OcrEngine` ABC.

The active engine is a user setting (see CLAUDE.md), created via `create_engine`.
Concrete engine classes are looked up lazily so importing this package never
imports torch / onnxruntime until an engine is actually instantiated.
"""

from .base import DEFAULT_SPEED, SPEED_PRESETS, OcrEngine, OcrRegion, OcrResult
from .group import group_lines

# name -> "module:ClassName". Kept as strings so `import ocr` stays cheap.
_ENGINES = {
    "MeikiOCR": ("meiki", "MeikiEngine"),
    "MangaOCR": ("manga", "MangaEngine"),
}

#: Engine selected when the app first starts (the light-weight, torch-free one).
DEFAULT_ENGINE = "MeikiOCR"


def available_engines() -> list[str]:
    """Names that can be passed to `create_engine`, in display order."""
    return list(_ENGINES)


def create_engine(name: str, speed: str = DEFAULT_SPEED) -> OcrEngine:
    """Instantiate the engine registered under `name` (weights load lazily).

    `speed` is one of `SPEED_PRESETS`, tuning the speed/quality tradeoff; it can
    be re-tuned later without a reload via `engine.apply_speed`.
    """
    if name not in _ENGINES:
        raise ValueError(f"Unknown OCR engine {name!r}; expected one of {available_engines()}")
    import importlib

    module_name, class_name = _ENGINES[name]
    module = importlib.import_module(f"{__name__}.{module_name}")
    return getattr(module, class_name)(speed=speed)


__all__ = [
    "OcrEngine",
    "OcrRegion",
    "OcrResult",
    "group_lines",
    "available_engines",
    "create_engine",
    "DEFAULT_ENGINE",
    "DEFAULT_SPEED",
    "SPEED_PRESETS",
]
