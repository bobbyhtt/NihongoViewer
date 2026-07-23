"""MADLAD-400-3B (JA -> EN) translation via CTranslate2 + SentencePiece.

MADLAD-400 (`google/madlad400-3b-mt`, **Apache-2.0** — the cleanest commercial
license of the options we tried) is a 3B T5 multilingual MT model and clearly the
most fluent JA->EN of the models we evaluated. We run the community int8 CTranslate2
export `Nextcloud-AI/madlad400-3b-mt-ct2-int8` — the runtime stays the light
`ctranslate2` + `sentencepiece` stack, no torch ever, and no conversion step. The
model is **bundled with the app and loaded from a local directory** (`models/madlad/`,
or `$NIHONGOVIEWER_MADLAD_DIR`) — no network and no `huggingface_hub` at runtime.

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
import re
from pathlib import Path

from .base import Translator, _tidy_gloss
from .names import protect as protect_names
from .segment import has_japanese, segment

# The int8 CTranslate2 export of google/madlad400-3b-mt (Apache-2.0) is loaded
# from a LOCAL directory bundled with the app (the Steam depot ships it) — no
# network and no huggingface_hub at runtime. The directory holds the CTranslate2
# files (model.bin + config.json + shared_vocabulary.json) plus the shared
# SentencePiece vocab (spiece.model). Point at a different copy with the env var;
# otherwise the bundled models/madlad/ next to the app is used.
_MODEL_DIR_ENV = "NIHONGOVIEWER_MADLAD_DIR"
_DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "madlad"


def _resolve_model_dir() -> str:
    """Locate the bundled MADLAD model directory, or fail with a clear message."""
    candidates = []
    override = os.environ.get(_MODEL_DIR_ENV)
    if override:
        candidates.append(Path(override))
    candidates.append(_DEFAULT_MODEL_DIR)
    for d in candidates:
        if (d / "model.bin").is_file():
            return str(d)
    raise FileNotFoundError(
        "MADLAD model not found. Expected a CTranslate2 model directory "
        "(model.bin, config.json, shared_vocabulary.json, spiece.model) at "
        f"{_DEFAULT_MODEL_DIR}, or set ${_MODEL_DIR_ENV} to its location. "
        "The model ships bundled with the app; see the build instructions."
    )

# Target-language prefix token MADLAD prepends to the source ("<2en>" => English).
# It's a genuine piece in the shared vocab, so it survives SentencePiece encoding.
_TGT_PREFIX = "<2en>"

# Decoding knobs that suppress MADLAD's tendency to repeat/pad short UI lines.
_MAX_DECODING_LENGTH = 256
_REPETITION_PENALTY = 1.1
_NO_REPEAT_NGRAM_SIZE = 3
# Greedy decoding (beam 1) is ~2x faster than beam 2 on CPU with negligible quality
# loss here — the real latency cost of a text-heavy screen — so we favor speed.
_BEAM_SIZE = 1

# Japanese quotation/corner brackets — copied straight through by the model and
# rendered as tofu ("= =") in the overlay font, so we drop them from both sides.
_JA_BRACKETS = "「」『』【】〔〕〈〉《》｢｣〝〟〖〗｟｠"
_STRIP_BRACKETS = str.maketrans("", "", _JA_BRACKETS)


def _strip_brackets(text: str) -> str:
    return text.translate(_STRIP_BRACKETS).strip()


# Japanese punctuation that can survive into an otherwise-English line — either
# from a name-only segment that skips the model (a trailing "。") or copied
# straight through the model (the katakana middle dot "・" separating names, e.g.
# "作者・瀬崎遊" -> "Author・Yui Sezaki"). Map each to its ASCII equivalent so no
# CJK punctuation reaches the overlay; "・" becomes a space since it joins names.
_JA_PUNCT = {
    "。": ".", "．": ".", "、": ",", "，": ",",
    "！": "!", "？": "?", "：": ":", "；": ";",
    "・": " ", "／": "/", "〜": "~", "～": "~",
}
# Censor placeholders: "〇〇装置" is a deliberately blanked-out word ("[blank]
# device"). The model copies "〇" (U+3007) through untranslated; render each as one
# "X" so the redaction shows as "XX" and no CJK symbol reaches the overlay. A
# per-char 1:1 map keeps the count (three "〇〇〇" -> "XXX"). Circle variants too.
_CENSOR = {"〇": "X", "○": "X", "◯": "X"}
_NORMALIZE_TABLE = str.maketrans({**_JA_PUNCT, **_CENSOR})

# Kana / kanji / halfwidth-katakana — real Japanese *script* (not punctuation). Its
# presence in a translated line means the model failed on that segment.
_JP_SCRIPT = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ]")
# Maximal runs of Japanese script — the unit we try to salvage via the dictionary.
_JP_RUN = re.compile(r"[぀-ヿ㐀-鿿ｦ-ﾟ々〆〇]+")
# Only kanji-bearing tokens are salvaged inline (see _gloss_japanese): a word the
# model echoes untranslated is a rare kanji compound (刀剣), whereas a leftover
# kana fragment (せた) is a hallucination or grammatical bit whose "dictionary
# entry" is an aux-verb description — glossing that is worse than dropping it.
_HAS_KANJI = re.compile(r"[一-鿿々〆〇ヶ]")

# A lone kana character (hiragana / katakana / halfwidth katakana).
_LONE_KANA = re.compile(r"[぀-ゟ゠-ヿｦ-ﾟ]")


def _is_noise_fragment(text: str) -> bool:
    """True for a single lone kana char — a line-wrap orphan, not a translatable unit.

    The OCR line-grouping can leave a wrapped sentence's tail (``た``, ``せ``) as
    its own one-character segment. The model can only hallucinate off it ("た" ->
    "1980s 2000s"), so we drop it. A single *kanji* can be a real label (``水`` =
    water), so only lone kana are treated as noise.
    """
    return len(text) == 1 and bool(_LONE_KANA.match(text))


# Sentence terminators / separators. A short segment with none of these and no
# spaces is a bare word or label — no sentence structure for the model to lean on.
_SENTENCE_PUNCT = "。．！？!?、，,：:；;・…"


def _looks_like_word(text: str) -> bool:
    """True for a short, structureless source segment — a bare word or label.

    MADLAD pads a lone word into a repetitive clause ("速度" -> "Speed Speed is the
    speed of a web page.", "トイレ" -> "Toilet Toilet", "眠る" -> "Sleeping sleeper.").
    For those we coax a concise gloss instead — append a terminator and dedupe the
    output, the same trick `translate_word` uses (see `translate`).
    """
    return (0 < len(text) <= 8
            and " " not in text
            and not any(c in _SENTENCE_PUNCT for c in text))


def _normalize_punct(text: str) -> str:
    """Convert stray Japanese punctuation + censor marks to ASCII (see tables)."""
    text = text.translate(_NORMALIZE_TABLE)
    # "・" -> " " can leave a doubled space ("Author  Yui"); collapse runs.
    return re.sub(r"[ \t]{2,}", " ", text).strip()


def _gloss_japanese(run: str) -> str:
    """Best-effort English for a Japanese run the model left untranslated.

    JMdict covers vocabulary the MT model doesn't, so we tokenize the echoed run
    and replace it with the dictionary gloss of its content words (``刀剣`` ->
    "sword"). Best-effort and non-blocking: returns "" if the dictionary isn't
    built yet or the run has no headword, so the caller just drops the run as
    before. (See translate.dictionary / translate.furigana.)
    """
    try:
        from . import dictionary, furigana
        parts: list[str] = []
        for tok in furigana.tokens(run):
            # Only salvage kanji content words — a kana fragment's "entry" is a
            # grammatical aux description, worse than dropping it.
            if not tok.get("lookup") or not _HAS_KANJI.search(tok.get("surface", "")):
                continue
            g = dictionary.gloss(tok.get("query") or tok.get("surface", ""),
                                 tok.get("reading") or None,
                                 tok.get("pos") or None, max_glosses=1)
            if g:
                parts.append(g)
        return " ".join(parts)
    except Exception:
        return ""


def _recover_untranslated(text: str) -> str:
    """Recover a model output that still contains Japanese script (a failed segment).

    Two failure modes leak script into the "English": the model echoes an
    untranslated word (``刀剣`` -> "As a 刀剣 geek …"), or a lone OCR fragment makes
    it hallucinate (``せた`` -> "2017-09-16 15:38:45 - せたせた!"). Replace each residual
    Japanese run with its JMdict gloss where possible (``刀剣`` -> "sword"), so an
    echoed word is salvaged instead of dropped; runs the dictionary can't help
    with fall away. If almost no English survives, the whole line was an echo /
    hallucination, so drop it rather than surface the garbage.
    """
    recovered = _JP_RUN.sub(lambda m: f" {_gloss_japanese(m.group(0))} ", text)
    recovered = re.sub(r"\s{2,}", " ", recovered).strip()
    letters = sum(1 for c in recovered if c.isascii() and c.isalpha())
    return recovered if letters >= 4 else ""


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

        model_dir = _resolve_model_dir()
        self._sp = spm.SentencePieceProcessor(
            os.path.join(model_dir, "spiece.model")
        )
        self._translator = ctranslate2.Translator(
            model_dir, device=self._device, compute_type=self._compute_type,
            # Use all cores for a single translation — a text-heavy screen otherwise
            # leaves most of the CPU idle (measured ~10% faster than the default).
            intra_threads=os.cpu_count() or 4,
        )

    def translate(self, text: str) -> str:
        self.load()
        # Romanize name-like katakana first (ヤツシロ -> Yatsushiro) so the model
        # doesn't mistranslate proper nouns (see translate.names). Then one
        # sentence per segment (translate.segment): a long multi-sentence line
        # makes the model drop a clause, so we split first and batch them.
        segments = [_strip_brackets(s) for s in segment(protect_names(text))]
        # Drop empties and lone-kana wrap orphans (the model only hallucinates off
        # a single "た"/"せ" — see _is_noise_fragment).
        segments = [s for s in segments if s and not _is_noise_fragment(s)]
        if not segments:
            return ""
        # A segment with no Japanese left (e.g. a name already romanized to Latin)
        # would only make the model hallucinate padding — pass it through as-is.
        outputs: list[str | None] = [None if has_japanese(s) else s for s in segments]
        todo = [(i, s) for i, s in enumerate(segments) if outputs[i] is None]
        if todo:
            # A bare word makes MADLAD pad/repeat ("速度" -> "Speed Speed is the
            # speed of a web page."). For those segments, append a terminator to
            # coax a complete short rendering and dedupe the result (`_tidy_gloss`),
            # so a single word comes out concise instead of doubled/tripled.
            words = [_looks_like_word(s) for _, s in todo]
            batch = [
                self._sp.encode(f"{_TGT_PREFIX} {s}" + ("。" if w else ""), out_type=str)
                for (_, s), w in zip(todo, words)
            ]
            results = self._translator.translate_batch(
                batch,
                max_decoding_length=_MAX_DECODING_LENGTH,
                repetition_penalty=_REPETITION_PENALTY,
                no_repeat_ngram_size=_NO_REPEAT_NGRAM_SIZE,
                beam_size=_BEAM_SIZE,
            )
            for (i, _), r, w in zip(todo, results, words):
                out = _strip_brackets(self._sp.decode(r.hypotheses[0]))
                # Residual Japanese script means the model failed on this segment
                # (echoed a word, or hallucinated off a lone fragment) — recover it.
                if _JP_SCRIPT.search(out):
                    out = _recover_untranslated(out)
                if w:  # a bare word — collapse the model's padding to a gloss
                    out = _tidy_gloss(out)
                outputs[i] = out
        # Sentences of one block read as one utterance — join with a space, then
        # normalize any stray Japanese punctuation left by the skip-path or model.
        return _normalize_punct(" ".join(o for o in outputs if o))
