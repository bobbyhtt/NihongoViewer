"""Protect proper-noun katakana (names) from mistranslation.

A general MT model can't know a katakana run is somebody's name. MADLAD-400 saw the
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

# Small kana and the ー long-mark can never START a word — nothing begins with a
# sokuon (ッ) or a small yoon (ャ). A run that starts with one is therefore a
# word-fragment the OCR split off, not a name: ベッド misread ベ->べ leaves the
# katakana tail ッド, which romanized becomes garbage ("Xtsudo"). Such a run is
# left for the model instead of being romanized.
_KATA_NONINITIAL = set("ァィゥェォッャュョヮヵヶーｧｨｩｪｫｬｭｮｯｰ")

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

# Honorific suffixes that mark the PRECEDING token as a person's name. UniDic
# often mis-tags a surname as a place (仁木 -> 地名) or can't parse an invented name
# at all, but "<X>君 / <X>さん / <X>ちゃん …" is unambiguously a person, so a proper
# noun or unknown kanji token sitting right before one of these is romanized as a
# name too. Common nouns are deliberately NOT eligible, so "店員さん" stays "clerk"
# and "神様" stays "god" rather than becoming "Tenin"/"Kami".
_NAME_HONORIFICS = {"君", "くん", "さん", "ちゃん", "様", "さま", "氏", "殿",
                    "先生", "先輩", "先パイ", "せんぱい"}

# Hiragana okurigana that can trail a katakana verb/adjective stem, and the POS
# tags of an inflected word — used to spot a katakana run that is really a stylized
# verb (シゴいて = しごいて), which the model otherwise reads as a name ("Shigo").
_HIRAGANA_TAIL = re.compile(r"[ぁ-ゟ]+")
_INFLECTED_POS1 = {"動詞", "形容詞", "形状詞"}  # verb / i-adjective / na-adjective

# Built-in glossary of terms a general MT model reliably gets wrong — colloquial
# anatomical vocabulary an MT model renders as the wrong word ("おちんちん" -> "pussy" /
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
    # おっぱい is a JMdict headword, but the model still gets it wrong in context
    # ("おっぱいが大きい" -> "My ass is big"), so pin the literal gloss. The OCR
    # small-tsu misread おつぱい is auto-covered by _load_glossary (see there).
    "おっぱい": "breasts",
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

# OCR reads a small kana as its full-size twin — the glyphs differ only in size,
# so "おっぱい" comes back as "おつぱい" and "ふふっ" as "ふふつ". The mangled form is
# not a dictionary word, so the model romanizes it ("otsubai") instead of
# translating. `_load_glossary` enlarges every small kana in each glossary key to
# register that misread spelling under the same gloss, so a mangled capture still
# resolves. Keyed small -> large; covers hiragana and katakana small kana.
_SMALL_TO_LARGE = str.maketrans({
    "ぁ": "あ", "ぃ": "い", "ぅ": "う", "ぇ": "え", "ぉ": "お",
    "っ": "つ", "ゃ": "や", "ゅ": "ゆ", "ょ": "よ", "ゎ": "わ",
    "ァ": "ア", "ィ": "イ", "ゥ": "ウ", "ェ": "エ", "ォ": "オ",
    "ッ": "ツ", "ャ": "ヤ", "ュ": "ユ", "ョ": "ヨ", "ヮ": "ワ",
})

# --- bare name plates ------------------------------------------------------
# A character-name label ("トワ" over a speaker) reaches us as its own OCR region
# with no sentence around it, and that is exactly where the run logic below
# fails. Two reasons, both observed:
#
#   * OCR homoglyphs break the run outright. PP-OCR reads a heavy outlined game
#     font's ワ as the digit 7 and ト as the kanji 卜, so "トワ" arrives as "ト7"
#     or "卜ワ" — no katakana run matches, nothing is protected, and the model
#     invents an English word ("T7").
#   * UniDic tags short name-shaped runs by their surface form, not their role:
#     ワワ is 感動詞 (interjection), リゼ is 助動詞+助詞. `_is_name_run` says "not
#     a name", the model gets it raw, and out comes "Wow".
#
# For a line that is *only* katakana we can decide by vocabulary instead, which
# is far more reliable than the POS tag: a JMdict headword is a real word the
# model should translate (コーヒー -> "Coffee", ダメ -> "No good"), and anything
# else is a name to romanize. See `_label_name`.

# Halfwidth katakana (ﾄﾜ) — another OCR spelling of the same label. Neither
# UniDic nor the romanizer handles it, so it is folded to fullwidth first.
_HALFWIDTH = re.compile(r"[ｦ-ﾟ]")

# Digits PP-OCR emits for the katakana they are drawn like. Applied only to a
# bare label, and only when the repair makes that label parse as a single proper
# noun — so an ordinary "レベル7" (which would become the noun+particle "レベルワ")
# keeps its digit. See `_label_name`.
_DIGIT_LOOKALIKES = {"7": "ワ", "1": "イ"}

# The label body: katakana plus the digits above, nothing else.
_LABEL_BODY = re.compile(r"^[ァ-ヺーヿ71]+$")

# Decoration around a name plate, kept out of the romanization and re-attached
# ("トワ、" -> "Towa、", which `qwen._normalize_punct` later turns into "Towa,").
_LABEL_TRIM = " \t　。．！？!?、，,：:；;・…「」『』（）()【】〔〕"

# Word classes that mean a katakana line is speech, not a name — a stylized verb
# (ヤメテ), adjective (スゴイ) or interjection (アリガトウ). A particle is allowed:
# UniDic splits real names that way (アスナ = アス/名詞 + ナ/助詞).
_PREDICATE_POS1 = {"動詞", "形容詞", "形状詞", "助動詞", "感動詞", "副詞"}

# A trailing っ/ッ has no sound of its own, but jaconv romanizes it literally
# ("トワッ" -> "towaxtsu"); mid-word it correctly doubles the next consonant
# ("バック" -> "bakku"), so only the trailing case is stripped.
_TRAILING_SOKUON = re.compile(r"[っッ]+$")
# jaconv spells a small kana that has no digraph partner as "x" + its vowel
# ("ぁ" -> "xa"); drop the marker so the romaji stays pronounceable.
_SMALL_KANA_ROMAJI = re.compile(r"x([aiueo])")

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
    hira = _TRAILING_SOKUON.sub("", hira)
    romaji = _SMALL_KANA_ROMAJI.sub(r"\1", jaconv.kana2alphabet(hira))
    return romaji[:1].upper() + romaji[1:] if romaji else kana


def _load_overrides(lang: str = "en") -> dict:
    """`names.json` overrides for target language `lang`, as {japanese: name}.

    A value may be a plain string — the original format, which pins the ENGLISH
    name ("ボブ": "Bob") — or a per-language object
    ("カズ": {"en": "Kazu", "zh-CN": "和真", "zh-TW": "和真"}). A plain-string pin is
    English-only: it is NOT applied to Chinese, where an English name would land in
    the middle of a Chinese sentence. zh-TW falls back to the zh-CN pin, so one
    Chinese pin covers both scripts.
    """
    global _overrides
    if _overrides is None:
        _overrides = _load_name_map("names.json")
    if lang == "zh-TW":
        return {k: v.get("zh-TW") or v["zh-CN"] for k, v in _overrides.items()
                if "zh-TW" in v or "zh-CN" in v}
    return {k: v[lang] for k, v in _overrides.items() if lang in v}


def _load_name_map_cached() -> dict:
    """`names.json` as {japanese: {lang: name}}, loaded once."""
    global _overrides
    if _overrides is None:
        _overrides = _load_name_map("names.json")
    return _overrides


def _load_name_map(filename: str) -> dict:
    """Load `names.json` as {japanese: {lang: name}} ({} if absent / invalid)."""
    try:
        import config

        path = config.path().with_name(filename)
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError, OSError, ImportError):
        return {}
    out: dict = {}
    if isinstance(data, dict):
        for key, val in data.items():
            if not key:
                continue
            if isinstance(val, str) and val:
                out[str(key)] = {"en": val}
            elif isinstance(val, dict):
                langs = {str(l): str(n) for l, n in val.items() if l and n}
                if langs:
                    out[str(key)] = langs
    return out


def _load_glossary() -> dict:
    """Built-in `_GLOSSARY` merged with a user `glossary.json` (user wins).

    Each key is also registered under its OCR small-kana misread spelling (see
    `_SMALL_TO_LARGE`), mapping to the same gloss — so a captured "おつぱい"
    resolves like "おっぱい". Canonical and user keys win over a generated variant.
    """
    global _glossary
    if _glossary is None:
        merged = {**_GLOSSARY, **_load_json_map("glossary.json")}
        for key, val in list(merged.items()):
            mangled = key.translate(_SMALL_TO_LARGE)
            if mangled != key:
                merged.setdefault(mangled, val)
        _glossary = merged
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

    Two cases are treated as a person name:
      * ``固有名詞・人名`` — the analyzer is sure (久夫 -> Hisao);
      * a proper noun the analyzer mis-typed as a place (仁木 -> 地名) OR an unknown
        kanji token, when it sits immediately before a name honorific (仁木**君**,
        <name>**さん**). The honorific is what disambiguates it from a real place
        (東京 alone is left for the model). Common nouns are never eligible even
        with an honorific, so 店員さん stays "clerk".
    """
    tagger = _get_tagger()
    if tagger is None:
        return text
    toks = list(tagger(text))
    repl: dict[str, str] = {}
    for i, tok in enumerate(toks):
        surf = tok.surface
        if surf in repl or not _HAS_KANJI.search(surf):
            continue
        feat = tok.feature
        is_proper = getattr(feat, "pos2", None) == _PROPER_NOUN
        is_person = is_proper and getattr(feat, "pos3", None) == _PERSON_NAME
        # A proper noun (any subtype) or unknown token right before an honorific is
        # a person's name being addressed — romanize it even if UniDic guessed a
        # place or couldn't parse it.
        before_honorific = (
            (is_proper or getattr(tok, "is_unk", False))
            and i + 1 < len(toks)
            and toks[i + 1].surface in _NAME_HONORIFICS
        )
        if is_person or before_honorific:
            reading = getattr(feat, "kana", None) or getattr(feat, "pron", None)
            if reading and reading != "*":
                repl[surf] = _romaji(reading)
    for surf, rom in repl.items():
        text = text.replace(surf, rom)
    return text


