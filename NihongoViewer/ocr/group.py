"""Turn per-line OCR regions into sentence-chunks for translation + placement.

The OCR engine returns one region per *physical line*. Two things go wrong if we
treat each line as a translation unit:

  * a sentence that wraps across lines ("本当にただの" / "学生?") gets translated in
    fragments — mangled or half-dropped;
  * conversely, translating a whole multi-line block as one unit and drawing it in
    one place reads as if only part of the text was translated (the English bunches
    up next to one line).

So we group vertically-adjacent, horizontally-overlapping lines into a **block**
(a name label or far menu button stays its own block, since it doesn't overlap the
paragraph's column). Each block becomes **one** region — its text is the merged
(de-wrapped) source, its box the union of its lines — so a whole paragraph is
translated *and* drawn as a single unit. The overlay wraps that translation and
grows its box to fit (see `overlay._render_fitted`), which keeps a paragraph one
coherent box instead of fragmenting it into mismatched per-sentence boxes.

A line that starts with a bullet glyph ("・種族：…") is treated as a standalone
list item: it never merges, so a column of "・…" topics stays one box each rather
than collapsing into a single paragraph block.

(The translator still splits the merged text into one sentence per model call
internally — see `translate.segment` — so translation quality is unaffected; this
grouping only decides how text is boxed for display.)

Japanese has no inter-word spaces, so wrapped lines are joined with **no**
separator. Regions without a box pass through.
"""

from .base import OcrRegion

# A candidate line joins a block when the vertical gap to the block's current
# bottom is at most this fraction of the line's height. Kept comfortably above 1.0
# so a short trailing line (e.g. "徒までいた。", whose glyph height is a little
# smaller) still merges at normal line spacing, while staying well under the much
# larger blank gap between separate paragraphs.
_GAP_RATIO = 1.3
# ...and the block covers at least this fraction of the CANDIDATE's horizontal
# span. Measuring against the candidate (not the narrower of the two) is what
# tells a wrapped line from a label: a continuation line lies *inside* the
# paragraph's column, so the block covers nearly all of it, while the paragraph
# line under a short name plate sticks out far past the plate on both sides.
# Using the narrower span made a 99px name label swallow the 768px line below it
# (79px of overlap is 80% of the label but only 10% of the line).
_OVERLAP_RATIO = 0.3

# --- split-line repair -----------------------------------------------------
# The detector cuts a very long line into two side-by-side boxes: the 1238px
# dialogue line "『意識を変えることで、…信じることは" comes back as 26..794 plus
# 772..1264 at the same y. The block pass below only ever stacks lines top to
# bottom, so those fragments land in different blocks — the sentence is torn in
# half and reassembled out of order. We stitch them back together first.
#
# Two fragments are the same physical line when their vertical spans overlap by
# at least this fraction of the shorter one...
_LINE_OVERLAP_RATIO = 0.5
# ...and the horizontal gap between them is no wider than this fraction of the
# line height. A split is adjacent or slightly overlapping; a genuinely separate
# element on the same row (a far menu button) sits well beyond that.
_LINE_GAP_RATIO = 0.5

# Line-initial bullet glyphs. A line that starts with one is a standalone list
# item ("・種族：ウィッチ" — a labelled topic), so it is NOT merged into the
# paragraph above or into its sibling bullets: each becomes its own box. We only
# look at the first character, so a mid-string "・" (the name separator in
# "サフィア・ランカスター") is unaffected.
_BULLETS = "・•·●○◦‣▪▫◆◇■□★☆※"


def _is_bullet(text: str) -> bool:
    """True if `text`'s first non-space character is a bullet glyph."""
    t = text.lstrip()
    return bool(t) and t[0] in _BULLETS


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


def _join_line_fragments(boxed: list[OcrRegion]) -> list[OcrRegion]:
    """Stitch a physical line the detector split into side-by-side boxes.

    Input must already be in reading order, so fragments of one line arrive
    left-to-right and the rejoined text keeps its order. See the
    `_LINE_OVERLAP_RATIO` comment for why this has to run before the block pass.
    """
    lines: list[list[OcrRegion]] = []
    spans: list[tuple] = []          # running (y0, y1, x1) per line
    for r in boxed:
        x0, y0, x1, y1 = r.box
        height = max(1, y1 - y0)
        for i, (ly0, ly1, lx1) in enumerate(spans):
            shared = min(y1, ly1) - max(y0, ly0)
            shortest = min(height, max(1, ly1 - ly0))
            if (shared >= _LINE_OVERLAP_RATIO * shortest
                    and x0 - lx1 <= _LINE_GAP_RATIO * height):
                lines[i].append(r)
                spans[i] = (min(ly0, y0), max(ly1, y1), max(lx1, x1))
                break
        else:
            lines.append([r])
            spans.append((y0, y1, x1))
    # Sort each line's fragments by x before merging: the incoming order is by
    # (y0, x0), so a fragment whose box starts a few px lower — common, since the
    # detector fits each piece independently — would otherwise be appended last
    # and land at the end of the line's text.
    return [group[0] if len(group) == 1
            else _merge(sorted(group, key=lambda r: r.box[0]))
            for group in lines]


def group_lines(regions, *, gap_ratio: float = _GAP_RATIO,
                overlap_ratio: float = _OVERLAP_RATIO) -> list[OcrRegion]:
    """Merge line-regions into sentence-chunks (see module docstring)."""
    boxed = [r for r in regions if r.box]
    passthrough = [r for r in regions if not r.box]
    # Reading order: top-to-bottom, then left-to-right.
    boxed.sort(key=lambda r: (r.box[1], r.box[0]))
    # Pass 0 — rejoin any physical line the detector split horizontally, so the
    # block pass below sees one region per line as it expects.
    boxed = _join_line_fragments(boxed)

    # Pass 1 — geometric blocks (each a list of line-regions in reading order).
    blocks: list[list[OcrRegion]] = []
    boxes: list[tuple] = []  # running union box per block, parallel to `blocks`
    for r in boxed:
        x0, y0, x1, y1 = r.box
        height = max(1, y1 - y0)
        # A bulleted topic line starts its own block and never merges — so a list
        # of "・…" items stays one box each instead of collapsing into a paragraph.
        joined = False
        if not _is_bullet(r.text):
            for i, (bx0, by0, bx1, by1) in enumerate(boxes):
                gap = y0 - by1  # >0 below the block, <0 overlapping it vertically
                overlap = min(x1, bx1) - max(x0, bx0)
                width = max(1, x1 - x0)  # the CANDIDATE's span — see _OVERLAP_RATIO
                if -0.5 * height <= gap <= gap_ratio * height and overlap >= overlap_ratio * width:
                    blocks[i].append(r)
                    boxes[i] = (min(bx0, x0), min(by0, y0), max(bx1, x1), max(by1, y1))
                    joined = True
                    break
        if not joined:
            blocks.append([r])
            boxes.append((x0, y0, x1, y1))

    # Pass 2 — each block's lines are de-wrapped and merged into one region, so a
    # paragraph is boxed (and drawn) as a single unit rather than per sentence.
    chunks = [_merge(lines) for lines in blocks]

    return chunks + passthrough
