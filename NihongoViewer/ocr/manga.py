"""MangaOCR engine — vertical (縦書き / tategaki) Japanese text via manga-ocr (ONNX).

The **vertical** OCR engine, selected when the user picks OCR mode = *Vertical*
(a manga-reader use case). It is **Apache-2.0, torch-free and paddle-free**: it runs
the `mayocream/manga-ocr-onnx` export (a Vision-Encoder-Decoder — ViT image encoder +
autoregressive text decoder) through pure `onnxruntime`, so both the code *and* the
model weights are commercial-clean (same bar as the shipped RapidOCR engine; see
THIRD_PARTY_LICENSES.md).

Crucially, manga-ocr is **recognition-only**: it turns *one already-cropped text
block* (a speech bubble / a single column run) into a string, with **no text detector
and no bounding boxes**. So this engine is used in **Area mode only** — the user draws
a detect box around the bubble and we recognize the whole crop as one region. Full-frame
Screen mode keeps using RapidOCR (its DB detector is horizontal and can't feed clean
vertical crops here — see CLAUDE.md "Open decisions" #3).

Pieces and their licenses (all Apache-2.0), bundled under ``ocr/models/manga/``:
  * ``encoder_model.onnx`` — ViT image encoder (pixel_values -> last_hidden_state).
  * ``decoder_model.onnx`` — text decoder (input_ids + encoder_hidden_states -> logits).
  * ``vocab.txt`` — the character-level tokenizer's vocabulary (one token per line,
    id == line index). manga-ocr's tokenizer is ``character`` subword type, so decoding
    is a plain id->line lookup + join — **no MeCab/unidic needed at runtime** (those are
    only used training-side).
  * ``*_config.json`` — preprocessing (224x224, /255, normalize 0.5/0.5) and generation
    (decoder start / eos / pad ids) parameters, read at load time.

`recognize` returns a single `OcrRegion` spanning the whole crop; `group.py` passes it
through unchanged and the Area-mode path draws the translation into the box's translate
rect (the region box isn't used for placement in Area mode).
"""

import json
import re
from pathlib import Path

import numpy as np
from PIL import Image

from .base import DEFAULT_SPEED, OcrEngine, OcrRegion, OcrResult

# Bundled manga-ocr ONNX export (Apache-2.0). Overridable for the frozen build /
# tests via NIHONGOVIEWER_MANGA_DIR, mirroring the Qwen dir override.
_MODEL_DIR = Path(__file__).resolve().parent / "models" / "manga"
_DIR_ENV = "NIHONGOVIEWER_MANGA_DIR"

# "Is this really Japanese?" gate — manga-ocr can hallucinate a short string on a
# blank/near-empty crop; requiring a kana/kanji char drops most of that noise while
# keeping any genuine bubble text. Same character classes as ocr.paddle._JAPANESE.
_JAPANESE = re.compile(
    "[ぁ-ゖ"   # hiragana
    "ァ-ヺ"    # katakana
    "ㇰ-ㇿ"    # katakana phonetic extensions
    "㐀-䶿"    # CJK extension A
    "一-鿿"    # CJK unified ideographs (kanji)
    "ｦ-ﾝ]"   # halfwidth katakana
)

# Safety cap on generated tokens. The decoder has no KV-cache (the export is the
# plain decoder_model.onnx, not decoder_with_past), so each step re-reads the whole
# prefix — O(n^2). A real bubble stops early at [SEP] (~20-40 tokens); this only
# bounds the worst case where manga-ocr loops on a bad crop. 160 comfortably covers
# a full bubble while keeping a mistaken box from running the full generation_config
# max_length (300) worth of quadratic decode.
_MAX_NEW_TOKENS = 160

# manga-ocr has no confidence score and will hallucinate a short Japanese string on a
# blank/near-uniform crop (an empty Area box). A genuine text bubble always has strong
# dark/light contrast, so a tiny grayscale std means "no text here" — skip recognition
# entirely. The threshold is deliberately very low (real text is tens of levels of std)
# so faint low-contrast text is never dropped.
_MIN_STD = 3.0

