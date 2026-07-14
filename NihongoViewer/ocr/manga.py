"""MangaOCR engine — Japanese text (visual novels, manga UI, older JRPGs).

`manga-ocr` is a **recognition-only** model: it reads ONE tightly-cropped block
of text and returns a string. It has no text *detection*, so feeding it a whole
captured game frame (art + UI + several text areas) produces garbage — the model
has nothing single-block to read. That was the original bug.

We therefore pair it with a detector to get the input manga-ocr actually expects:

    meikiocr detect+read  ->  drop non-Japanese boxes  ->  GROUP into blocks
      ->  manga-ocr reads each Japanese block

We run `meikiocr.run_ocr` (a **core** dependency — ONNX, no torch), which both
locates text boxes (any orientation) *and* reads them. meikiocr's reading is the
**source-language gate**: it is Latin-capable and reads the actual script, so a
browser URL reads as `lora/comments/...` (no Japanese) while a bubble reads as
Japanese. Boxes with no Japanese are dropped **at the source** — crucial because
manga-ocr would otherwise *hallucinate* plausible Japanese from a Latin URL and
sail past any output-based filter. Only the surviving Japanese boxes go to
manga-ocr for the accurate reading (which also means manga-ocr never wastes a
pass on junk).

The grouping step is what makes MangaOCR right for manga / visual novels: a
speech bubble is several vertical columns (or lines) that belong together. If we
translated each line separately, every line's English would start at the same
origin and the overlay boxes would overlap. So we cluster nearby line boxes into
one block (≈ one bubble), read the whole block in a single manga-ocr pass (it
reads multi-line / vertical text natively, in correct order), and return **one
region per block** — one translation, one overlay, no overlap.

The heavy `torch` / `manga-ocr` import stays guarded so MeikiOCR-only users are
never forced to install torch.
"""

import re

import numpy as np
from PIL import Image

from .base import DEFAULT_SPEED, OcrEngine, OcrRegion, OcrResult

# A block is real Japanese text only if it contains an actual kana or kanji. This
# drops standalone English/Latin junk the detector picks up from around the
# manga — browser chrome (tab titles, address bar), window titles, UI buttons —
# while KEEPING English/numbers that sit *inside* a Japanese block (the
# surrounding kana/kanji still qualify it). Mirrors MeikiOCR's `_JAPANESE` gate.
_JAPANESE = re.compile(
    "[ぁ-ゖ"   # hiragana syllables
    "ァ-ヺ"    # katakana syllables
    "ㇰ-ㇿ"    # katakana phonetic extensions
    "㐀-䶿"    # CJK extension A
    "一-鿿"    # CJK unified ideographs (kanji)
    "ｦ-ﾝ]"   # halfwidth katakana
)

# Detectors clip tight to the glyphs; manga-ocr reads a touch cleaner with a
# little margin around the block, so pad each block box outward before cropping.
_BOX_PAD = 6

# Two line boxes join the same block when the gap between them (in both axes) is
# within this many "character sizes". Adjacent columns/lines of one bubble sit
# well under a character apart; separate bubbles are much farther, so ~1.6 keeps
# a bubble together without swallowing its neighbour.
_GROUP_TOLERANCE = 1.6

# Speed/quality knobs per preset. `det_max_dim` caps the frame's longest side for
# the ONNX detector (0 = no cap): a smaller detection image is faster but can miss
# small text; a higher threshold detects fewer (only stronger) boxes, so fewer
# manga-ocr passes. Crops for manga-ocr are always taken from the FULL-resolution
# frame, so the reading quality of each detected block is unchanged by the cap.
_SPEED = {
    "fast":     {"det_max_dim": 1500, "det_threshold": 0.55},
    "balanced": {"det_max_dim": 2000, "det_threshold": 0.50},
    "accurate": {"det_max_dim": 0,    "det_threshold": 0.50},
}

# manga-ocr's torch pass is the dominant cost, and game dialogue lingers on screen
# for several capture ticks. So we cache each block's reading keyed on the
# detector's text for that block: an unchanged block skips the torch pass entirely
# on later frames. Bounded (FIFO) so a long session can't grow it without limit.
_CACHE_MAX = 128


