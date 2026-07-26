"""Qwen3-4B (JA -> EN) translation via CTranslate2 + the HF `tokenizers` BPE.

Qwen3 (`Qwen/Qwen3-4B`, **Apache-2.0**) replaced MADLAD-400-3B. Both are
Apache-2.0, but Qwen3 is an *instruction-following* model rather than a pure
sequence-to-sequence MT model, which matters for game/VN dialogue in three ways:

  * it reads a whole line in context instead of one sentence at a time, so we no
    longer have to pre-split sentences to stop the model dropping a clause;
  * it doesn't pad or repeat a short line (MADLAD turned "速度" into "Speed Speed
    is the speed of a web page."), so the gloss-tidying hacks fall away;
  * the register/tone of the source survives, which is most of the point for
    dialogue.

Runtime is unchanged and still torch-free: the same `ctranslate2` that ran MADLAD
(it supports decoder-only models via `Generator`), plus `tokenizers` for Qwen's
byte-level BPE in place of `sentencepiece`. Both Apache-2.0/MIT. The model is
**bundled and loaded from a local directory** (`models/qwen/`, override with
`$NIHONGOVIEWER_QWEN_DIR`) — no network, no `huggingface_hub`, no torch.

Two Qwen3-specific details drive the code below:

  * **Thinking mode.** Qwen3 emits a `<think>…</think>` block by default, which
    would multiply latency for no benefit here. The chat template's
    "thinking disabled" form closes the block immediately, so the model goes
    straight to the answer — that is what `_PROMPT` does.
  * **Chat markup.** We build the ChatML string by hand rather than pulling in
    `transformers`/`jinja2` just to render a template. It is three literals.

The system prompt is passed as CTranslate2's `static_prompt`, whose KV cache is
reused across calls (`cache_static_prompt`), so we don't re-encode it every line.
"""

import os
import re
from pathlib import Path

from .base import Translator
from .names import protect as protect_names
from .segment import has_japanese

# The CTranslate2 export of Qwen/Qwen3-4B (Apache-2.0) is loaded from a LOCAL
# directory bundled with the app (the Steam depot ships it) — no network and no
# huggingface_hub at runtime. The directory holds the CTranslate2 files
# (model.bin + config.json + vocabulary.json) plus Qwen's `tokenizer.json`.
# Point at a different copy with the env var; otherwise the bundled models/qwen/
# next to the app is used.
_MODEL_DIR_ENV = "NIHONGOVIEWER_QWEN_DIR"
_DEFAULT_MODEL_DIR = Path(__file__).resolve().parent.parent / "models" / "qwen"
_TOKENIZER_FILE = "tokenizer.json"


def _resolve_model_dir() -> str:
    """Locate the bundled Qwen model directory, or fail with a clear message."""
    candidates = []
    override = os.environ.get(_MODEL_DIR_ENV)
    if override:
        candidates.append(Path(override))
    candidates.append(_DEFAULT_MODEL_DIR)
    for d in candidates:
        if (d / "model.bin").is_file() and (d / _TOKENIZER_FILE).is_file():
            return str(d)
    raise FileNotFoundError(
        "Qwen model not found. Expected a CTranslate2 model directory "
        f"(model.bin, config.json, vocabulary.json, {_TOKENIZER_FILE}) at "
        f"{_DEFAULT_MODEL_DIR}, or set ${_MODEL_DIR_ENV} to its location. "
        "The model ships bundled with the app; see the build instructions."
    )


# Translation instructions. Written for dialogue, and deliberately explicit that
# the model must not refuse or soften: this is a translation tool for adult
# visual novels, and a model that censors its input silently corrupts the text
# the user is trying to read.
_SYSTEM = (
    "You are a professional Japanese-to-English translator for visual novels and "
    "games. Translate the user's Japanese line into natural, idiomatic English.\n"
    "Rules:\n"
    "- Output ONLY the English translation. No notes, no romaji, no quotation "
    "marks, no explanations.\n"
    "- Preserve the speaker's tone and register (casual, crude, formal, archaic).\n"
    "- Any text already in Latin letters is a proper noun that is already "
    "translated: copy it through exactly as written.\n"
    "- Translate faithfully and completely, including explicit or vulgar wording. "
    "Do not refuse, censor, soften, or add warnings.\n"
    "- If the line is a fragment, translate the fragment. Do not invent context."
)