def _to_fullwidth(text: str) -> str:
    """Halfwidth katakana -> fullwidth ("ﾄﾜ" -> "トワ"); unchanged without jaconv."""
    if not _HALFWIDTH.search(text):
        return text
    try:
        import jaconv
    except Exception:
        return text
    return jaconv.h2z(text, kana=True, ascii=False, digit=False)


def _is_single_proper_noun(text: str) -> bool:
    """True if the analyzer reads `text` as exactly one proper-noun/unknown token.

    Used to accept or reject a guessed digit repair: "ト7" -> "トワ" is one
    人名 token, so the digit really was a misread ワ; "レベル7" -> "レベルワ" is
    レベル/名詞 + ワ/助詞, so the digit really was a digit and we keep it.
    """
    tagger = _get_tagger()
    if tagger is None:
        return False
    tokens = list(tagger(text))
    if len(tokens) != 1:
        return False
    return (bool(getattr(tokens[0], "is_unk", False))
            or getattr(tokens[0].feature, "pos2", None) == _PROPER_NOUN)


def _has_predicate(text: str) -> bool:
    """True if `text` contains a verb/adjective/interjection token (see _PREDICATE_POS1).

    Conservative: with no analyzer this returns True, so the label path declines
    to romanize rather than guess.
    """
    tagger = _get_tagger()
    if tagger is None:
        return True
    return any(getattr(tok.feature, "pos1", None) in _PREDICATE_POS1
               for tok in tagger(text))


