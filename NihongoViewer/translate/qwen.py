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
from .segment import has_japanese, split_sentences as _split_sentences

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

# ChatML, built by hand (see the module docstring). Two belt-and-braces signals
# turn Qwen3's reasoning OFF:
#   * the empty `<think></think>` block (Qwen3's "thinking disabled" template form);
#   * the `/no_think` soft switch appended to the user turn.
# The empty block ALONE was not enough: for some inputs the model ignored it and
# generated a full chain-of-thought in the answer anyway — e.g. "いいよ" produced
# ~360 tokens of reasoning (~66 s on CPU) before the one-word answer, and the same
# reasoning occasionally leaked into the output. Adding `/no_think` to the user
# turn reliably suppresses it (66 s -> 0.7 s on that input), while `/no_think` in
# the SYSTEM prompt did not. Placed after the text (not before) so it never reads
# as part of the line to translate.
_SYSTEM_PROMPT = f"<|im_start|>system\n{_SYSTEM}<|im_end|>\n"
_PROMPT = ("<|im_start|>user\n{text} /no_think<|im_end|>\n"
           "<|im_start|>assistant\n<think>\n\n</think>\n\n")

# Stop as soon as the assistant turn ends.
_END_TOKENS = ["<|im_end|>", "<|endoftext|>"]

# Decoding: greedy (topk=1) is deterministic, which matters because the fuzzy
# cache assumes the same source yields the same translation. A light repetition
# penalty plus a no-repeat n-gram block guard against the degenerate loop a very
# broken line can trigger — a moan/stammer line with almost nothing to translate
# ("んっ、く、ふ……いじわるっ………") sent the model into "very much… very much…" repeated
# to _MAX_LENGTH, which filled the whole overlay. `no_repeat_ngram_size` makes it
# IMPOSSIBLE to emit the same 3-token sequence twice, so the loop cannot form
# regardless of input; the penalty alone (1.05) was too weak to stop it. 3-grams
# don't recur in normal translation, so ordinary lines are unaffected (a short
# "no, no" is a 1-gram repeat and still allowed).
# Hard cap on generated tokens. Lowered 512 -> 160: with SentenceCache every call
# is a single sentence, whose translation tops out ~35 tokens (measured), so 160 is
# ample headroom for a real line while bounding a reasoning/repetition runaway the
# callback misses. (512 let a runaway burn ~120 s.)
_MAX_LENGTH = 160
_SAMPLING_TOPK = 1
_REPETITION_PENALTY = 1.05
_NO_REPEAT_NGRAM = 3

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
# The `/no_think` soft switch we append to the user turn (see `_PROMPT`) is a
# directive, not content — but the model occasionally echoes it into the answer
# ("…we too /no_think"). It never belongs in a translation, so strip any leaked
# `/no_think` / `/think` marker from the output.
_NOTHINK_MARK = re.compile(r"\s*/\s*(?:no[_ ]?)?think\b", re.IGNORECASE)
# Wrapping quotes the model sometimes adds around the whole line despite the
# instruction not to — only stripped when they enclose the entire output.
_WRAPPED_QUOTES = re.compile(r'^\s*["“”「『\'](.*)["“”」』\']\s*$',
                             re.DOTALL)

# Sentence splitting for the derail-recovery fallback (see `translate`) is shared
# with SentenceCache — imported as `_split_sentences` from `translate.segment`.


# Romanized-Japanese markers a correct English translation essentially never
# contains and that are not names either — the copula / politeness / verb endings
# of a line the model TRANSLITERATED instead of translating ("…suki desu ne",
# "…maji de hataraku ssne"). Such output has no Japanese *script*, so the
# residual-Japanese net (`_JP_SCRIPT`) misses it; these catch it instead. One hit
# is enough — none of these is an English word. Pronouns/particles (kono, maji,
# wa, no, are…) are deliberately excluded: they collide with English words or
# names ("Kono" is a surname, "are"/"no"/"sore" are English), which would flag a
# good translation. High precision is the priority — a derailment carried ONLY by
# those slips through, but a correct line is never mistaken for romaji.
_ROMAJI_MARKERS = frozenset({
    "desu", "masu", "deshita", "mashita", "deshou", "darou", "daro",
    "gozaimasu", "gozaimashita", "kudasai", "nandesu", "arigatou",
    "sumimasen", "onegaishimasu", "ssne", "ssu", "ttsu",
})
_WORD_TOKEN = re.compile(r"[a-z]+")


def _looks_romaji(text: str) -> bool:
    """True if the output looks transliterated rather than translated.

    Flags only on the high-precision markers above, so a normal English
    translation is never mistaken for romaji (see `_ROMAJI_MARKERS`).
    """
    return any(tok in _ROMAJI_MARKERS for tok in _WORD_TOKEN.findall(text.lower()))


