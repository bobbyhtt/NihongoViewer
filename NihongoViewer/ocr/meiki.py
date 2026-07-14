"""MeikiOCR engine — horizontal Japanese text, tuned for pixel/retro-game fonts.

ONNX Runtime based (no torch), so this is the light-weight default engine.
Weights download on first use into the standard Hugging Face cache.
"""

import re

import numpy as np
from PIL import Image

from .base import DEFAULT_SPEED, OcrEngine, OcrRegion, OcrResult

# A region is real Japanese text if it contains an actual kana syllable or a
# kanji. This drops standalone latin/number junk a screen grab picks up — menu
# buttons ("SAVE", "TITLE"), window title bars, "Screenshot2026..." — while
# keeping English/numbers that sit *inside* a Japanese line (the surrounding
# kana/kanji still qualify the region).
#
# The ranges deliberately EXCLUDE the katakana punctuation marks ・ (U+30FB) and
# ー (U+30FC): OCR sprinkles those into latin junk ("Screenshot2026・07",
# "O-SAVE" read as "O—SAVE"), so counting them as "Japanese" let noise through.
_JAPANESE = re.compile(
    "[ぁ-ゖ"   # hiragana syllables
    "ァ-ヺ"    # katakana syllables (no ・ ー or iteration marks)
    "ㇰ-ㇿ"    # katakana phonetic extensions
    "㐀-䶿"    # CJK extension A
    "一-鿿"    # CJK unified ideographs (kanji)
    "ｦ-ﾝ]"   # halfwidth katakana
)

# Drop low-confidence regions: faint/semi-transparent glyphs MeikiOCR misreads
# (e.g. a translucent menu button read as "O-LO9E") score poorly here.
_DEFAULT_MIN_CONFIDENCE = 0.5

# The speed/quality knob for MeikiOCR is the pre-OCR upscale factor: more pixels
# read small/low-contrast glyphs more accurately but cost more time (the frame is
# resized up, so 2.0x is ~4x the pixels of 1.0x). Each preset picks a factor.
_SPEED_UPSCALE = {"fast": 1.0, "balanced": 1.5, "accurate": 2.0}


class MeikiEngine(OcrEngine):
    name = "MeikiOCR"

    def __init__(self, min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
                 require_japanese: bool = True, speed: str = DEFAULT_SPEED,
                 max_dimension: int = 2600) -> None:
        self._ocr = None
        self._min_confidence = min_confidence
        self._require_japanese = require_japanese
        # Upscaling the frame before OCR gives the recognizer more pixels for
        # small features (e.g. the dakuten voicing mark ゛ that turns た->だ), so
        # low-contrast game text reads more accurately. Capped so we don't blow
        # up an already-large frame. Boxes are scaled back to source pixels. The
        # factor is set by the speed preset (see apply_speed).
        self._upscale = _SPEED_UPSCALE[DEFAULT_SPEED]
        self._max_dimension = max_dimension
        self.apply_speed(speed)

    def apply_speed(self, preset: str) -> None:
        # Only the upscale factor changes — no weight reload, so this is live.
        self._upscale = _SPEED_UPSCALE.get(preset, _SPEED_UPSCALE[DEFAULT_SPEED])

    def load(self) -> None:
        if self._ocr is None:
            # Imported lazily so simply importing this module doesn't pull in
            # onnxruntime / opencv until the engine is actually selected.
            from meikiocr import MeikiOCR

            self._ocr = MeikiOCR()

    def _scale_for(self, width: int, height: int) -> float:
        scale = self._upscale
        longest = max(width, height)
        if longest * scale > self._max_dimension:
            scale = max(1.0, self._max_dimension / longest)
        return scale

    def recognize(self, image: Image.Image) -> OcrResult:
        self.load()
        image = image.convert("RGB")
        scale = self._scale_for(image.width, image.height)
        src = image
        if scale != 1.0:
            src = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.BICUBIC,
            )
        # meikiocr expects an OpenCV-style BGR numpy array (its demo feeds the
        # result of cv2.imdecode). PIL gives us RGB, so flip the channel order.
        arr = np.asarray(src)[:, :, ::-1].copy()
        regions = []
        for line in self._ocr.run_ocr(arr):
            text = (line.get("text") or "").strip()
            if not text:
                continue
            if self._require_japanese and not _JAPANESE.search(text):
                continue
            if _mean_confidence(line) < self._min_confidence:
                continue
            regions.append(OcrRegion(text=text, box=_line_box(line, scale)))
        return OcrResult(regions=regions)


def _mean_confidence(line: dict) -> float:
    """Average per-character recognition confidence for a line (1.0 if unknown)."""
    confs = [c["conf"] for c in line.get("chars", []) if c.get("conf") is not None]
    return sum(confs) / len(confs) if confs else 1.0


def _line_box(line: dict, scale: float = 1.0):
    """Union of a line's per-char boxes -> (x0, y0, x1, y1) in source pixels.

    `scale` is the pre-OCR upscale factor; boxes are divided by it so they map
    back to the original (un-upscaled) image the caller positions against.
    """
    boxes = [c["bbox"] for c in line.get("chars", []) if c.get("bbox")]
    if not boxes:
        return None
    return (
        int(min(b[0] for b in boxes) / scale),
        int(min(b[1] for b in boxes) / scale),
        int(max(b[2] for b in boxes) / scale),
        int(max(b[3] for b in boxes) / scale),
    )
