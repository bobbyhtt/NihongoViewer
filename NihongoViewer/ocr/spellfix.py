"""Post-OCR spell-fix: repair kana misreads using JMdict. Two passes.

**1. Within-katakana confusables.** `normalize.py` fixes katakana<->kanji
lookalikes from *context* (there's a script boundary to exploit). Confusions
**within** katakana have no such boundary — both glyphs are valid kana, so only
vocabulary can tell ``ワーポン`` (not a word) from ``クーポン`` (a word). Common pairs:

    ク<->ワ   ソ<->ン   シ<->ツ   and small<->large kana (ャ<->ヤ, ッ<->ツ, ...)

This pass pulls out each katakana run JMdict does **not** recognise and, if swapping
exactly one known-confusable glyph turns it into a run JMdict **does** recognise,
applies that single correction. Safety: a run already a valid word is never
touched; a correction applies only when *exactly one* single-glyph swap yields a
valid word (zero or 2+ matches are left alone); length is preserved.

**2. Hiragana small-kana (yoon / sokuon) flattening.** OCR reads a small kana as
its full-size twin — they differ only in size — so ``ちょっと`` comes back as
``ちよっと``. The mangled word isn't in the dictionary, and the translator then
**romanizes the whole line** ("Chotto so no anata") instead of translating it.

Unlike the katakana pass, this can't be driven by JMdict: a yoon lives inside a
long hiragana run of particles and word boundaries, and a dictionary probe can't
tell a genuine yoon (``にゃ``) from a particle boundary (``に｜やる``) — a general
JMdict rule (tested on real captures) corrupted correct verbs (``気がつく`` ->
``がっく``, ``養子にやる`` -> ``にゃる``, even the impossible ``てゃって``). Doing it right
needs a morphological analyzer, which is out of scope here. So this pass is a
small **curated list** of exact mangled spellings that are (a) not themselves
JMdict words and (b) distinctive enough not to occur across a word boundary —
applied by plain substring replacement. Extend `_SMALL_KANA_FIXES` as new
unambiguous mangles show up.

The JMdict index (pass 1) is the one Read Mode / the translator already use. That
pass is best-effort and **non-blocking**: if the index isn't built yet we kick
off a one-time background build and skip it for now. Pass 2 needs no dictionary,
so it always runs.
"""
import re
import threading
from functools import lru_cache

# Visually-confusable katakana glyphs -> the other glyphs they get read as.
_GROUPS = [
    {"ク", "ワ"},
    {"ソ", "ン"},
    {"シ", "ツ"},
    {"ッ", "ツ"},
    {"ャ", "ヤ"}, {"ュ", "ユ"}, {"ョ", "ヨ"},
    {"ァ", "ア"}, {"ィ", "イ"}, {"ゥ", "ウ"}, {"ェ", "エ"}, {"ォ", "オ"},
]
_ALT: dict[str, set[str]] = {}
for _g in _GROUPS:
    for _ch in _g:
        _ALT.setdefault(_ch, set()).update(_g - {_ch})

# Maximal katakana run: a letter followed by letters / the ー prolonged mark. It
# must START on a letter, so a stray leading ー (a dash) isn't pulled in, and ・
# (the name separator) is excluded so compound names split into their parts.
_KATA_RUN = re.compile(r"[ァ-ヺ][ァ-ヺー]*")

_MIN_LEN = 3            # shorter runs are too ambiguous to correct safely

# --- hiragana small-kana (yoon / sokuon) repair ----------------------------
# Exact mangled spelling -> correct spelling (see module docstring for why this
# is a curated list, not a JMdict rule). Every key is verified NOT a JMdict word
# and long/distinctive enough not to appear inside or across real words. OCR may
# flatten any subset of a word's small kana, so a word with two contributes
# several keys (ちょっと: よ and/or っ mis-sized).
_SMALL_KANA_FIXES = {
    "ちよっと": "ちょっと", "ちよつと": "ちょっと", "ちょつと": "ちょっと",
    "いらっしや": "いらっしゃ",   # いらっしゃる / いらっしゃい (both extend this stem)
    "ひよんな": "ひょんな",
}

_warm_lock = threading.Lock()
_warming = False


def _warm(dictionary) -> None:
    """Build the JMdict index once, in the background (never blocks OCR)."""
    global _warming
    with _warm_lock:
        if _warming:
            return
        _warming = True
    threading.Thread(
        target=lambda: _safe_ensure(dictionary), name="dict-warm", daemon=True
    ).start()


def _safe_ensure(dictionary) -> None:
    try:
        dictionary.ensure()
    except Exception:
        pass


@lru_cache(maxsize=8192)
def _is_word(token: str) -> bool:
    """True if `token` is a JMdict headword. Caller must confirm index readiness."""
    from translate import dictionary
    return bool(dictionary.lookup(token, limit=1))


def _correct_run(run: str) -> str | None:
    """The unique valid single-swap of `run`, or None to leave it as-is."""
    if _is_word(run):
        return None                       # already a real word — never touch it
    candidates = set()
    for i, ch in enumerate(run):
        for alt in _ALT.get(ch, ()):
            candidates.add(run[:i] + alt + run[i + 1:])
    candidates.discard(run)
    hits = [c for c in candidates if _is_word(c)]
    return hits[0] if len(hits) == 1 else None


def _repair_small_kana(text: str) -> str:
    """Restore flattened small kana from the curated list (ちよっと -> ちょっと)."""
    for mangled, fixed in _SMALL_KANA_FIXES.items():
        if mangled in text:
            text = text.replace(mangled, fixed)
    return text


def correct(text: str) -> str:
    """Repair OCR kana misreads in `text` (best-effort, non-blocking).

    Two passes (see module docstring): the small-kana whitelist (no dictionary,
    always runs) and the within-katakana confusable repair (needs the JMdict
    index; if it isn't built yet we kick off a background build and skip it).
    """
    if not text:
        return text
    out = _repair_small_kana(text)

    needs_kata = any(
        len(m.group()) >= _MIN_LEN and any(c in _ALT for c in m.group())
        for m in _KATA_RUN.finditer(out))
    if not needs_kata:
        return out
    try:
        from translate import dictionary
    except Exception:
        return out
    if dictionary.status().get("state") != "ready":
        _warm(dictionary)                 # build in the background; skip for now
        return out

    for start, run in [(m.start(), m.group()) for m in _KATA_RUN.finditer(out)]:
        if len(run) < _MIN_LEN or not any(c in _ALT for c in run):
            continue
        fixed = _correct_run(run)
        if fixed and fixed != run:        # same length, so offsets stay valid
            out = out[:start] + fixed + out[start + len(run):]
    return out