# Ellipsis / hesitation marks — the fingerprint of broken, stammered VN speech
# ("ん………う、ん!、下…………下…"). A normal grammatical sentence doesn't pepper these
# between fragments, so their presence gates the more aggressive recovery below
# (structural-romaji detection + 、-splitting) to exactly the lines it helps.
_ELLIPSIS = re.compile(r"…|‥|\.{3,}|。{2,}")


def _has_ellipsis(text: str) -> bool:
    return bool(_ELLIPSIS.search(text))


# A romaji WORD decomposes fully into Japanese syllables (including a sokuon
# k/s/t/p). This is a *structural* romaji signal for output the markers miss
# ("kashita", "wakaru", "deshino"). On its own it over-fires — several loanwords
# and CV-shaped English words ("banana", "katana", "arena") decompose too — so it
# is only ever consulted on ellipsis-laden broken speech (see `_derailed`), which
# a normal banana/katana sentence is not.
_ROMAJI_SYL = (
    r"(?:kya|kyu|kyo|gya|gyu|gyo|sha|shu|sho|sya|syu|syo|jya|jyu|jyo|cha|chu|cho|"
    r"nya|nyu|nyo|hya|hyu|hyo|bya|byu|byo|pya|pyu|pyo|mya|myu|myo|rya|ryu|ryo|"
    r"shi|chi|tsu|ji|zi|fu|hu|si|ti|tu|di|du|[kgsztdnhbpmr][aiueo]|[wy][aiueo]|"
    r"wo|n|[kstpc]|[aiueo])"
)
_ROMAJI_WORD = re.compile(r"^(?:" + _ROMAJI_SYL + r"){2,}$")


def _looks_romaji_structural(text: str) -> bool:
    """True if 2+ longish output words fully decompose into romaji syllables."""
    hits = [t for t in _WORD_TOKEN.findall(text.lower())
            if len(t) >= 5 and _ROMAJI_WORD.match(t)]
    return len(hits) >= 2


# The model occasionally breaks character and narrates its own reasoning, or echoes
# the system-prompt rules, INTO the answer instead of just translating ("This is
# the assistant's response… based on the rules provided, I need to output only the
# English translation…", seen on 副校舎). The <think> stripping misses it because
# it is in the answer, not a think block. These phrases essentially never occur in
# a real VN line, so their presence marks a derailed, meta output. High precision
# is the priority — every marker is a multi-word phrase a character would not say.
_META_MARKERS = (
    "the assistant's response", "based on the rules", "the user's line",
    "i need to output only", "the correct translation of",
    "translation of the japanese", "the japanese line", "the user wants",
    "the user is asking", "as an ai language model", "i cannot translate",
)


def _looks_meta(text: str) -> bool:
    """True if the output narrates the task / echoes the rules instead of translating."""
    low = (text or "").lower()
    return any(m in low for m in _META_MARKERS)


def _derailed(source: str, out: str) -> bool:
    """Whole-line output looks like a failure worth a split-retry (see `translate`).

    Four signals, in rising order of how much they need gating: an empty output;
    meta/rule-echo commentary (trusted on any line — see `_META_MARKERS`); a
    high-precision romaji marker (trusted on any line); or the structural romaji
    test — trusted ONLY on ellipsis-laden broken speech, where a correct English
    translation (which has no such ellipses) can't be mistaken for it.
    """
    if not out.strip():
        return True
    if _looks_meta(out):
        return True
    if _looks_romaji(out):
        return True
    return _has_ellipsis(source) and _looks_romaji_structural(out)


# The 、-fragment rejoin must gain at least this many letters over the whole-line
# output to be worth taking — so it recovers a genuinely dropped fragment without
# displacing a coherent line whose fragments merely read differently.
_MIN_RECOVER = 4
_LETTER = re.compile(r"[A-Za-z]")


def _ascii_letters(text: str) -> int:
    return sum(1 for c in text if c.isascii() and c.isalpha())


