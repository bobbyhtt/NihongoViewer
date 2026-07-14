"""Turn per-line OCR regions into sentence-chunks for translation + placement.

MeikiOCR returns one region per *physical line*. Two things go wrong if we treat
each line as a translation unit:

  * a sentence that wraps across lines ("本当にただの" / "学生?") gets translated in
    fragments — mangled or half-dropped;
  * conversely, translating a whole multi-line block as one unit and drawing it in
    one place reads as if only part of the text was translated (the English bunches
    up next to one line).

So we do two passes. First, group vertically-adjacent, horizontally-overlapping
lines into a **block** (a name label or far menu button stays its own block).
Then, *within* a block, split the lines into **sentence-chunks**: consecutive
lines are merged until the text so far ends a sentence. Each chunk becomes one
region — its text is the merged (de-wrapped) source, its box the union of its
lines — so the pipeline translates it as a coherent unit *and* the overlay draws
it over its own lines. A sentence per visible line therefore lands one English
line per Japanese line; a wrapped sentence stays whole over the lines it spans.

Japanese has no inter-word spaces, so wrapped lines are joined with **no**
separator. Regions without a box (e.g. MangaOCR whole-bubble blocks) pass through.
"""

from .base import OcrRegion

# A candidate line joins a block when the vertical gap to the block's current
# bottom is at most this fraction of the line's height...
_GAP_RATIO = 0.9
# ...and its horizontal span overlaps the block's by at least this fraction of the
# narrower span (lines of one paragraph share a column; a side label does not).
_OVERLAP_RATIO = 0.3

# Sentence-ending marks (Japanese + ASCII), and trailing closers to look past when
# deciding whether a line ends a sentence (a line can end "…だ。」" or "…する?』").
_TERMINATORS = "。．！？!?"
_CLOSERS = "」』）)】〕〉》〙〗”’\"'"


def _completes_sentence(text: str) -> bool:
    """True if `text`, ignoring trailing quotes/brackets, ends on a terminator."""
    t = text.rstrip()
    while t and t[-1] in _CLOSERS:
        t = t[:-1].rstrip()
    return bool(t) and t[-1] in _TERMINATORS


def _merge(lines: list[OcrRegion]) -> OcrRegion:
    """One region from consecutive line-regions: de-wrapped text + union box."""
    boxes = [line.box for line in lines]
    box = (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )
    return OcrRegion(text="".join(line.text for line in lines), box=box)


def group_lines(regions, *, gap_ratio: float = _GAP_RATIO,
                overlap_ratio: float = _OVERLAP_RATIO) -> list[OcrRegion]:
    """Merge line-regions into sentence-chunks (see module docstring)."""
    boxed = [r for r in regions if r.box]
    passthrough = [r for r in regions if not r.box]
    # Reading order: top-to-bottom, then left-to-right.
    boxed.sort(key=lambda r: (r.box[1], r.box[0]))

    # Pass 1 — geometric blocks (each a list of line-regions in reading order).
    blocks: list[list[OcrRegion]] = []
    boxes: list[tuple] = []  # running union box per block, parallel to `blocks`
    for r in boxed:
        x0, y0, x1, y1 = r.box
        height = max(1, y1 - y0)
        for i, (bx0, by0, bx1, by1) in enumerate(boxes):
            gap = y0 - by1  # >0 below the block, <0 overlapping it vertically
            overlap = min(x1, bx1) - max(x0, bx0)
            min_width = max(1, min(x1 - x0, bx1 - bx0))
            if -0.5 * height <= gap <= gap_ratio * height and overlap >= overlap_ratio * min_width:
                blocks[i].append(r)
                boxes[i] = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1))
                break
        else:
            blocks.append([r])
            boxes.append((x0, y0, x1, y1))

    # Pass 2 — within each block, split lines into sentence-chunks.
    chunks: list[OcrRegion] = []
    for lines in blocks:
        current: list[OcrRegion] = []
        for line in lines:
            current.append(line)
            if _completes_sentence("".join(l.text for l in current)):
                chunks.append(_merge(current))
                current = []
        if current:  # trailing lines with no closing terminator (casual speech)
            chunks.append(_merge(current))

    return chunks + passthrough