def _dictionary_ready() -> bool:
    """True once the JMdict index is usable (main.py warms it at startup).

    Checked before any lookup so this stays **non-blocking** — we never trigger
    the one-time build from the translation path, and never decide a label is a
    name on the strength of a dictionary that simply isn't loaded yet.
    """
    try:
        from . import dictionary

        return dictionary.status().get("state") == "ready"
    except Exception:
        return False


def _is_headword(text: str) -> bool:
    """True if `text` is a JMdict entry — a real word, not a name."""
    try:
        from . import dictionary

        return bool(dictionary.lookup(text, limit=1))
    except Exception:
        return False


def _label_name(text: str, overrides: dict, to_name=None) -> str | None:
    """Romanize a bare katakana label (a character-name plate), or None to skip.

    Returns the romanized name with any surrounding punctuation kept, or None
    when `text` isn't a katakana-only label or is a real word the model should
    translate. See the block comment above `_HALFWIDTH` for why name plates need
    their own path.
    """
    stripped = text.strip().strip(_LABEL_TRIM)
    if len(stripped) < 2:
        return None
    core = _to_fullwidth(stripped)
    if not _LABEL_BODY.match(core):
        return None

    repaired = "".join(_DIGIT_LOOKALIKES.get(c, c) for c in core)
    if repaired != core:
        if not _is_single_proper_noun(repaired):
            return None      # the digit was a digit — leave the whole line alone
        core = repaired

    if core in overrides:
        name = overrides[core]   # an explicit user pin wins, dictionary or not
    else:
        # The vocabulary test is what keeps loanword labels with the model, so
        # until the index is warm we fall through to the general run logic.
        if not _dictionary_ready():
            return None
        if _is_headword(core) or _has_predicate(core):
            return None      # a real word (コーヒー / スゴイ) — let the model translate
        name = (to_name or _romaji)(core)
    # Re-attach whatever punctuation framed the plate, so "トワ、" keeps its comma.
    head, tail = text[:text.index(stripped)], text[text.index(stripped) + len(stripped):]
    return f"{head}{name}{tail}"


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


