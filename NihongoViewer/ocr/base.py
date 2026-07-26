"""Common interface for OCR engines (pipeline stage 2).

Each engine is swappable behind this ABC — the pipeline only ever talks to an
`OcrEngine`, never a concrete class. Keep this deliberately small: one method to
(lazily) load model weights, one to recognize text on a frame.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from PIL import Image

# A bounding box in captured-image pixels: (x0, y0, x1, y1), origin top-left.
Box = tuple[int, int, int, int]

# Speed/quality presets the user picks in "Configure OCR". Each engine maps these
# to its own knobs (see `apply_speed`); the names and order are the UI's source of
# truth. "balanced" is the default — "fast" (least upscale) is quicker but its
# detector drops whole low-contrast/short lines (measured ~10% miss on a VN
# backlog), which is worse than a little latency for a translator; "accurate"
# upscales more for a marginal gain at noticeably more CPU.
SPEED_PRESETS = ("fast", "balanced", "accurate")
DEFAULT_SPEED = "balanced"


@dataclass
class OcrRegion:
    """One detected text region and where it sits in the captured frame."""

    text: str
    box: Box | None = None  # None when the engine reports no location


@dataclass
class OcrResult:
    """All text found in a frame, with per-region locations when available."""

    regions: list[OcrRegion] = field(default_factory=list)

    @property
    def text(self) -> str:
        """All region text joined into one block ("" if nothing detected)."""
        return "\n".join(r.text for r in self.regions if r.text)


class OcrEngine(ABC):
    """A selectable OCR backend that turns a captured frame into Japanese text."""

    #: Human-readable name shown in the UI / status row.
    name: str = "OCR"

    @abstractmethod
    def load(self) -> None:
        """Load model weights into memory.

        Called when the engine is selected so the (potentially slow) first-run
        model download / init happens up front rather than on the first frame.
        Must be idempotent — calling it again once loaded is a no-op.
        """

    @abstractmethod
    def recognize(self, image: Image.Image) -> OcrResult:
        """Return the Japanese text (and locations) found in `image`."""

    def apply_speed(self, preset: str) -> None:
        """Tune the speed/quality tradeoff to one of `SPEED_PRESETS`, live.

        Adjusts only per-frame knobs (upscale, detection size, caching) — it must
        NOT reload weights, so the pipeline can retune without a stage restart.
        Default is a no-op for engines that don't expose a tradeoff.
        """