class MangaEngine(OcrEngine):
    name = "MangaOCR"

    def __init__(self, det_threshold: float | None = None, box_pad: int = _BOX_PAD,
                 group_tolerance: float = _GROUP_TOLERANCE,
                 speed: str = DEFAULT_SPEED) -> None:
        self._mocr = None   # manga-ocr recognizer (torch, optional heavy dep)
        self._det = None    # meikiocr detector (ONNX, core dep — always present)
        self._box_pad = box_pad
        self._group_tolerance = group_tolerance
        # Set by the speed preset (see apply_speed); an explicit det_threshold
        # still wins so callers/tests can pin it.
        self._det_threshold = 0.5
        self._det_max_dim = 0
        self.apply_speed(speed)
        if det_threshold is not None:
            self._det_threshold = det_threshold
        # block-text -> manga-ocr reading; skips the torch pass on repeat frames.
        self._cache: dict[str, str] = {}

    def apply_speed(self, preset: str) -> None:
        # Detection size + threshold only — no weight reload, so this is live.
        knobs = _SPEED.get(preset, _SPEED[DEFAULT_SPEED])
        self._det_threshold = knobs["det_threshold"]
        self._det_max_dim = knobs["det_max_dim"]

    def _read_cached(self, key: str, crop: Image.Image) -> str:
        """manga-ocr reading for `key`, from cache or a fresh (cached) torch pass."""
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        text = (self._mocr(crop) or "").strip()
        if len(self._cache) >= _CACHE_MAX:
            # Drop the oldest entry (dicts keep insertion order) to stay bounded.
            self._cache.pop(next(iter(self._cache)))
        self._cache[key] = text
        return text

    def load(self) -> None:
        if self._mocr is None:
            try:
                from manga_ocr import MangaOcr
            except ImportError as exc:  # torch / manga-ocr not installed
                raise RuntimeError(
                    "MangaOCR needs the optional 'manga-ocr' package (pulls in torch). "
                    "Install it with:  pip install -r requirements-mangaocr.txt"
                ) from exc
            self._mocr = MangaOcr()
        if self._det is None:
            # meikiocr is a core dependency (no torch), so this never adds an
            # install burden beyond what MeikiOCR already needs.
            from meikiocr import MeikiOCR

            self._det = MeikiOCR()

    def recognize(self, image: Image.Image) -> OcrResult:
        self.load()
        image = image.convert("RGB")
        # Optionally downscale for the detector only (speed knob). Crops for
        # manga-ocr are taken from `image` (full res), so reading stays sharp.
        scale = _det_scale(image.width, image.height, self._det_max_dim)
        det_img = image
        if scale != 1.0:
            det_img = image.resize(
                (round(image.width * scale), round(image.height * scale)),
                Image.BICUBIC,
            )
        # meikiocr expects an OpenCV-style BGR array; PIL gives RGB.
        arr = np.asarray(det_img)[:, :, ::-1].copy()

        # Detect AND read every line with meikiocr. Its reading is used only as a
        # language gate (below); manga-ocr does the real reading of the crop.
        # Boxes come back in detection-image pixels, so map them to full res.
        lines = []  # (box, meiki_text)
        for line in self._det.run_ocr(arr, self._det_threshold):
            box = _line_box(line, scale)
            if box is not None:
                lines.append((box, line.get("text") or ""))
        if not lines:
            return OcrResult(regions=[])

        boxes = [b for b, _ in lines]
        # Cluster line/column boxes into blocks (≈ speech bubbles).
        blocks = []  # (union_box, combined_meiki_text)
        for idxs in _group_indices(boxes, self._group_tolerance):
            blocks.append((
                _union(boxes[i] for i in idxs),
                "".join(lines[i][1] for i in idxs),
            ))

        regions = []
        for union_box, meiki_text in _reading_order(blocks):
            # Source-language gate: meikiocr read the ACTUAL script (it is
            # Latin-capable), so a block with no Japanese here is English/URL/UI
            # junk — drop it before manga-ocr can hallucinate Japanese from it.
            # English *inside* a Japanese block survives (the kana/kanji qualify).
            if not _JAPANESE.search(meiki_text):
                continue
            box = _pad_clamp(union_box, image.width, image.height, self._box_pad)
            if box is None:
                continue
            # manga-ocr reads the whole block in one pass, in correct reading
            # order (top-to-bottom, right-to-left for vertical text). The reading
            # is cached on the block's detector text, so a block that lingers
            # across frames skips the (expensive) torch pass after the first read.
            text = self._read_cached(meiki_text, image.crop(box))
            if not text:
                continue
            if not _JAPANESE.search(text):  # belt-and-suspenders on the output too
                continue
            regions.append(OcrRegion(text=text, box=box))
        return OcrResult(regions=regions)