def _is_proper_noun_run(run: str) -> bool:
    """True if the analyzer tags the run as a known proper noun (not just unknown)."""
    tagger = _get_tagger()
    if tagger is None:
        return False
    return any(getattr(tok.feature, "pos2", None) == _PROPER_NOUN
               and not getattr(tok, "is_unk", False)
               for tok in tagger(run))


def protect(text: str, lang: str = "en", names_out: list | None = None) -> str:
    """Prepare `text` for translation into `lang` so names survive.

    English: fix known-bad terms, romanize furigana readings and name-like katakana.
    Chinese: see `_protect_zh` (transliterate katakana names into hanzi instead).
    `names_out`, if given, collects the Chinese name spellings inserted, so the
    translator can restore them if the model "corrects" one (卢纳里亚 for 鲁纳里亚).
    """
    if not text:
        return text
    if lang == "zh-TW":
        # zh-TW runs through the Simplified pipeline and is converted at the end
        # (see translate.kana_zh.to_traditional_tw). A pin is fed in Simplified, and
        # recorded as (simplified, exact pin) so the final step writes the pin back
        # EXACTLY — a round trip through the converters would turn a pinned 里 into
        # 裡 or 托 into 託. A zh-CN fallback pin is recorded as a name, so it gets the
        # same name-safe conversion as a transliteration.
        from .kana_zh import to_simplified

        overrides = {}
        for key, entry in (_load_name_map_cached()).items():
            pin = entry.get("zh-TW")
            cn = to_simplified(pin) if pin else entry.get("zh-CN")
            if not cn:
                continue
            overrides[key] = cn
            if names_out is not None and key in text:
                names_out.append((cn, pin) if pin else cn)
        return _protect_zh(text, overrides, names_out)
    if lang.startswith("zh"):
        return _protect_zh(text, _load_overrides(lang), names_out)
    overrides = _load_overrides("en")

    # 0) Glossary: replace terms the model gets wrong with the correct English
    #    (any script — hiragana/kanji/katakana). Longest key first so a longer term
    #    wins over a shorter substring of it.
    glossary = _load_glossary()
    for key in sorted(glossary, key=len, reverse=True):
        if key in text:
            text = text.replace(key, glossary[key])

    # 0a) Kanji-name overrides (names.json). A kanji name never reaches the
    #     katakana/furigana override checks below, and the analyzer either mistags
    #     it (静音 -> common noun) or splits it (羽加道 -> 羽/加/道), so a kanji-key
    #     override is applied here as a direct substring replacement. Katakana-key
    #     overrides stay CONTEXTUAL (handled below) so a short one can't match
    #     inside a longer word (リン -> リンゴ). Longest key first.
    for key in sorted((k for k in overrides if _HAS_KANJI.search(k)),
                      key=len, reverse=True):
        if key in text:
            text = text.replace(key, overrides[key])

    # 0b) A bare katakana line is a character-name plate — decided by vocabulary
    #     rather than by POS tag, and repaired first for OCR homoglyphs. Runs
    #     before the general logic below because those homoglyphs ("ト7") stop
    #     the katakana run from matching at all.
    label = _label_name(text, overrides)
    if label is not None:
        return label

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
        # A run that STARTS with a small kana / ー is a word-fragment (ベッド misread
        # as べ + ッド), never a name — romanizing it yields garbage ("Xtsudo"), so
        # leave it for the model. See _KATA_NONINITIAL.
        if run[0] in _KATA_NONINITIAL:
            return run
        # A katakana run written as a verb/adjective stem — followed by hiragana
        # okurigana, e.g. シゴいて for しごいて — is not a name. The model reads bare
        # katakana like シゴ as a name ("Shigo takes …"), so rewrite it to hiragana
        # and let the model translate it as the verb it is.
        tail = _HIRAGANA_TAIL.match(text, match.end())
        if tail and _is_inflected(run + tail.group(0)):
            return _to_hiragana(run)
        return _romaji(run) if _is_name_run(run) else run

    return _KATAKANA_RUN.sub(katakana, text)