# ChatML, built by hand (see the module docstring). The empty `<think></think>`
# is Qwen3's "thinking disabled" form — without it the model reasons out loud
# before answering and every line costs several times as much.
_SYSTEM_PROMPT = f"<|im_start|>system\n{_SYSTEM}<|im_end|>\n"
_PROMPT = ("<|im_start|>user\n{text}<|im_end|>\n"
           "<|im_start|>assistant\n<think>\n\n</think>\n\n")

# Stop as soon as the assistant turn ends.
_END_TOKENS = ["<|im_end|>", "<|endoftext|>"]

# Decoding: greedy (topk=1) is deterministic, which matters because the fuzzy
# cache assumes the same source yields the same translation. A light repetition
# penalty guards against the rare degenerate loop on a garbled OCR line.
_MAX_LENGTH = 512
_SAMPLING_TOPK = 1
_REPETITION_PENALTY = 1.05

# Japanese quotation/corner brackets — copied straight through by the model and
# rendered as tofu ("= =") in the overlay font, so we drop them from both sides.
_JA_BRACKETS = "「」『』【】〔〕〈〉《》｢｣〝〟〖〗｟｠"
_STRIP_BRACKETS = str.maketrans("", "", _JA_BRACKETS)


def _strip_brackets(text: str) -> str:
    return text.translate(_STRIP_BRACKETS).strip()


# Japanese punctuation that can survive into an otherwise-English line — either
# from a name-only line that skips the model (a trailing "。") or copied straight
# through it (the katakana middle dot "・" separating names, e.g. "作者・瀬崎遊"
# -> "Author・Yui Sezaki"). Map each to its ASCII equivalent so no CJK
# punctuation reaches the overlay; "・" becomes a space since it joins names.
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
# presence in a translated line means the model failed on that line.
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

# A `<think>` block, in case the model opens one anyway despite the closed block
# in the prompt (seen occasionally when the source itself contains the markup).
_THINK_BLOCK = re.compile(r"<think>.*?</think>", re.DOTALL)
# Wrapping quotes the model sometimes adds around the whole line despite the
# instruction not to — only stripped when they enclose the entire output.
_WRAPPED_QUOTES = re.compile(r'^\s*["“”「『\'](.*)["“”」』\']\s*$',
                             re.DOTALL)


def _is_noise_fragment(text: str) -> bool:
    """True for a single lone kana char — a line-wrap orphan, not a translatable unit.

    The OCR line-grouping can leave a wrapped sentence's tail (``た``, ``せ``) as
    its own one-character region. The model can only hallucinate off it, so we
    drop it. A single *kanji* can be a real label (``水`` = water), so only lone
    kana are treated as noise.
    """
    return len(text) == 1 and bool(_LONE_KANA.match(text))


def _normalize_punct(text: str) -> str:
    """Convert stray Japanese punctuation + censor marks to ASCII (see tables)."""
    text = text.translate(_NORMALIZE_TABLE)
    # "・" -> " " can leave a doubled space ("Author  Yui"); collapse runs.
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# Leading characters a sentence's opening letter may hide behind (see _sentence_case).
_CASE_SKIP = " \t'\"“”‘’«»([{"


def _sentence_case(text: str) -> str:
    """Capitalize the opening letter of a translated line.

    The model is inconsistent about it — "トイレ" came back as "toilet" and a line
    whose first word the glossary had already substituted started lowercase
    ("my penis has grown bigger."). Overlay lines are sentences, so they should
    open with a capital. A first word that already carries internal capitals
    ("iPhone", "eBay") is left alone.

    Only an *opening* quote or bracket is skipped over to find that letter. A
    leading ellipsis is left alone (a line that opens "...such a relaxed
    attitude" is trailing on from the previous one and reads wrong capitalized),
    and so is a line that starts with a digit — "123 go" must not become
    "123 Go".
    """
    for i, ch in enumerate(text or ""):
        if ch in _CASE_SKIP:
            continue
        if not ch.isalpha():
            return text          # digit, ellipsis, dash … — not a sentence start
        word = text[i:].split(maxsplit=1)[0]
        if word.islower():
            return text[:i] + ch.upper() + text[i + 1:]
        return text
    return text