def _group_indices(boxes, tolerance):
    """Cluster boxes into blocks; returns a list of index lists (one per block).

    Boxes are joined (union-find) when the empty gap between them is small in
    both axes — adjacent columns of a vertical bubble overlap in Y with a tiny X
    gap; stacked lines overlap in X with a tiny Y gap. The threshold scales with
    the estimated character size so it adapts to text at any zoom.
    """
    if not boxes:
        return []

    # Character size ≈ the short side of a line box (a vertical column's width,
    # a horizontal line's height). Median is robust to stray tiny/huge boxes.
    shorts = sorted(min(x2 - x1, y2 - y1) for x1, y1, x2, y2 in boxes)
    char = shorts[len(shorts) // 2] or 1
    tol = char * tolerance

    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        parent[find(i)] = find(j)

    for i in range(len(boxes)):
        for j in range(i + 1, len(boxes)):
            gx, gy = _gap(boxes[i], boxes[j])
            if gx <= tol and gy <= tol:
                union(i, j)

    groups: dict[int, list] = {}
    for i in range(len(boxes)):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _union(boxes):
    """Smallest box covering all of `boxes` -> (x0, y0, x1, y1)."""
    boxes = list(boxes)
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def _line_box(line, scale: float = 1.0):
    """Union of a meikiocr line's per-char boxes -> (x0, y0, x1, y1), or None.

    `scale` is the pre-detection downscale factor; boxes are divided by it so they
    map back to the original (full-resolution) frame the crops are taken from.
    """
    boxes = [c["bbox"] for c in line.get("chars", []) if c.get("bbox")]
    if not boxes:
        return None
    return (
        int(min(b[0] for b in boxes) / scale), int(min(b[1] for b in boxes) / scale),
        int(max(b[2] for b in boxes) / scale), int(max(b[3] for b in boxes) / scale),
    )


def _det_scale(width: int, height: int, max_dim: int) -> float:
    """Downscale factor (<=1.0) so the longest side is at most `max_dim` (0 = off)."""
    longest = max(width, height)
    if not max_dim or longest <= max_dim:
        return 1.0
    return max_dim / longest


def _gap(a, b):
    """Empty (non-overlapping) gap between two boxes as (gap_x, gap_y); 0 if they overlap on that axis."""
    gx = max(0, max(a[0], b[0]) - min(a[2], b[2]))
    gy = max(0, max(a[1], b[1]) - min(a[3], b[3]))
    return gx, gy


def _reading_order(blocks):
    """Order (box, text) blocks as a reader scans a page: top-to-bottom, right-to-left."""
    return sorted(blocks, key=lambda bt: (bt[0][1], -bt[0][2]))


def _pad_clamp(bbox, width, height, pad):
    """Pad (x1,y1,x2,y2) outward by `pad` and clamp to the frame; None if empty."""
    x1, y1, x2, y2 = bbox
    x1 = max(0, int(x1) - pad)
    y1 = max(0, int(y1) - pad)
    x2 = min(width, int(x2) + pad)
    y2 = min(height, int(y2) + pad)
    if x2 <= x1 or y2 <= y1:
        return None
    return (x1, y1, x2, y2)
