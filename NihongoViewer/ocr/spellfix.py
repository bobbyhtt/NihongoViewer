"""Post-OCR spell-fix: repair within-katakana misreads using JMdict.

`normalize.py` fixes katakana<->kanji lookalikes from *context* (there's a script
boundary to exploit). Confusions **within** katakana have no such boundary — both
glyphs are valid kana, so only vocabulary can tell ``ワーポン`` (not a word) from
``クーポン`` (a word), or ``スタミナ`` from a one-glyph misread of it. Common pairs:

    ク<->ワ   ソ<->ン   シ<->ツ   and small<->large kana (ャ<->ヤ, ッ<->ツ, ...)

This pass pulls out each katakana run JMdict does **not** recognise and, if swapping
exactly one known-confusable glyph turns it into a run JMdict **does** recognise,
applies that single correction.

Safety (why it won't corrupt correct text):
  * a run that is already a valid word is never touched;
  * a run is only corrected when *exactly one* single-glyph swap yields a valid
    word — zero matches (a name/rare word we can't confirm) or two+ matches (an
    ambiguous run) are both left untouched;
  * corrections preserve length (one glyph for one glyph), so offsets stay valid.

The JMdict index is the one Read Mode / the translator already use. This is
best-effort and **non-blocking**: if the index isn't built yet we kick off a
one-time background build and return the text unchanged until it's ready — the OCR
path never blocks on a download.
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


def correct(text: str) -> str:
    """Repair within-katakana OCR misreads in `text` (best-effort, non-blocking)."""
    if not text:
        return text
    runs = [(m.start(), m.group()) for m in _KATA_RUN.finditer(text)]
    # Fast out: nothing worth checking unless a run is long enough AND contains a
    # confusable glyph.
    if not any(len(r) >= _MIN_LEN and any(c in _ALT for c in r) for _, r in runs):
        return text

    try:
        from translate import dictionary
    except Exception:
        return text
    if dictionary.status().get("state") != "ready":
        _warm(dictionary)                 # build in the background; skip for now
        return text

    out = text
    for start, run in runs:
        if len(run) < _MIN_LEN or not any(c in _ALT for c in run):
            continue
        fixed = _correct_run(run)
        if fixed and fixed != run:        # same length, so offsets stay valid
            out = out[:start] + fixed + out[start + len(run):]
    return out