# Screen-mode block assembly (see `recognize_frame`): the RapidOCR detector returns one
# box per vertical *column*; the columns of one speech bubble must be merged into a
# single block so manga-ocr reads the whole bubble (multi-column, right-to-left) in one
# pass. Two boxes merge when the gap between them is under a fraction of the typical
# column thickness — wider allowance across x (adjacent columns sit side by side) than y
# (vertically separated boxes are usually different bubbles/lines).
_MERGE_X = 1.2   # × median column thickness — horizontal gap that still merges columns
_MERGE_Y = 0.5   # × median column thickness — vertical gap that still merges
# Only merge boxes of similar glyph size (short side): min/max ≥ this. Stops a normal
# dialogue column from absorbing a big adjacent brush-SFX box — the bug where the clean
# "俺の…ッ♥" bubble merged with the "ぬちゃ" moan next to it and read as garbage. Columns
# of one bubble are near-identical size (ratio ~1), so real merges are unaffected.
_MERGE_SIZE_RATIO = 0.4
_BLOCK_PAD = 0.15  # crop padding around a merged block, × block short side

# A merged block wider than this many text-columns is not a speech bubble: it's either a
# big graphic sound-effect (a stylised scream drawn over the panel) or an art-blob the
# detector stitched together from scattered marks over the artwork — both of which
# manga-ocr "reads" as garbage/hallucinated sentences. Real dialogue bubbles are only a
# few columns wide (measured max ~6.7 across the manga/doujin test set; wordy pages use
# TALLER columns, not more of them), while the junk blocks measured 11–14 columns — so a
# cut here drops the graphic SFX and hallucinations without touching real bubble text.
# The user's steer: focus on text inside bubbles, ignore graphic sound words.
_MAX_BLOCK_COLS = 9.0

# A block whose glyph size (median detected-box thickness) is this many times the page's
# normal text size is a big drawn graphic sound-effect (a brush-stroke scream over the
# panel), not dialogue. manga-ocr always mis-reads these stylised glyphs as garbage
# ("んええろいいう"), so we drop them rather than translate nonsense. Measured: real
# dialogue sits at 0.8–1.6× the page median; graphic SFX at 2.0–5.6×. Catches the tall,
# narrow SFX that the width cap (_MAX_BLOCK_COLS) can't. Focus = text inside bubbles.
_MAX_GLYPH_RATIO = 2.0
# ...but oversized glyphs are only dropped when the block also sits on a BUSY background
# (ring std over this). A big drawn SFX is over artwork (ring std measured 52–107); a
# shouted greeting drawn large *inside a clean speech balloon* has a low ring std (0–11)
# and is real dialogue — so it's kept. Without this, big-but-real bubbles like
# 「和真おはよ」/「カズくん！おはよう！」 were wrongly dropped as SFX.
_GLYPH_RING_STD = 40.0

# Targeted small-SFX filter (for the handwritten sound-words drawn over artwork that are
# the same SIZE as dialogue, so the size/width caps can't catch them). Drop a block only
# when ALL hold: it's SHORT (≤ this many kana/kanji chars), KANA-ONLY (no kanji), and sits
# on a BUSY background (ring std over the threshold — artwork, not a clean balloon). This
# spares narration (has kanji / longer) and short lines inside clean bubbles (low ring
# std), while removing や / っ / ぐぢ / ドキ-type SFX over the art.
_SFX_MAX_CHARS = 3
_SFX_RING_STD = 55.0
_KANA_KANJI = re.compile("[ぁ-ゖァ-ヺㇰ-ㇿ㐀-䶿一-鿿ｦ-ﾝ]")
_KANJI = re.compile("[㐀-䶿一-鿿]")


def _ring_std(image: Image.Image, box: tuple, pad: int) -> float:
    """Std-dev of the pixels in the ring just OUTSIDE `box` (background busyness).

    A clean speech balloon around the text has low std; artwork/screentone is high.
    """
    x0, y0, x1, y1 = box
    ex0, ey0 = max(0, x0 - pad), max(0, y0 - pad)
    ex1, ey1 = min(image.width, x1 + pad), min(image.height, y1 + pad)
    g = np.asarray(image.convert("L"), dtype=np.float32)[ey0:ey1, ex0:ex1]
    if g.size == 0:
        return 0.0
    mask = np.ones(g.shape, bool)
    mask[max(0, y0 - ey0):max(0, y1 - ey0), max(0, x0 - ex0):max(0, x1 - ex0)] = False
    ring = g[mask]
    return float(ring.std()) if ring.size else 0.0

# Drop only VERY wide, short detection boxes (width > height × this): a site watermark
# like "manga1001.com" or an English caption is ~8:1. We used to cut at 1.3, but that also
# killed the wide horizontal STRIPS that dramatic dot-spaced vertical text (彼・は・選・ば…)
# fragments into — so the bubble went undetected. At 5:1 those strips survive and merge
# back into the bubble (they overlap in x — see the merge size-guard), while true 8:1+
# watermarks/captions are still dropped. Vertical columns are tall (≤1:1 wide), unaffected.
_MAX_BOX_WH = 5.0


