"""Protect proper-noun katakana (names) from mistranslation.

A general MT model can't know a katakana run is somebody's name. MADLAD sees the
rude word ヤツ inside the name ヤツシロ and renders "you bitch"; ヤツシロ alone becomes
"You're a shithead". The fix that works is to hand the model the *romaji* instead —
it passes Latin text through verbatim and places it correctly ("Wait a minute,
Yatsushiro.").

So before translating we scan each maximal katakana run and, using a morphological
analyzer (fugashi/UniDic), decide:

  * the run is an ordinary word / loanword (コーヒー = 普通名詞) -> leave it, let the
    model translate it;
  * the run is a proper noun or unknown word (ヤツシロ = 固有名詞) -> replace it with
    its romaji so it survives as a name.

We also handle **furigana** — a term written 漢字(かな), e.g. 鬼戮(きりく). The model
can't translate the rare kanji and just echoes it ("鬼殺 (kiriku)"), but the parens
literally hand us the reading, so we replace the whole thing with the romanized
reading (-> "Kiriku"), which is what a proper-noun term wants.

A user-editable override map (`names.json` in the config dir) wins over the
automatic decision — needed because katakana Western names romanize to their
Japanese spelling (ボブ -> "Bobu"), so a user can pin ボブ -> "Bob" (or 鬼戮 -> "Kiriku"
if they'd rather fix the reading). Override keys match either the katakana run or a
furigana term's kanji base.

Everything here is best-effort: if the analyzer / romanizer isn't installed the
text passes through unchanged and translation still works, just without name
protection (see the guarded imports).
"""

import json
import re

# Maximal runs of katakana (incl. the ー long-vowel mark and halfwidth katakana),
# length >= 2 (a lone kana is never a name and is often punctuation-ish).
_KATAKANA_RUN = re.compile(r"[ァ-ヺーヿｦ-ﾟ]{2,}")

# Furigana: kanji immediately followed by a kana-only reading in (full/half) parens
# — 鬼戮(きりく). The parens holding *only* kana is the tell that it's a reading.
_FURIGANA = re.compile(r"([一-鿿々〆ヶ]+)\s*[（(]\s*([ぁ-ゟァ-ヺー]+)\s*[）)]")

# UniDic tags a name as 名詞-固有名詞-… (pos2 == 固有名詞).
_PROPER_NOUN = "固有名詞"

_tagger = None                   # fugashi.Tagger singleton (or False if missing)
_overrides: dict | None = None   # JA -> EN user overrides, loaded once


def _get_tagger():
    global _tagger
    if _tagger is None:
        try:
            import fugashi

            _tagger = fugashi.Tagger()
        except Exception:
            _tagger = False  # analyzer missing/broken -> auto-detection disabled
    return _tagger or None


def get_tagger():
    """Public accessor for the shared fugashi/UniDic tagger (or None if missing).

    Exposed so other stages (furigana generation) reuse the one loaded instance
    instead of paying to load UniDic a second time.
    """
    return _get_tagger()


def _romaji(kana: str) -> str:
    """Hepburn-ish romaji for a katakana run, capitalized ("ヤツシロ" -> "Yatsushiro")."""
    try:
        import jaconv
    except Exception:
        return kana
    hira = jaconv.kata2hira(kana.replace("ー", ""))
    romaji = jaconv.kana2alphabet(hira)
    return romaji[:1].upper() + romaji[1:] if romaji else kana


def _load_overrides() -> dict:
    global _overrides
    if _overrides is None:
        _overrides = {}
        try:
            import config

            path = config.path().with_name("names.json")
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                _overrides = {str(k): str(v) for k, v in data.items() if k and v}
        except (FileNotFoundError, json.JSONDecodeError, OSError, ImportError):
            pass
    return _overrides


def _is_name_run(run: str) -> bool:
    """True if the analyzer sees the run as a proper noun / unknown word (a name)."""
    tagger = _get_tagger()
    if tagger is None:
        return False  # no analyzer -> can't tell -> leave it for the model
    tokens = list(tagger(run))
    if not tokens:
        return False
    # A name run has a proper-noun or unknown token; a loanword is all common nouns.
    for tok in tokens:
        if getattr(tok, "is_unk", False):
            return True
        if getattr(tok.feature, "pos2", None) == _PROPER_NOUN:
            return True
    return False


def protect(text: str) -> str:
    """Romanize furigana readings and name-like katakana (overrides first)."""
    if not text:
        return text
    overrides = _load_overrides()

    # 1) Furigana 漢字(かな) -> the romanized reading (override on the kanji base wins).
    def furigana(match: "re.Match") -> str:
        kanji, reading = match.group(1), match.group(2)
        return overrides.get(kanji) or _romaji(reading)

    text = _FURIGANA.sub(furigana, text)

    # 2) Proper-noun katakana runs -> romaji (override on the run wins).
    def katakana(match: "re.Match") -> str:
        run = match.group(0)
        if run in overrides:
            return overrides[run]
        return _romaji(run) if _is_name_run(run) else run

    return _KATAKANA_RUN.sub(katakana, text)
