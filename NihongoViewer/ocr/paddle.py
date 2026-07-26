"""RapidOCR engine — horizontal Japanese text via the RapidOCR runtime (ONNX).

The **only shipped OCR engine**, chosen because it is **Apache-2.0, torch-free and
paddle-free** end to end: it runs the PP-OCR detection + recognition models through
`rapidocr-onnxruntime` (pure `onnxruntime`, no PaddlePaddle at runtime), so both the
code *and* the model weights are commercial-clean. (It replaced MeikiOCR, whose
recognition/detection weights were LGPL-3.0 — see THIRD_PARTY_LICENSES.md.)

Pieces and their licenses (all Apache-2.0):
  * `rapidocr-onnxruntime` — the ONNX inference wrapper (DB detection + CRNN/CTC
    recognition). Installed ``--no-deps`` so it reuses the app's existing
    `opencv-python-headless` instead of pulling a second OpenCV build.
  * detection + angle-cls models — bundled inside the rapidocr wheel (PP-OCR v3).
  * recognition model — ``ocr/models/japan_ppocrv5_rec.onnx`` (bundled here). This
    is the PP-OCRv5 mobile recognizer (Simplified/Traditional Chinese + English +
    Japanese in one head), far more accurate than the older PP-OCR JA models,
    including rare kanji and katakana names. Its 18383-char dict is baked into the
    ONNX ``character`` metadata (RapidOCR reads it from there — no separate keys
    file). Its fixed input height is 48, so ``rec_img_shape=[3, 48, 320]``.

This returns one `OcrRegion` per detected line; `group.py` then merges lines into
sentence-blocks downstream — unchanged.
"""

import re
from pathlib import Path

import numpy as np
from PIL import Image

from .base import DEFAULT_SPEED, OcrEngine, OcrRegion, OcrResult
from .normalize import fix_lookalikes
from .spellfix import correct as spellfix_correct

# "Is this really Japanese?" gate — drop standalone latin/number junk (menu
# buttons, title bars) while keeping latin that sits *inside* a kana/kanji line.
# The ranges deliberately exclude the katakana marks ・ (U+30FB) and ー (U+30FC):
# OCR sprinkles those into latin junk ("Screenshot2026・07"), so counting them as
# "Japanese" would let noise through.
_JAPANESE = re.compile(
    "[ぁ-ゖ"   # hiragana
    "ァ-ヺ"    # katakana (no ・ ー / iteration marks)
    "ㇰ-ㇿ"    # katakana phonetic extensions
    "㐀-䶿"    # CJK extension A
    "一-鿿"    # CJK unified ideographs (kanji)
    "ｦ-ﾝ]"   # halfwidth katakana
)

# RapidOCR emits a per-line recognition score in [0, 1]; drop the faint/garbled
# low-confidence lines (semi-transparent menu glyphs, etc.).
_DEFAULT_MIN_CONFIDENCE = 0.5

# The recognition model's fixed tensor shape (channels, height, width). PP-OCRv5
# uses height 48 (which is also the rapidocr default, but we set it explicitly).
_REC_IMG_SHAPE = [3, 48, 320]

# Bundled Apache-2.0 Japanese recognition model (PP-OCRv5; dict baked into ONNX).
_REC_MODEL = Path(__file__).resolve().parent / "models" / "japan_ppocrv5_rec.onnx"

# A wide, short capture makes RapidOCR's text DETECTOR misbehave in two ways, and
# both drop or garble the text:
#
#   * Above ~8:1, RapidOCR skips detection outright (its `width_height_ratio`
#     config), feeding the whole strip to the recognizer as one squashed line.
#   * Well BELOW that — from ~2.5:1 up — the DB detector still runs but fragments
#     a single line into several out-of-order pieces and drops parts of it
#     (measured: "ついていってあげようか?」" → "てあげ。 | うか?」"). This is the real
#     cause of "a line with a 」 / ？」 ending won't detect" and of Area-mode /
#     thin-window misses: the trigger is the aspect ratio, not the punctuation.
#
# The detector wants text to be a modest fraction of the frame height; when text
# fills a short strip it breaks up. So we letterbox any over-wide capture down to
# ~2:1 (empirically the point where a filled line reads whole again — 3:1 still
# fragments, 2:1 is clean), then shift the boxes back. A normal game window is
# ~1.3–1.8:1, safely under the trigger, so full-frame Screen captures are never
# touched. On the >8:1 no-detection path RapidOCR also returns one box spanning
# the whole image, so this keeps grouping/overlay placement meaningful too.
_DETECT_MAX_RATIO = 2.5      # letterbox any capture wider than this
_DETECT_TARGET_RATIO = 2.0   # ...down to ~2:1, where a filled line reads whole

# Speed/quality knob: a pre-OCR upscale factor. More pixels
# help the detector find small/low-contrast game glyphs at the cost of latency
# (boxes are mapped back to source pixels afterwards). Pure per-frame knob — no
# model reload — so apply_speed stays live.
_SPEED_UPSCALE = {"fast": 1.0, "balanced": 1.5, "accurate": 2.0}


# RapidOCR resolves its detection / classification / recognition stages by BARE
# top-level module name (`ch_ppocr_v3_det`, ...) — its config.yaml stores those
# names and `RapidOCR.init_module` feeds them to `importlib.import_module`, relying
# on a `sys.path.append(<package dir>)` done at import time. That works from a
# normal site-packages install, but NOT in a frozen (Nuitka) build: there the
# modules are compiled into the binary under their package-qualified names
# (`rapidocr_onnxruntime.ch_ppocr_v3_det`), no top-level ones exist, and the
# sys.path trick can't reach them — so RapidOCR() raised and the whole OCR stage
# came up "unavailable" in the built exe. Registering them under the bare names
# first makes import_module find them in sys.modules. Harmless in a dev install
# (the names are simply already resolvable).
_SUBMODULES = ("ch_ppocr_v3_det", "ch_ppocr_v3_rec", "ch_ppocr_v2_cls")