# Titles that follow a NAME in fantasy / noble-romance fiction (most of the Syosetu
# ranking): a katakana run right before one is a person or house, even when the
# analyzer can't tell (ドーンカソ侯爵令息 — the model otherwise spelled it two ways).
_NAME_TITLES_ZH = ("侯爵", "伯爵", "公爵", "男爵", "子爵", "辺境伯", "大公", "令嬢",
                   "令息", "殿下", "陛下", "閣下", "王子", "王女", "皇子", "皇女", "姫",
                   "卿", "嬢", "夫人", "家")


def _kata_to_hira(run: str) -> str:
    return "".join(chr(ord(c) - 0x60) if "ァ" <= c <= "ヶ" else c for c in run)


# Rough second-person pronouns written in katakana; a run starting with one is
# an insult/slang compound, never a name.
_SLANG_PREFIXES_ZH = ("オメー", "テメー", "オマエ", "テメエ", "キサマ", "コイツ", "アイツ", "ソイツ")


def _is_word_zh(run: str) -> bool:
    """True if a katakana run is (a form of) a dictionary word, not a name.

    Catches the slang/sound words syosetu testing turned into fake names: an
    elongated word (ゴリラー), a word written in katakana for tone (オジサマ),
    a verb stem (フザケ, チビリ), a mimetic (キリリ, ギョロ), and a compound of
    two words (バナナゼリー, スーパーマッチョ).
    """
    if run.startswith(_SLANG_PREFIXES_ZH):
        return True      # オメーポンギ "you Roppongi girl" — an insult, not a name
    hira = _kata_to_hira(run)
    trimmed = run.rstrip("ー")
    forms = {run, trimmed, hira, _kata_to_hira(trimmed), hira + "る",
             hira + "と", hira + "り", hira + "りと", hira * 2}
    if any(_is_headword(f) for f in forms if f):
        return True
    return any(_is_headword(run[:i]) and _is_headword(run[i:])
               for i in range(3, len(run) - 2))


