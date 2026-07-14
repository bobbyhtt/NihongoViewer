"""MADLAD-400-3B (JA -> EN) translation via CTranslate2 + SentencePiece.

MADLAD-400 (`google/madlad400-3b-mt`, **Apache-2.0** — the cleanest commercial
license of the options we tried) is a 3B T5 multilingual MT model and clearly the
most fluent JA->EN of the models we evaluated. We run the community int8 CTranslate2
export `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (~1.65 GB) — the runtime stays the
light `ctranslate2` + `sentencepiece` stack, no torch ever, and no conversion step.
Weights download on first run into the standard Hugging Face cache.

It's a T5 with a single *shared* SentencePiece vocab (`spiece.model`) used for both
encoding and decoding, and each source sentence is prefixed with a `<2en>`
target-language token (a real piece in the vocab).

MADLAD tends to repeat/pad short lines, so we decode with a light repetition
penalty + no-repeat-ngram, which removes the duplicated-sentence artifact without
hurting fluency.

Trade-off: at 3B it's heavy — ~0.5-2 s per *new* line on CPU and ~2 GB RAM. The
pipeline's `FuzzyCache` means only genuinely new text pays that; repeated or
near-identical lines are free.
"""

import os

from .base import Translator
from .names import protect as protect_names
from .segment import has_japanese, segment

# int8 CTranslate2 export of google/madlad400-3b-mt (Apache-2.0). Ready to use —
# no torch conversion. Override with a different CT2 repo via the env var below.
_REPO = os.environ.get(
    "NIHONGOVIEWER_MADLAD_CT2_REPO", "Nextcloud-AI/madlad400-3b-mt-ct2-int8"
)

# Target-language prefix token MADLAD prepends to the source ("<2en>" => English).
# It's a genuine piece in the shared vocab, so it survives SentencePiece encoding.
_TGT_PREFIX = "<2en>"

# Decoding knobs that suppress MADLAD's tendency to repeat/pad short UI lines.
_MAX_DECODING_LENGTH = 256
_REPETITION_PENALTY = 1.1
_NO_REPEAT_NGRAM_SIZE = 3
_BEAM_SIZE = 2

# Japanese quotation/corner brackets — copied straight through by the model and
# rendered as tofu ("= =") in the overlay font, so we drop them from both sides.
_JA_BRACKETS = "「」『』【】〔〕〈〉《》｢｣〝〟〖〗｟｠"
_STRIP_BRACKETS = str.maketrans("", "", _JA_BRACKETS)


def _strip_brackets(text: str) -> str:
    return text.translate(_STRIP_BRACKETS).strip()


class MadladTranslator(Translator):
    name = "MADLAD-400"

    def __init__(self, device: str = "cpu", compute_type: str = "int8") -> None:
        self._device = device
        self._compute_type = compute_type
        self._translator = None
        self._sp = None  # single shared SentencePiece model (encode + decode)

    def load(self) -> None:
        if self._translator is not None:
            return
        import ctranslate2
        import sentencepiece as spm
        from huggingface_hub import snapshot_download

        model_dir = snapshot_download(_REPO)
        self._sp = spm.SentencePieceProcessor(
            os.path.join(model_dir, "spiece.model")
        )
        self._translator = ctranslate2.Translator(
            model_dir, device=self._device, compute_type=self._compute_type
        )

    def translate(self, text: str) -> str:
        self.load()
        # Romanize name-like katakana first (ヤツシロ -> Yatsushiro) so the model
        # doesn't mistranslate proper nouns (see translate.names). Then one
        # sentence per segment (translate.segment): a long multi-sentence line
        # makes the model drop a clause, so we split first and batch them.
        segments = [_strip_brackets(s) for s in segment(protect_names(text))]
        segments = [s for s in segments if s]
        if not segments:
            return ""
        # A segment with no Japanese left (e.g. a name already romanized to Latin)
        # would only make the model hallucinate padding — pass it through as-is.
        outputs: list[str | None] = [None if has_japanese(s) else s for s in segments]
        todo = [(i, s) for i, s in enumerate(segments) if outputs[i] is None]
        if todo:
            batch = [self._sp.encode(f"{_TGT_PREFIX} {s}", out_type=str) for _, s in todo]
            results = self._translator.translate_batch(
                batch,
                max_decoding_length=_MAX_DECODING_LENGTH,
                repetition_penalty=_REPETITION_PENALTY,
                no_repeat_ngram_size=_NO_REPEAT_NGRAM_SIZE,
                beam_size=_BEAM_SIZE,
            )
            for (i, _), r in zip(todo, results):
                outputs[i] = _strip_brackets(self._sp.decode(r.hypotheses[0]))
        # Sentences of one block read as one utterance — join with a space.
        return " ".join(o for o in outputs if o).strip()