def _register_rapidocr_submodules() -> None:
    import importlib
    import sys

    for name in _SUBMODULES:
        if name in sys.modules:
            continue
        try:
            sys.modules[name] = importlib.import_module(
                f"rapidocr_onnxruntime.{name}")
        except Exception:
            pass  # a renamed/absent stage surfaces as RapidOCR's own error


class PaddleEngine(OcrEngine):
    name = "RapidOCR"

    def __init__(self, min_confidence: float = _DEFAULT_MIN_CONFIDENCE,
                 require_japanese: bool = True, speed: str = DEFAULT_SPEED,
                 max_dimension: int = 2600) -> None:
        self._ocr = None
        self._min_confidence = min_confidence
        self._require_japanese = require_japanese
        self._upscale = _SPEED_UPSCALE[DEFAULT_SPEED]
        self._max_dimension = max_dimension
        self.apply_speed(speed)

    def apply_speed(self, preset: str) -> None:
        # Only the upscale factor changes — no weight reload, so this is live.
        self._upscale = _SPEED_UPSCALE.get(preset, _SPEED_UPSCALE[DEFAULT_SPEED])

    def load(self) -> None:
        if self._ocr is None:
            # Imported lazily so importing this module doesn't pull in
            # onnxruntime / opencv until the engine is actually selected.
            from rapidocr_onnxruntime import RapidOCR

            _register_rapidocr_submodules()
            self._ocr = RapidOCR(
                rec_model_path=str(_REC_MODEL),
                rec_img_shape=_REC_IMG_SHAPE,
            )

    def _scale_for(self, width: int, height: int) -> float:
        scale = self._upscale
        longest = max(width, height)
        if longest * scale > self._max_dimension:
            scale = max(1.0, self._max_dimension / longest)
        return scale

    def recognize(self, image: Image.Image) -> OcrResult:
        self.load()
        image = image.convert("RGB")
        # A wide, short grab (an Area-mode detect box) would otherwise skip
        # detection entirely — see _DETECT_MAX_RATIO. No-op for normal frames.
        image, pad_top = _letterbox(image)
        scale = self._scale_for(image.width, image.height)
        src = image
        if scale != 1.0:
            src = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.BICUBIC,
            )
        # RapidOCR/OpenCV expect a BGR ndarray (PIL gives RGB), so flip channels.
        arr = np.asarray(src)[:, :, ::-1].copy()
        result, _ = self._ocr(arr)
        regions = []
        for quad, text, score in result or []:
            text = (text or "").strip()
            if not text:
                continue
            # Repair katakana<->kanji lookalike misreads (工->エ, 協カ->協力), then
            # within-katakana misreads via JMdict (ワーポン->クーポン), before anything
            # downstream sees the text — see ocr.normalize / ocr.spellfix.
            text = spellfix_correct(fix_lookalikes(text))
            if self._require_japanese and not _JAPANESE.search(text):
                continue
            if _to_float(score) < self._min_confidence:
                continue
            regions.append(OcrRegion(text=text, box=_quad_box(quad, scale, pad_top)))
        return OcrResult(regions=regions)


def _to_float(score) -> float:
    """RapidOCR reports the per-line score as a string; be defensive."""
    try:
        return float(score)
    except (TypeError, ValueError):
        return 1.0


def _letterbox(image: Image.Image) -> tuple[Image.Image, int]:
    """Pad an over-wide image vertically so RapidOCR still runs detection.

    Returns `(image, y_offset)` — the offset is how far the original content was
    pushed down, so boxes can be mapped back. Images already inside
    `_DETECT_MAX_RATIO` are returned untouched with offset 0.

    The bands replicate the top and bottom edge rows rather than adding a flat
    colour, so we don't hand the detector an artificial high-contrast border to
    latch onto. See `_DETECT_MAX_RATIO` for why this is needed at all.
    """
    width, height = image.width, image.height
    if height <= 0 or width / height <= _DETECT_MAX_RATIO:
        return image, 0
    target = int(round(width / _DETECT_TARGET_RATIO))
    top = (target - height) // 2
    bottom = target - height - top
    canvas = Image.new("RGB", (width, target))
    if top > 0:
        canvas.paste(image.crop((0, 0, width, 1)).resize((width, top)), (0, 0))
    if bottom > 0:
        canvas.paste(image.crop((0, height - 1, width, height)).resize((width, bottom)),
                     (0, top + height))
    canvas.paste(image, (0, top))
    return canvas, top


def _quad_box(quad, scale: float = 1.0, y_offset: int = 0):
    """RapidOCR's 4-point polygon -> axis-aligned (x0, y0, x1, y1) in source px.

    `scale` is the pre-OCR upscale factor; coords are divided by it so boxes map
    back to the original (un-upscaled) image the caller positions against.
    `y_offset` undoes any letterbox padding (see `_letterbox`).
    """
    if not quad:
        return None
    xs = [p[0] for p in quad]
    ys = [p[1] for p in quad]
    return (
        int(min(xs) / scale),
        int(min(ys) / scale) - y_offset,
        int(max(xs) / scale),
        int(max(ys) / scale) - y_offset,
    )