def _unknown_name_zh(run: str) -> bool:
    """A katakana run the dictionary doesn't know is most likely a name.

    Novels introduce dozens of names the analyzer has never seen (マイスナー,
    ルナリア, ラグザス); leaving them to the model spelled the same character two
    or three ways in one chapter. A non-word of 3+ morae is transliterated so the
    spelling is fixed. Only when the dictionary is loaded (never guess blind);
    sound effects ending in ッ and anything `_is_word_zh` recognizes stay with
    the model.
    """
    if not _dictionary_ready():
        return False
    core = run.replace("ー", "")
    if len(core) < 3 or run.endswith(("ッ", "ｯ", "ァ", "ィ", "ゥ", "ェ", "ォ")):
        return False     # SFX / cries end on a small kana (ブヘェ), names don't
    for unit in (1, 2):
        # A repeated sound is a laugh or SFX (フフフフ, ドキドキ), never a name.
        if len(core) >= 2 * unit and core == core[:unit] * (len(core) // unit) + core[:len(core) % unit]:
            return False
    return not _is_word_zh(_to_fullwidth(run))


# Katakana runs already confirmed as names this session (see `_protect_zh`).
_LEARNED_ZH: set[str] = set()


def _protect_zh(text: str, overrides: dict, names_out: list | None = None) -> str:
    """Name handling for Chinese output.

    Tested approaches (Qwen3-4B, JA -> zh-CN):
      * romaji names (the English path) -> the model invents a Chinese name
        (Kazu -> 秋山) or leaves romaji in the Chinese ("Kasumi真的好可爱");
      * raw katakana -> mostly sensible transliteration, but it still invents names
        it doesn't know (ミナト -> 矿田), misreads some (ヤツシロ -> 那家伙, "that
        guy"), and transliterates the same name differently per line
        (アリサ -> 艾丽莎 / 阿里萨);
      * transliterating katakana names OURSELVES with a fixed sound table
        (`kana_zh`) -> no invented names, and the same name every time. Used here.

    So: katakana names -> hanzi via `kata_to_zh` (a per-language `names.json` pin
    wins); kanji names are left as kanji (a Chinese reader reads them directly);
    furigana keeps the kanji and drops the reading (鬼戮(きりく) -> 鬼戮). The
    English glossary is NOT applied — its replacements are English words.
    """
    from .kana_zh import kata_to_zh as _kata_to_zh

    def kata_to_zh(run: str) -> str:
        zh = _kata_to_zh(run)
        if names_out is not None and zh:
            names_out.append(zh)
        return zh

    # 0) Kanji-key overrides — direct substring replacement, longest first (as EN).
    for key in sorted((k for k in overrides if _HAS_KANJI.search(k)),
                      key=len, reverse=True):
        if key in text:
            text = text.replace(key, overrides[key])

    # 0a) Fixed Chinese terms (グリフォン 狮鹫, 屋敷 宅邸) and katakana slang
    #     pronouns (テメー -> 你) the model leaves as kana or renders differently
    #     per line. Longest first; a names.json entry for the same key wins.
    from .kana_zh import GLOSSARY_ZH, SLANG_YOU_ZH
    for key in sorted(GLOSSARY_ZH, key=len, reverse=True):
        if key in text and key not in overrides:
            text = text.replace(key, GLOSSARY_ZH[key])
    for key in SLANG_YOU_ZH:
        text = re.sub(re.escape(key) + r"(?![ァ-ヺー])", "你", text)

    # 達 after a katakana name is the plural (アルベルト達 "Albert and the others"),
    # not part of the name — the model wrote 阿尔贝尔特达. Spell it as たち, which the
    # honorific clean-up turns into 们.
    text = re.sub(r"(?<=[ァ-ヺー])達", "たち", text)

    # A lone 机 is "desk" in Japanese but "machine" in Chinese (机の上 -> 在机器上).
    text = re.sub(r"(?<![一-龯])机(?![一-龯])", "桌子", text)

    # 0b) A bare katakana line is a character-name plate.
    label = _label_name(text, overrides, to_name=kata_to_zh)
    if label is not None:
        return label

    # 1) Furigana: keep the kanji (or its override), drop the kana reading.
    text = _FURIGANA.sub(lambda m: overrides.get(m.group(1), m.group(1)), text)

    # 2) Katakana name runs -> Chinese transliteration.
    def katakana(match: "re.Match") -> str:
        run = match.group(0)
        if run in overrides:
            return overrides[run]
        if run in _LEARNED_ZH:
            return kata_to_zh(_to_fullwidth(run)) or run   # a name seen earlier
        if run[0] in _KATA_NONINITIAL:
            return run           # word fragment, never a name (see _KATA_NONINITIAL)
        tail = _HIRAGANA_TAIL.match(text, match.end())
        if tail and _is_inflected(run + tail.group(0)):
            return run           # stylized verb stem (ムカつく) — leave for the model
        # A katakana run right before a name honorific (ミナト+くん) is a person even
        # when the analyzer doesn't tag it as a proper noun.
        rest = text[match.end():]
        before_honorific = (any(rest.startswith(h) for h in _NAME_HONORIFICS)
                            or any(rest.startswith(t) for t in _NAME_TITLES_ZH)
                            # "ヨーコ・レンボウ": katakana joined by ・ is a full name.
                            or (rest[:1] == "・" and _KATAKANA_RUN.match(rest, 1) is not None)
                            or text[:match.start()].endswith("・"))
        # Stricter than the English path: only a dictionary PROPER NOUN counts on its
        # own. `_is_name_run` also accepts any UNKNOWN word, which in Chinese turned
        # katakana slang into fake names (オメーポンギ "you, Roppongi girl" -> 奥梅庞吉);
        # romaji in English is harmless, a hanzi "name" is not. Unknown words are left
        # for the model unless an honorific marks them as a person (ミナト+くん).
        if before_honorific or _is_proper_noun_run(run) or _unknown_name_zh(run):
            if before_honorific:
                # Strong evidence (an honorific/title or ・ full name): remember it,
                # so the same run alone later is still a name. インパチェンス・マーデン
                # makes インパチェンス a name; alone it is also a dictionary word
                # (the flower), which the model translated as 不可逆地 ("irreversibly").
                if len(_LEARNED_ZH) < 2000:
                    _LEARNED_ZH.add(run)
            return kata_to_zh(_to_fullwidth(run)) or run
        return run

    return _KATAKANA_RUN.sub(katakana, text)
