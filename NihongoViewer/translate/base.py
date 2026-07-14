"""Common interface for translation backends (pipeline stage 3).

Like the OCR stage, the pipeline only ever talks to a `Translator` ABC so the
backend is swappable, even though we ship one implementation (MADLAD-400).
"""

import re
from abc import ABC, abstractmethod

#: Sentence-ending marks (JP + ASCII) used when glossing a single word.
_TERMINATORS = "。．.!?！？"


def _tidy_gloss(text: str) -> str:
    """Collapse the repetition a sentence-MT model emits when padding a word.

    A bare word makes these models pad ("天気" -> "Weather Weather forecast",
    "学生。" -> "Students. Students."). We drop consecutive duplicate sentences and
    words (case-insensitive) and strip the trailing sentence punctuation, leaving a
    concise dictionary-style gloss ("The weather", "Students").
    """
    text = (text or "").strip()
    if not text:
        return text
    # 1) Drop consecutive duplicate sentences ("Students. Students." -> "Students.").
    kept: list[tuple[str, str]] = []
    for part in re.split(r"(?<=[.!?。．！？])\s*", text):
        part = part.strip()
        if not part:
            continue
        norm = part.rstrip(_TERMINATORS).strip().lower()
        if kept and kept[-1][1] == norm:
            continue
        kept.append((part, norm))
    text = " ".join(p for p, _ in kept)
    # 2) Drop consecutive duplicate words ("Weather Weather forecast" -> "Weather forecast").
    out: list[str] = []
    for word in text.split():
        cw = re.sub(r"[^\w]", "", word).lower()
        if cw and out and re.sub(r"[^\w]", "", out[-1]).lower() == cw:
            continue
        out.append(word)
    # 3) A gloss shouldn't trail a sentence terminator.
    return " ".join(out).rstrip(" .!?。．！？,;:").strip()


class Translator(ABC):
    """A selectable JA -> EN translation backend."""

    #: Human-readable name shown in the UI / status row.
    name: str = "Translator"

    @abstractmethod
    def load(self) -> None:
        """Load model weights (idempotent). Done up front when selected."""

    @abstractmethod
    def translate(self, text: str) -> str:
        """Translate Japanese `text` to English ("" for empty input)."""

    def translate_word(self, text: str) -> str:
        """Translate a single vocabulary word/term to a concise gloss.

        Sentence-level MT models pad a bare word into a whole clause ("学生" ->
        "Students are students."). Appending a sentence terminator coaxes a short,
        complete rendering, and `_tidy_gloss` removes the leftover repetition — so
        the Create-card Word field gets "Student(s)", not a padded sentence. Goes
        through `translate()` (hence the fuzzy cache when wrapped)."""
        core = (text or "").strip()
        if not core:
            return ""
        src = core if core[-1] in _TERMINATORS else core + "。"
        return _tidy_gloss(self.translate(src))