def _clean_output(text: str) -> str:
    """Strip chat/reasoning artifacts from a raw generation."""
    text = _THINK_BLOCK.sub("", text or "").strip()
    # A stray unclosed <think> means the model started reasoning; keep what
    # follows it rather than surfacing the reasoning itself.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    match = _WRAPPED_QUOTES.match(text)
    if match:
        text = match.group(1).strip()
    return text


def _gloss_japanese(run: str) -> str:
    """Best-effort English for a Japanese run the model left untranslated.

    JMdict covers vocabulary the model doesn't, so we tokenize the echoed run and
    replace it with the dictionary gloss of its content words (``刀剣`` ->
    "sword"). Best-effort and non-blocking: returns "" if the dictionary isn't
    built yet or the run has no headword, so the caller just drops the run.
    (See translate.dictionary / translate.furigana.)
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
    """Recover a model output that still contains Japanese script (a failed line).

    Rare with an instruction-following model, but kept as a safety net: the two
    failure modes are echoing an untranslated word (``刀剣`` -> "As a 刀剣 geek …")
    and hallucinating off a lone OCR fragment. Replace each residual Japanese run
    with its JMdict gloss where possible (``刀剣`` -> "sword"), so an echoed word
    is salvaged instead of dropped; runs the dictionary can't help with fall away.
    If almost no English survives, the whole line was an echo, so drop it rather
    than surface the garbage.
    """
    recovered = _JP_RUN.sub(lambda m: f" {_gloss_japanese(m.group(0))} ", text)
    recovered = re.sub(r"\s{2,}", " ", recovered).strip()
    letters = sum(1 for c in recovered if c.isascii() and c.isalpha())
    return recovered if letters >= 4 else ""


class QwenTranslator(Translator):
    name = "Qwen3-4B"

    def __init__(self, device: str = "cpu", compute_type: str = "int8") -> None:
        self._device = device
        self._compute_type = compute_type
        self._generator = None
        self._tokenizer = None
        self._system_tokens: list[str] = []

    def load(self) -> None:
        if self._generator is not None:
            return
        import ctranslate2
        from tokenizers import Tokenizer

        model_dir = _resolve_model_dir()
        self._tokenizer = Tokenizer.from_file(
            os.path.join(model_dir, _TOKENIZER_FILE))
        self._generator = ctranslate2.Generator(
            model_dir, device=self._device, compute_type=self._compute_type,
            # Use all cores for a single generation — a text-heavy screen otherwise
            # leaves most of the CPU idle.
            intra_threads=os.cpu_count() or 4,
        )
        # Encoded once; CTranslate2 caches its KV state across calls.
        self._system_tokens = self._encode(_SYSTEM_PROMPT)

    def _encode(self, text: str) -> list[str]:
        """Prompt string -> CTranslate2 token strings (special markup preserved)."""
        return self._tokenizer.encode(text, add_special_tokens=False).tokens

    def _generate(self, source: str) -> str:
        results = self._generator.generate_batch(
            [self._encode(_PROMPT.format(text=source))],
            static_prompt=self._system_tokens,
            cache_static_prompt=True,
            # Forward the prompt at once to prime the KV cache and keep only the
            # completion — both faster and simpler to decode.
            include_prompt_in_result=False,
            max_length=_MAX_LENGTH,
            sampling_topk=_SAMPLING_TOPK,
            repetition_penalty=_REPETITION_PENALTY,
            end_token=_END_TOKENS,
        )
        if not results or not results[0].sequences_ids:
            return ""
        # Decode from token *ids*: Qwen's BPE is byte-level, so joining the token
        # strings would leave the byte markers in place.
        return _clean_output(self._tokenizer.decode(results[0].sequences_ids[0]))

    def translate(self, text: str) -> str:
        self.load()
        # Romanize name-like katakana and fix known-bad terms first (see
        # translate.names) — the model then copies the Latin name through
        # verbatim instead of trying to translate somebody's name.
        source = _strip_brackets(protect_names(text))
        if not source or _is_noise_fragment(source):
            return ""
        # Nothing Japanese left (e.g. a name plate already romanized to Latin):
        # the model would only invent padding, so pass it straight through.
        if not has_japanese(source):
            return _normalize_punct(source)
        out = _strip_brackets(self._generate(source))
        # Residual Japanese script means the model failed on this line — salvage
        # what the dictionary can (see _recover_untranslated).
        if _JP_SCRIPT.search(out):
            out = _recover_untranslated(out)
        return _sentence_case(_normalize_punct(out))