def _has_gap(text: str) -> bool:
    """True if the output has a comma-separated segment with no letters at all.

    A silently dropped 、-fragment surfaces as "..., ... , ..." — the model keeps
    the comma but renders the fragment as bare ellipsis. Distinct from an empty or
    romaji output, so `_derailed` misses it; this catches it cheaply, without a
    re-translation, so a plainly-complete line skips the fragment retry.
    """
    segs = [s for s in text.split(",") if s.strip()]
    return len(segs) > 1 and any(not _LETTER.search(s) for s in segs)


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
    text = _NOTHINK_MARK.sub("", text).strip()   # drop a leaked /no_think marker
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
        # Early-stop on runaway reasoning. Qwen3 sometimes ignores the no-think
        # prompt and reasons out loud ("Okay, let's tackle this translation. The
        # user provided the Japanese line…") for hundreds of tokens (100+ s) before
        # an answer we then discard as meta anyway. A real translation is short (a
        # single sentence tops out ~35 tokens), so as soon as the GROWING output
        # trips the meta detector we stop generating — turning a ~120 s freeze into
        # a couple of seconds. `_MAX_LENGTH` is the hard backstop if a reasoning run
        # somehow avoids the markers.
        acc: list[int] = []

        def _stop_on_reasoning(res) -> bool:
            acc.append(res.token_id)
            # Check every few tokens (decoding each step would be wasteful). The
            # markers never occur in a real VN translation, so this can't clip a
            # good line; and a good line ends (end_token) long before it matters.
            if res.step >= 8 and res.step % 8 == 0:
                return _looks_meta(self._tokenizer.decode(acc))
            return False

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
            no_repeat_ngram_size=_NO_REPEAT_NGRAM,
            end_token=_END_TOKENS,
            callback=_stop_on_reasoning,
        )
        if not results or not results[0].sequences_ids:
            return ""
        # Decode from token *ids*: Qwen's BPE is byte-level, so joining the token
        # strings would leave the byte markers in place.
        return _clean_output(self._tokenizer.decode(results[0].sequences_ids[0]))

    def _translate_line(self, source: str) -> str:
        """One model call on a whole source line + the residual-Japanese net.

        `source` must already be name-protected and bracket-stripped. Residual
        Japanese script means the model failed on this line, so we salvage what
        the dictionary can (see `_recover_untranslated`).
        """
        out = _strip_brackets(self._generate(source))
        if _JP_SCRIPT.search(out):
            out = _recover_untranslated(out)
        return out

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
        out = self._translate_line(source)
        # Whole-line-with-context is the primary path (it reads best — see the
        # module docstring). But a derailing line can make the model emit NOTHING,
        # transliterate into romaji, or silently DROP a 、-fragment — taking the
        # good parts down with it. When that happens, re-translate in pieces.
        recovered = self._recover(source, out)
        if recovered is not None:
            out = recovered
        # If the model rambled meta-commentary / rule-echo that recovery couldn't
        # clear (greedy decoding makes a plain retry reproduce it, and a single
        # sentence has nothing to split), drop the unit — a blank beats a paragraph
        # of the model narrating itself on the overlay. SentenceCache means only
        # the offending sentence is lost, not the whole line.
        if _looks_meta(out):
            return ""
        return _sentence_case(_normalize_punct(out))

    def _recover(self, source: str, out: str) -> str | None:
        """Re-translate a line the whole pass mishandled; None to keep `out`.

        Broken, hesitant VN speech ("下…………下、わかる…でしょ………") derails as a whole —
        the model emits nothing, transliterates into romaji, or silently drops a
        、-fragment (only ellipsis for it) — yet each piece translates cleanly
        alone. Two routes, in order:

          1. **Sentence split (。！？)** — preferred when the line has real sentence
             boundaries: it keeps clauses intact and never orphans a stutter comma
             ("こ、こそ"). Taken for an empty/romaji whole pass, kept only if the
             rejoin is itself clean.
          2. **Per-、 fragments** — for broken speech route 1 can't split (no 。) or
             didn't fix. Each pause-separated fragment is its own unit, rejoined
             with ", ", and kept ONLY if it recovers materially more text than the
             whole pass (`_MIN_RECOVER` more letters) — so a coherent line, whose
             fragments read no fuller, is left as the whole-line version.

        A normal sentence has no ellipsis (route 2 skips it) and no empty/romaji
        failure (route 1 skips it), so its clauses are never chopped.
        """
        empty = not out.strip()
        # Route 1 — sentence split for an empty/romaji whole pass.
        if empty or _derailed(source, out):
            parts = _split_sentences(source)
            if len(parts) > 1:
                joined = " ".join(
                    p for p in (self._translate_line(s).strip() for s in parts) if p)
                if joined and (empty or not _derailed(source, joined)):
                    return joined
        # Route 2 — per-、 fragments for broken speech, taken only when it recovers
        # more content. The cheap gate (empty / derailed / a letter-less gap)
        # spares a plainly-complete line the extra per-fragment calls.
        if (_has_ellipsis(source) and "、" in source
                and (empty or _derailed(source, out) or _has_gap(out))):
            frags = [p for p in source.split("、") if p.strip()]
            if len(frags) > 1:
                # Drop a lone trailing period per fragment so ", " joining doesn't
                # read "um., N!" — but keep an ellipsis, "!" or "?" (they carry tone).
                trim = lambda s: s[:-1] if s.endswith(".") and not s.endswith("..") else s
                joined = ", ".join(
                    p for p in (trim(self._translate_line(f).strip()) for f in frags) if p)
                if joined and _ascii_letters(joined) > _ascii_letters(out) + _MIN_RECOVER:
                    return joined
        return None
