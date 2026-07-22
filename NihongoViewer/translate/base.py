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
    "学生。" -> "Students. Students.", "ありがとう" -> "Thank you, thank you"). We drop
    consecutive duplicate clauses (split on sentence marks AND commas, since the
    padding often repeats after a comma) and consecutive duplicate words
    (case-insensitive), then strip trailing punctuation — leaving a concise
    dictionary-style gloss ("The weather", "Students", "Thank you").
    """
    text = (text or "").strip()
    if not text:
        return text
    # 1) Drop consecutive duplicate clauses ("Students. Students." -> "Students.",
    #    "Thank you, thank you" -> "Thank you"). Comma-split too: the model repeats
    #    a short gloss after a comma as often as after a full stop.
    kept: list[tuple[str, str]] = []
    for part in re.split(r"(?<=[.!?。．！？,、，])\s*", text):
        part = part.strip()
        if not part:
            continue
        norm = part.rstrip(_TERMINATORS + ",、，").strip().lower()
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

        JMdict is authoritative for vocabulary, so a word that's a dictionary
        headword gets its real gloss straight from JMdict — a sentence-MT model
        romanizes rare or compound words it doesn't know (足コキ -> "Foot Koki",
        where JMdict has "footjob"). Only non-headwords (names, phrases, novel
        coinages) fall back to the model.

        The model path: sentence-level MT pads a bare word into a whole clause
        ("学生" -> "Students are students."). Appending a sentence terminator coaxes
        a short, complete rendering, and `_tidy_gloss` removes the leftover
        repetition. Goes through `translate()` (hence the fuzzy cache when wrapped).
        """
        core = (text or "").strip()
        if not core:
            return ""
        # 1) Dictionary-first (best-effort, non-blocking — see dictionary.gloss).
        try:
            from . import dictionary
            gloss = dictionary.gloss(core)
        except Exception:
            gloss = ""
        # 2) Not a headword — let the model gloss it (the glossary in names.protect
        #    still corrects terms the model mangles, e.g. パイスリ -> "breast rubbing").
        if not gloss:
            src = core if core[-1] in _TERMINATORS else core + "。"
            gloss = _tidy_gloss(self.translate(src))
        # Capitalize like a translation ("breast rubbing" -> "Breast rubbing"); the
        # in-sentence path keeps the lowercase glossary/dictionary form.
        return gloss[:1].upper() + gloss[1:] if gloss else gloss