# Cap the crop's aspect ratio before the square 224 resize. A ratio at/under this is
# left alone; anything more extreme is padded on the short axis. 1.5 keeps a single
# vertical column readable without distorting multi-column bubbles (already near-square).
_MAX_ASPECT = 1.5


def _pad_toward_square(img: Image.Image) -> Image.Image:
    """Pad the shorter side (with the border median colour) so max/min aspect ≤ _MAX_ASPECT.

    Centers the content; returns the image unchanged if it's already within the cap.
    """
    w, h = img.size
    if w == 0 or h == 0:
        return img
    long_side, short_side = max(w, h), min(w, h)
    if long_side <= short_side * _MAX_ASPECT:
        return img
    target_short = int(round(long_side / _MAX_ASPECT))
    tw = max(w, target_short) if w < h else w
    th = max(h, target_short) if h < w else h
    a = np.asarray(img)
    border = np.concatenate([a[0, :, :], a[-1, :, :], a[:, 0, :], a[:, -1, :]], axis=0)
    color = tuple(int(v) for v in np.median(border, axis=0))
    canvas = Image.new("RGB", (tw, th), color)
    canvas.paste(img, ((tw - w) // 2, (th - h) // 2))
    return canvas


def _merge_boxes(boxes: list, mx: float, my: float) -> list:
    """Union boxes whose inflated rects touch; return `(bbox, glyph_size)` per group.

    `glyph_size` is the median thickness of the group's member boxes (a proxy for text
    size, used to spot oversized graphic SFX). `mx`/`my` are the horizontal/vertical gap
    allowances in pixels. Simple O(n²) union-find — n is a handful of text boxes/frame.
    """
    boxes = [tuple(b) for b in boxes if b]
    n = len(boxes)
    shorts = [float(min(b[2] - b[0], b[3] - b[1])) for b in boxes]
    if n <= 1:
        return [(b, shorts[i]) for i, b in enumerate(boxes)]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def near(i, j) -> bool:
        a, b = boxes[i], boxes[j]
        gap_x = max(b[0] - a[2], a[0] - b[2], 0.0)  # 0 if they overlap in x
        gap_y = max(b[1] - a[3], a[1] - b[3], 0.0)
        if gap_x > mx or gap_y > my:
            return False
        # Size guard only for SIDE-BY-SIDE boxes (a real horizontal gap between them):
        # stops a dialogue column absorbing a big adjacent brush-SFX box. Boxes that
        # OVERLAP in x (gap_x == 0) are stacked in the same column — always merge them
        # regardless of size, so dot-spaced text detected as mixed strip sizes rejoins.
        if gap_x > 0:
            si, sj = shorts[i], shorts[j]
            if min(si, sj) < _MERGE_SIZE_RATIO * max(si, sj):
                return False
        return True

    for i in range(n):
        for j in range(i + 1, n):
            if near(i, j):
                parent[find(i)] = find(j)

    groups: dict = {}
    for i, b in enumerate(boxes):
        groups.setdefault(find(i), []).append(b)
    out = []
    for members in groups.values():
        x0 = min(m[0] for m in members)
        y0 = min(m[1] for m in members)
        x1 = max(m[2] for m in members)
        y1 = max(m[3] for m in members)
        shorts = sorted(min(m[2] - m[0], m[3] - m[1]) for m in members)
        glyph = float(shorts[len(shorts) // 2])  # median box thickness = glyph size
        out.append(((x0, y0, x1, y1), glyph))
    return out


class MangaEngine(OcrEngine):
    """manga-ocr recognition-only engine for vertical Japanese (Area mode)."""

    name = "MangaOCR"

    def __init__(self, speed: str = DEFAULT_SPEED, require_japanese: bool = True) -> None:
        self._enc = None
        self._dec = None
        self._vocab: list[str] = []
        self._require_japanese = require_japanese
        # Preprocessing / generation params — defaults match the bundled configs and
        # are overwritten from the JSON in load() so a future re-export stays correct.
        self._size = 224
        self._mean = 0.5
        self._std = 0.5
        self._start_id = 2   # [CLS]
        self._eos_id = 3     # [SEP]
        self._pad_id = 0     # [PAD]
        # speed preset is accepted for interface parity; manga-ocr exposes no knob.

    # apply_speed: inherited no-op (manga-ocr has no speed/quality tradeoff).

    def _dir(self) -> Path:
        import os

        override = os.environ.get(_DIR_ENV)
        return Path(override) if override else _MODEL_DIR

    def load(self) -> None:
        if self._enc is not None:
            return
        # Imported lazily so importing this module doesn't pull onnxruntime in until
        # the vertical engine is actually selected.
        import onnxruntime as ort

        d = self._dir()
        try:
            pp = json.loads((d / "preprocessor_config.json").read_text(encoding="utf-8"))
            self._size = int(pp["size"]["height"])
            self._mean = float(pp["image_mean"][0])
            self._std = float(pp["image_std"][0])
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass  # keep the defaults above
        try:
            gen = json.loads((d / "generation_config.json").read_text(encoding="utf-8"))
            self._start_id = int(gen["decoder_start_token_id"])
            self._eos_id = int(gen["eos_token_id"])
            self._pad_id = int(gen.get("pad_token_id", 0))
        except (OSError, KeyError, ValueError, json.JSONDecodeError):
            pass
        # vocab.txt: one token per line, id == line index. Preserve blank lines so the
        # index alignment is exact; only drop a single trailing newline's empty entry.
        vocab = (d / "vocab.txt").read_text(encoding="utf-8").split("\n")
        if vocab and vocab[-1] == "":
            vocab.pop()
        self._vocab = vocab

        opts = ort.SessionOptions()
        self._enc = ort.InferenceSession(
            str(d / "encoder_model.onnx"), opts, providers=["CPUExecutionProvider"])
        self._dec = ort.InferenceSession(
            str(d / "decoder_model.onnx"), opts, providers=["CPUExecutionProvider"])
        self._enc_input = self._enc.get_inputs()[0].name  # "pixel_values"

    def _preprocess(self, image: Image.Image) -> np.ndarray:
        # manga-ocr feeds a grayscale image spread over 3 channels, then the ViT
        # feature extractor: resize to size x size, /255, normalize (x-mean)/std.
        # First pad extreme aspect ratios toward square: a lone tall vertical column
        # (e.g. 1:6) would otherwise be squashed by the square resize and lose its
        # bottom characters. Padding with the border colour keeps the glyphs undistorted
        # and on-distribution (manga-ocr trained on roughly bubble-shaped crops).
        img = _pad_toward_square(image.convert("L").convert("RGB"))
        img = img.resize((self._size, self._size), Image.BILINEAR)
        x = np.asarray(img, dtype=np.float32) / 255.0
        x = (x - self._mean) / self._std
        x = x.transpose(2, 0, 1)[None]  # HWC -> (1, C, H, W)
        return np.ascontiguousarray(x, dtype=np.float32)

    def recognize(self, image: Image.Image) -> OcrResult:
        self.load()
        # Blank/near-uniform crop -> no text (avoids a hallucination on an empty box).
        gray = np.asarray(image.convert("L"), dtype=np.float32)
        if gray.size == 0 or float(gray.std()) < _MIN_STD:
            return OcrResult(regions=[])
        pixel_values = self._preprocess(image)
        enc = self._enc.run(None, {self._enc_input: pixel_values})[0]

        # Greedy autoregressive decode (top-1) — matches the app's greedy translation
        # so "same crop -> same text" holds for the frame caches upstream. A
        # no-repeat-3gram block (from the model's generation_config) stops manga-ocr
        # looping forever on big scream/SFX text ("いやあああ..." -> 160 × あ), which
        # otherwise wastes the whole token budget and yields garbage to translate.
        ids = [self._start_id]
        for _ in range(_MAX_NEW_TOKENS):
            dec_in = {
                "input_ids": np.array([ids], dtype=np.int64),
                "encoder_hidden_states": enc,
            }
            row = self._dec.run(None, dec_in)[0][0, -1]
            banned = self._banned_ngram_tokens(ids)
            if banned:
                row = row.copy()
                for t in banned:
                    row[t] = -np.inf
            nxt = int(row.argmax())
            if nxt == self._eos_id:
                break
            ids.append(nxt)

        text = self._detokenize(ids[1:]).strip()  # drop the start token
        if not text:
            return OcrResult(regions=[])
        if self._require_japanese and not _JAPANESE.search(text):
            return OcrResult(regions=[])
        # One region spanning the whole crop (manga-ocr gives no sub-boxes).
        box = (0, 0, image.width, image.height)
        return OcrResult(regions=[OcrRegion(text=text, box=box)])

    def recognize_frame(self, image: Image.Image, boxes: list) -> OcrResult:
        """Screen-mode vertical OCR: read a whole frame given detected text `boxes`.

        manga-ocr has no detector, so the caller supplies boxes from RapidOCR's DB
        detector (see `PaddleEngine.detect_boxes`). Adjacent column-boxes are merged
        into per-bubble blocks, each block crop is recognized, and one `OcrRegion` is
        returned per block (positioned at the block's box for the overlay). Blocks are
        ordered right-to-left, top-to-bottom (manga reading order) for the Text panel.
        """
        self.load()
        # Keep only vertical/square boxes — drop wide horizontal runs (English captions,
        # watermarks, SFX) that aren't 縦書き text (see _MAX_BOX_WH).
        boxes = [b for b in (boxes or []) if b
                 and (b[2] - b[0]) <= (b[3] - b[1]) * _MAX_BOX_WH]
        if not boxes:
            return OcrResult(regions=[])
        shorts = [min(x1 - x0, y1 - y0) for (x0, y0, x1, y1) in boxes]
        shorts.sort()
        col = shorts[len(shorts) // 2] or 1.0  # median column thickness
        merged = _merge_boxes(boxes, mx=_MERGE_X * col, my=_MERGE_Y * col)
        ring_pad = max(4, int(0.6 * col))
        # Keep only real speech-bubble text. Drop blocks too WIDE to be a bubble
        # (art-blob / wide SFX, _MAX_BLOCK_COLS). Drop blocks with oversized GLYPHS
        # (_MAX_GLYPH_RATIO) ONLY when also over a busy background (_GLYPH_RING_STD) —
        # that's a big drawn graphic SFX; a big shout inside a clean balloon is dialogue.
        # `ring` is carried so the small-SFX drop below doesn't recompute it.
        blocks = []
        for (bbox, glyph) in merged:
            if (bbox[2] - bbox[0]) > _MAX_BLOCK_COLS * col:
                continue
            ring = _ring_std(image, bbox, ring_pad)
            if glyph >= _MAX_GLYPH_RATIO * col and ring > _GLYPH_RING_STD:
                continue
            blocks.append((bbox, ring))
        # Reading order: right-to-left columns, then top-to-bottom.
        blocks.sort(key=lambda t: (-t[0][2], t[0][1]))

        W, H = image.width, image.height
        regions: list[OcrRegion] = []
        for (x0, y0, x1, y1), ring in blocks:
            pad = int(round(_BLOCK_PAD * min(x1 - x0, y1 - y0)))
            cx0, cy0 = max(0, x0 - pad), max(0, y0 - pad)
            cx1, cy1 = min(W, x1 + pad), min(H, y1 + pad)
            if cx1 <= cx0 or cy1 <= cy0:
                continue
            res = self.recognize(image.crop((cx0, cy0, cx1, cy1)))
            text = res.text
            if not text:
                continue
            # Targeted small-SFX drop: short + kana-only + busy background (see
            # _SFX_MAX_CHARS / _SFX_RING_STD). Longer text and anything with kanji
            # (dialogue, narration) is always kept.
            chars = _KANA_KANJI.findall(text)
            if (len(chars) <= _SFX_MAX_CHARS and not _KANJI.search(text)
                    and ring > _SFX_RING_STD):
                continue
            regions.append(OcrRegion(text=text, box=(x0, y0, x1, y1)))
        return OcrResult(regions=regions)

    @staticmethod
    def _banned_ngram_tokens(ids: list[int]) -> set:
        """Tokens that would complete an already-seen 3-gram (no_repeat_ngram_size=3).

        Given the current tail (ids[-2], ids[-1]), any token `c` such that that pair was
        earlier followed by `c` is blocked — this halts runaway repetition without
        touching normal text.
        """
        if len(ids) < 2:
            return set()
        prefix = (ids[-2], ids[-1])
        banned = set()
        for k in range(len(ids) - 2):
            if (ids[k], ids[k + 1]) == prefix:
                banned.add(ids[k + 2])
        return banned

    def _detokenize(self, ids: list[int]) -> str:
        # Character-level tokenizer: each id is one token; join with no separator.
        out: list[str] = []
        for i in ids:
            if i in (self._pad_id, self._start_id, self._eos_id):
                continue
            if 0 <= i < len(self._vocab):
                tok = self._vocab[i]
                if tok.startswith("##"):  # not emitted by a char tokenizer, but be safe
                    tok = tok[2:]
                if len(tok) >= 2 and tok.startswith("[") and tok.endswith("]"):
                    continue  # skip any [PAD]/[UNK]/[CLS]/[SEP]/[MASK]/<unusedN>-style special
                if tok.startswith("<") and tok.endswith(">"):
                    continue  # skip <unusedN> placeholders
                out.append(tok)
        return "".join(out)
