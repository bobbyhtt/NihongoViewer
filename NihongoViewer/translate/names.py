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

# UniDic tags a name as 名詞-固有名詞-… (pos2 == 固有名詞); pos3 distinguishes a
# person's name (人名) from a place (地名) or general (一般). We romanize kanji
# *person* names (the model drops them), but leave place names — the model knows
# 東京 -> Tokyo, and its reading would give a worse "Toukyou".
_PROPER_NOUN = "固有名詞"
_PERSON_NAME = "人名"
_HAS_KANJI = re.compile(r"[一-鿿々〆ヶ]")

# Hiragana okurigana that can trail a katakana verb/adjective stem, and the POS
# tags of an inflected word — used to spot a katakana run that is really a stylized
# verb (シゴいて = しごいて), which the model otherwise reads as a name ("Shigo").
_HIRAGANA_TAIL = re.compile(r"[ぁ-ゟ]+")
_INFLECTED_POS1 = {"動詞", "形容詞", "形状詞"}  # verb / i-adjective / na-adjective

# Built-in glossary of terms a general MT model reliably gets wrong — colloquial
# anatomical vocabulary MADLAD renders as the wrong word ("おちんちん" -> "pussy" /
# "Oh, my God"). JA source -> correct EN; substituted before the model, so it
# survives verbatim. A user `glossary.json` in the config dir merges on top (user
# wins), so anyone can correct any other term for their own content.
_GLOSSARY = {
    "おちんちん": "penis",
    "ちんちん": "penis",
    "おちんぽ": "penis",
    "ちんぽ": "penis",
    "おまんこ": "vagina",
    "まんこ": "vagina",
    # パイズリ (titfuck) is in JMdict, but the ス-spelled variant パイスリ isn't, so
    # the model mangles it ("Spicy"); pin it to the plain literal gloss.
    "パイスリ": "breast rubbing",
    # Laughter onomatopoeia the model mangles ("ふふ" -> "fuck" / "I'm sorry") —
    # keep them as the romanized laugh.
    "ふふふ": "fufufu",
    "ふふっ": "fufu",
    "ふふ": "fufu",
    "うふふ": "ufufu",
    "えへへ": "ehehe",
}

_tagger = None                   # fugashi.Tagger singleton (or False if missing)
_overrides: dict | None = None   # JA -> EN user name overrides, loaded once
_glossary: dict | None = None    # JA -> EN term glossary (built-in + user), once


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
        _overrides = _load_json_map("names.json")
    return _overrides


def _load_glossary() -> dict:
    """Built-in `_GLOSSARY` merged with a user `glossary.json` (user wins)."""
    global _glossary
    if _glossary is None:
        _glossary = {**_GLOSSARY, **_load_json_map("glossary.json")}
    return _glossary


def _load_json_map(filename: str) -> dict:
    """Load a JA->EN string map from `filename` in the config dir ({} if absent)."""
    try:
        import config

        path = config.path().with_name(filename)
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items() if k and v}
    except (FileNotFoundError, json.JSONDecodeError, OSError, ImportError):
        pass
    return {}


def _to_hiragana(kata: str) -> str:
    """Katakana -> hiragana ("シゴ" -> "しご"); unchanged if jaconv is missing."""
    try:
        import jaconv
    except Exception:
        return kata
    return jaconv.kata2hira(kata)


def _is_inflected(text: str) -> bool:
    """True if `text`'s first token is a verb/adjective — i.e. the leading katakana
    run is a stylized verb stem (シゴいて), not a name."""
    tagger = _get_tagger()
    if tagger is None:
        return False
    tokens = list(tagger(text))
    return bool(tokens) and getattr(tokens[0].feature, "pos1", None) in _INFLECTED_POS1


def _romanize_kanji_names(text: str) -> str:
    """Romanize kanji person-names via the analyzer's reading (香純 -> Kasumi).

    A general MT model can't translate a kanji name and just drops it (香純 -> "")
    or hallucinates one. When the analyzer tags a kanji token as a *person* proper
    noun it also hands us the reading, so we romanize that and the name survives.
    """
    tagger = _get_tagger()
    if tagger is None:
        return text
    repl: dict[str, str] = {}
    for tok in tagger(text):
        surf = tok.surface
        if surf in repl or not _HAS_KANJI.search(surf):
            continue
        feat = tok.feature
        if (getattr(feat, "pos2", None) == _PROPER_NOUN
                and getattr(feat, "pos3", None) == _PERSON_NAME):
            reading = getattr(feat, "kana", None) or getattr(feat, "pron", None)
            if reading:
                repl[surf] = _romaji(reading)
    for surf, rom in repl.items():
        text = text.replace(surf, rom)
    return text


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
    """Fix known-bad terms, romanize furigana readings and name-like katakana."""
    if not text:
        return text
    overrides = _load_overrides()

    # 0) Glossary: replace terms the model gets wrong with the correct English
    #    (any script — hiragana/kanji/katakana). Longest key first so a longer term
    #    wins over a shorter substring of it.
    glossary = _load_glossary()
    for key in sorted(glossary, key=len, reverse=True):
        if key in text:
            text = text.replace(key, glossary[key])

    # 1) Furigana 漢字(かな) -> the romanized reading (override on the kanji base wins).
    def furigana(match: "re.Match") -> str:
        kanji, reading = match.group(1), match.group(2)
        return overrides.get(kanji) or _romaji(reading)

    text = _FURIGANA.sub(furigana, text)

    # 1b) Kanji person-names -> romaji reading (香純 -> Kasumi), which the model
    #     would otherwise drop. Place names are left for the model.
    text = _romanize_kanji_names(text)

    # 2) Proper-noun katakana runs -> romaji (override on the run wins).
    def katakana(match: "re.Match") -> str:
        run = match.group(0)
        if run in overrides:
            return overrides[run]
        # A katakana run written as a verb/adjective stem — followed by hiragana
        # okurigana, e.g. シゴいて for しごいて — is not a name. The model reads bare
        # katakana like シゴ as a name ("Shigo takes …"), so rewrite it to hiragana
        # and let the model translate it as the verb it is.
        tail = _HIRAGANA_TAIL.match(text, match.end())
        if tail and _is_inflected(run + tail.group(0)):
            return _to_hiragana(run)
        return _romaji(run) if _is_name_run(run) else run

    return _KATAKANA_RUN.sub(katakana, text)
