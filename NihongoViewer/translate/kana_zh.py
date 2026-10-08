"""Katakana name -> Simplified-Chinese phonetic transliteration (音译).

Used when translating into Chinese. Romaji is wrong there (a Chinese line with
"Kazu" in it reads badly), and leaving the katakana for the model doesn't work
either: Qwen *invents* a Chinese name for a katakana name it doesn't know
(カズ -> 秋山, ミナト -> 矿田) and gives the same character different names on
different lines (アリサ -> 艾丽莎 / 阿里萨). A fixed sound table instead gives the
same name every time and can never make one up — the same reason the English path
romanizes names. The mapping follows common Chinese fan-translation practice
(カ卡 ズ兹 シ西 ロ罗 …); a user can pin any name exactly in `names.json`.

Pure data + one function — no dependencies.
"""

# Two-mora syllables ending in ン read best as one character (ケン 肯, not 克恩).
_N_SYLLABLES = {
    "アン": "安", "イン": "因", "ウン": "温", "エン": "恩", "オン": "昂",
    "カン": "坎", "キン": "金", "クン": "坤", "ケン": "肯", "コン": "孔",
    "ガン": "甘", "ギン": "银", "ゲン": "根", "ゴン": "贡",
    "サン": "桑", "シン": "新", "スン": "孙", "セン": "森", "ソン": "松",
    "ザン": "赞", "ジン": "金", "ゼン": "禅", "ゾン": "宗",
    "タン": "坦", "チン": "琴", "テン": "滕", "トン": "通",
    "ダン": "丹", "デン": "登", "ドン": "东",
    "ナン": "南", "ニン": "宁", "ネン": "嫩", "ノン": "农",
    "ハン": "汉", "ヒン": "欣", "フン": "芬", "ヘン": "亨", "ホン": "洪",
    "バン": "班", "ビン": "宾", "ベン": "本", "ボン": "邦",
    "パン": "潘", "ピン": "平", "ペン": "彭", "ポン": "庞",
    "マン": "曼", "ミン": "明", "メン": "门", "モン": "蒙",
    "ラン": "兰", "リン": "琳", "ルン": "伦", "レン": "伦", "ロン": "隆",
    "ワン": "万", "ユン": "允", "ジュン": "俊", "シュン": "顺", "リョン": "良",
    # Nobility particle: German "von" is always 冯 in Chinese (エルゼ・フォン・… 冯).
    "フォン": "冯", "ヴォン": "冯",
}

# Contracted sounds (拗音) and the extended katakana used for foreign names.
_COMBOS = {
    "キャ": "卡", "キュ": "丘", "キョ": "京", "ギャ": "加", "ギュ": "久", "ギョ": "乔",
    "シャ": "夏", "シュ": "修", "ショ": "肖", "ジャ": "贾", "ジュ": "朱", "ジョ": "乔",
    "チャ": "查", "チュ": "丘", "チョ": "乔", "ニャ": "娘", "ニュ": "纽", "ニョ": "妞",
    "ヒャ": "夏", "ヒュ": "休", "ヒョ": "表", "ビャ": "比亚", "ビュ": "比由", "ビョ": "比奥",
    "ピャ": "皮亚", "ピュ": "皮由", "ピョ": "皮奥", "ミャ": "米亚", "ミュ": "缪", "ミョ": "苗",
    "リャ": "里亚", "リュ": "琉", "リョ": "良",
    "ヴァ": "瓦", "ヴィ": "维", "ヴェ": "维", "ヴォ": "沃",
    "ファ": "法", "フィ": "菲", "フェ": "费", "フォ": "福",
    "ティ": "蒂", "ディ": "迪", "トゥ": "图", "ドゥ": "杜",
    "ウィ": "维", "ウェ": "韦", "ウォ": "沃", "シェ": "谢", "ジェ": "杰", "チェ": "切",
    "ツァ": "察", "イェ": "耶",
}

# Single morae. リ 里 / レ 莱 are gender-neutral on purpose: 莉 / 蕾 read as female,
# which turned male names feminine (レオナルド 蕾奥纳鲁多, グレイソン).
_BASE = dict(zip(
    "アイウエオカキクケコガギグゲゴサシスセソザジズゼゾタチツテトダヂヅデド"
    "ナニヌネノハヒフヘホバビブベボパピプペポマミムメモヤユヨラリルレロワヲヴ",
    "阿伊乌艾奥卡基库克科加吉古格戈萨西斯塞索扎吉兹泽佐塔奇茨特托达吉兹德多"
    "纳尼努内诺哈希富赫霍巴比布贝博帕皮普佩波玛米姆梅莫亚尤约拉里鲁莱罗瓦沃维",
))

# Small vowels on their own (after a combo didn't match) and similar marks that
# carry no extra sound in a transliteration.
_SILENT = set("ッーァィゥェォャュョヮ")


# Word-final mora -> bare-consonant hanzi, for Western-style names only.
_FINAL_CONSONANT = {"ド": "德", "ト": "特", "ク": "克", "グ": "格", "フ": "夫", "ブ": "布"}


_CLUSTER = {"ク": "克", "グ": "格", "ブ": "布", "プ": "普", "フ": "弗", "ト": "特", "ド": "德"}
_R_ROW = set("ラリレロ")   # not ル: クルミ (Kurumi) is a Japanese name, not "Cr-"
_E_ROW = set("エケゲセゼテデネヘベペメレ")


def _is_western(run: str) -> bool:
    """Foreign-style name: long vowel, ヴ, small-vowel combos, ・, or a -ルド/-ルト end."""
    return (any(c in run for c in "ーヴァィゥェォ・")
            or run.endswith(("ルド", "ルト", "ンド", "ント")))


def _word_end(run: str, j: int) -> bool:
    """True if everything from j on is silent (end of a word / before ・)."""
    rest = run[j:].lstrip("ッー")
    return not rest or rest[0] == "・"


def kata_to_zh(run: str) -> str:
    """Transliterate a (fullwidth) katakana name into Chinese characters.

    Longest match first: 3-char ン syllables (ジュン), 2-char ン syllables (ケン),
    contracted/extended sounds (シャ, ファ), then single morae. ッ / ー and stray
    small vowels are dropped; a lone ン becomes 恩. Unmappable characters are
    dropped rather than guessed. Returns "" only for an empty/unmappable run.
    """
    western = _is_western(run)
    out: list[str] = []
    i = 0
    while i < len(run):
        if western and run[i] in _FINAL_CONSONANT and _word_end(run, i + 1):
            # Western names end on a bare consonant: ガーランド 加兰德, not 加兰多.
            out.append(_FINAL_CONSONANT[run[i]])
            i += 1
            continue
        for size, table in ((3, _N_SYLLABLES), (2, _N_SYLLABLES), (2, _COMBOS)):
            seg = run[i:i + size]
            if len(seg) == size and seg in table:
                out.append(table[seg])
                i += size
                break
        else:
            ch = run[i]
            if (ch in _CLUSTER and run[i + 1:i + 2] in _R_ROW
                    and (i == 0 or western or run[i - 1] in "ンーッ")):
                # Consonant cluster (Gr-, Cl-, Br-, Tr-): グレン 格伦, クレア 克莱亚,
                # not 古伦 / 库莱亚. Mid-word only for Western-style runs or after
                # ン/ー/ッ (アンドリュー), so a Japanese name (ミドリ 米多里) keeps its vowel.
                out.append(_CLUSTER[ch])
            elif ch == "・":
                out.append("·")          # name separator: 艾尔泽·冯·贝尔里茨
            elif ch == "ア" and i > 0:
                out.append("亚")         # マリア 玛里亚, アナスタジア
            elif ch == "イ" and i > 0 and run[i - 1] in _E_ROW:
                pass                     # レイ / グレイ: a diphthong, not a new mora
            elif ch == "ン":
                out.append("恩")
            elif ch == "ル" and i > 0:
                # Chinese convention for Western-style names writes a non-initial ル
                # as 尔 (アルベール 阿尔贝尔, ジルベール 吉尔贝尔), not 鲁 ("Aruberu").
                out.append("尔")
            elif ch in _BASE:
                out.append(_BASE[ch])
            # else: silent mark (ッ ー ・ small vowel) or unmappable — skip it
            i += 1
    return "".join(out)


# --- Output clean-up for Chinese translations --------------------------------
#
# Measured on 144 real manga lines translated JA -> zh-CN: no empty results and no
# drift into Traditional Chinese, but two narrow leaks, both from NAMES:
#   * an honorific left as kana after a name (新ちゃん, should be 新酱);
#   * a Japanese-only kanji form kept in a name (嶋田, Chinese writes 岛田).
# `fix_zh_output` repairs exactly those; ordinary sentences are left untouched.

# Honorific kana that can survive after a name, in Chinese fan-translation style.
# Longest first so さま is handled before さん-style prefixes of it.
_HONORIFICS_ZH = [
    ("ちゃん", "酱"), ("チャン", "酱"), ("くん", "君"), ("クン", "君"),
    ("さま", "大人"), ("サマ", "大人"), ("さん", "桑"), ("サン", "桑"),
    ("せんぱい", "前辈"), ("センパイ", "前辈"), ("たん", "酱"),
    # Plural after a name (紫ちゃんたち -> 紫酱们).
    ("たち", "们"), ("タチ", "们"),
]

# Japanese-only kanji forms (shinjitai / name variants) -> Simplified Chinese. Each
# key is never correct in Simplified text, so the swap is always safe. Covers the
# forms common in Japanese names plus everyday shinjitai the model might copy.
_JA_TO_ZH = str.maketrans({
    # name variants
    "嶋": "岛", "島": "岛", "沢": "泽", "澤": "泽", "浜": "滨", "濱": "滨",
    "辺": "边", "邊": "边", "斎": "斋", "齋": "斋", "広": "广", "廣": "广",
    "髙": "高", "桜": "樱", "櫻": "樱", "竜": "龙", "龍": "龙", "恵": "惠",
    "瀬": "濑", "實": "实", "賀": "贺", "條": "条",
    # everyday shinjitai
    "気": "气", "様": "样", "楽": "乐", "帰": "归", "図": "图", "発": "发",
    "単": "单", "戦": "战", "変": "变", "悪": "恶", "関": "关", "読": "读",
    "売": "卖", "続": "续", "絵": "绘", "鉄": "铁", "黒": "黑", "県": "县",
    "薬": "药", "歳": "岁", "雑": "杂", "実": "实", "対": "对", "応": "应",
    "険": "险", "験": "验", "検": "检", "剣": "剑", "弾": "弹", "転": "转",
    "軽": "轻", "鶏": "鸡", "隣": "邻", "価": "价", "経": "经", "継": "继",
    "総": "总", "縁": "缘", "闘": "斗", "拡": "扩", "稲": "稻", "掲": "揭",
    "姉": "姐", "従": "从", "徳": "德", "顔": "颜", "頭": "头",
    "駆": "驱", "駐": "驻", "駄": "驮", "説": "说", "話": "话", "語": "语",
    "誰": "谁", "時": "时", "間": "间", "聞": "闻", "開": "开",
    "閉": "闭", "門": "门", "東": "东", "車": "车", "馬": "马", "鳥": "鸟",
    "魚": "鱼", "見": "见", "貝": "贝", "長": "长", "飲": "饮", "飯": "饭",
})

_KANA = "ぁ-ゖァ-ヺー"


def fix_zh_output(text: str) -> str:
    """Repair the name-related leaks Qwen leaves in Simplified-Chinese output.

    1. Honorific kana after a name -> Chinese honorific (新ちゃん -> 新酱).
    2. Japanese-only kanji forms -> Simplified (嶋田 -> 岛田).
    Other text is unchanged.
    """
    if not text:
        return text
    # 令嬢 is Japanese-only; Chinese says 千金 (嬢 alone after a name: 小姐).
    text = text.replace("令嬢", "千金").replace("嬢", "小姐")
    for kana, zh in _HONORIFICS_ZH:
        text = text.replace(kana, zh)
    text = text.translate(_JA_TO_ZH)
    # 3. A small ッ/っ is a Japanese emphasis/cut-off mark (俺の…ッ♡) with no
    #    Chinese equivalent; the model sometimes copies it through. Drop it.
    text = text.replace("ッ", "").replace("っ", "")
    return _fix_zh_punct(to_simplified(text))


_CJK_CHAR = r"[　-〿一-鿿＀-￯…]"
_ASCII_TO_FULL = {",": "，", "!": "！", "?": "？", ":": "：", ";": "；"}


def _fix_zh_punct(text: str) -> str:
    """Normalize punctuation in a Chinese line.

    * ASCII , ! ? : ; touching Chinese text -> full-width (Chinese typography).
    * A comma/pause stuck after a sentence end collapses (吗？, -> 吗？) — the source's
      "?、" made the model emit both.
    * A trailing comma/pause at the very end is dropped.
    """
    import re

    # Whole runs (!! / ?!), so a doubled mark isn't left half-converted (！!).
    text = re.sub(rf"(?<={_CJK_CHAR})[,!?:;]+|[,!?:;]+(?={_CJK_CHAR})",
                  lambda m: "".join(_ASCII_TO_FULL[c] for c in m.group(0)), text)
    text = re.sub(r"([。！？…])\s*[，、,]+", r"\1", text)
    # An ASCII period after a full-width end mark (什么啊！.) is noise.
    text = re.sub(r"([。！？])\.+", r"\1", text)
    text = re.sub(r"([，、])[，、]+", r"\1", text)    # doubled comma (龙，，这次)
    return re.sub(r"[，、,]+\s*$", "", text)




def zh_leaked(source: str, out: str) -> bool:
    """True if a Chinese translation leaked another language.

    * any kana — Chinese never contains it, so kana means Japanese came through
      untranslated (言い逃れはできんぞ -> 逃走はできないぞ);
    * an English word (3+ letters) that is NOT in the source — the model slipping into
      English mid-sentence (…， apparently 并不是…). Latin text that IS in the source
      (a name, "OK") is legitimately copied and doesn't count.
    """
    import re as _re

    if not out:
        return False
    if _re.search(r"[ぁ-ゖァ-ヺ]", out) or _re.search(_FOREIGN, out):
        return True
    src = (source or "").lower()
    return any(w.lower() not in src for w in _re.findall(r"[A-Za-z]{3,}", out))


# --- Deterministic guards for things the 4B model flips -----------------------
#
# Syosetu testing (50 novels) found a few error kinds that prompt tweaks can't fix
# reliably (see CLAUDE.md, "don't tune the Chinese prompt"), so they are checked
# and repaired with plain rules instead.

# Rank / title words. Chinese writes them with the same characters, so the source
# word must appear verbatim in the output. Grouped into families the model confuses
# (子爵 -> 男爵, 陛下 -> 殿下); a swap inside one family is repaired.
_TITLE_FAMILIES = (
    ("公爵", "侯爵", "伯爵", "子爵", "男爵"),
    ("陛下", "殿下", "阁下"),
    ("国王", "王妃", "王子", "王女", "公主", "皇帝", "皇后", "皇子", "皇女"),
)
_TITLE_JA_TO_ZH = {"閣下": "阁下", "辺境伯": "边境伯"}


def _titles(text: str) -> list[str]:
    for ja, zh in _TITLE_JA_TO_ZH.items():
        text = text.replace(ja, zh)
    return [t for fam in _TITLE_FAMILIES for t in fam if t in text]


def fix_titles(source: str, out: str) -> str:
    """Swap a wrong rank word back (子爵 in the source, 男爵 in the output)."""
    src = _titles(source)
    for fam in _TITLE_FAMILIES:
        want = [t for t in fam if t in src]
        wrong = [t for t in fam if t in out and t not in src]
        missing = [t for t in want if t not in out]
        if len(missing) == 1 and len(wrong) == 1:
            out = out.replace(wrong[0], missing[0])
    # Name+様 came out as 先生 ("teacher/Mr.") even for women (エリザベート様).
    # 大人 is the gender-neutral Chinese rendering of 様.
    if "様" in source and "先生" not in source:
        out = out.replace("先生", "大人")
    return out


def titles_mismatch(source: str, out: str) -> bool:
    return any(t not in out for t in _titles(source))


_KANJI_DIGIT = {"〇": 0, "零": 0, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                "六": 6, "七": 7, "八": 8, "九": 9, "两": 2, "兩": 2}
_KANJI_UNIT = {"十": 10, "百": 100, "千": 1000}
_KANJI_BIG = {"万": 10**4, "萬": 10**4, "億": 10**8, "亿": 10**8}


def _numbers(text: str) -> set[int]:
    """Numbers written in digits or kanji numerals (十一 -> 11, 3 -> 3)."""
    import re

    text = text.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    found = {int(n) for n in re.findall(r"\d+", text)}
    for run in re.findall(r"[〇零一二三四五六七八九两兩十百千万萬億亿]+", text):
        if len(run) == 1 and run in "一二":
            continue     # 一 / 二 alone are mostly words (一人, 一緒), not counts
        # 万 / 億 multiply everything before them (二千八百万 = 28,000,000), unlike
        # 十 / 百 / 千 which only scale the digit right before them.
        big = section = cur = 0
        for ch in run:
            if ch in _KANJI_DIGIT:
                cur = cur * 10 + _KANJI_DIGIT[ch] if cur else _KANJI_DIGIT[ch]
            elif ch in _KANJI_BIG:
                big += ((section + cur) or 1) * _KANJI_BIG[ch]
                section = cur = 0
            else:
                section += (cur or 1) * _KANJI_UNIT[ch]
                cur = 0
        found.add(big + section + cur)
    return found


_NUM_RUN = r"[〇零一二三四五六七八九两兩十百千万萬億亿]{2,}"


def fix_numbers(source: str, out: str) -> str:
    """Swap a wrong kanji number back when it is unambiguous.

    二千八百万 (28 million) came out as 两亿八千万 (280 million) and the retry repeated
    it. With exactly one count in the source and exactly one count in the output that
    doesn't match, the source's numeral is put in its place (kanji numerals read
    the same in Chinese; 萬/億 become 万/亿).
    """
    import re

    src_runs = [r for r in re.findall(_NUM_RUN, source) if max(_numbers(r), default=0) >= 3]
    src_vals = _numbers(source)
    bad = [m for m in re.finditer(_NUM_RUN, out) if not (_numbers(m.group(0)) & src_vals)]
    if len(src_runs) == 1 and len(bad) == 1:
        zh = src_runs[0].replace("萬", "万").replace("億", "亿")
        m = bad[0]
        out = out[:m.start()] + zh + out[m.end():]
    return out


def numbers_mismatch(source: str, out: str) -> bool:
    """True if a count of 3+ in the source is missing from the output (十一 -> 十二)."""
    want = {n for n in _numbers(source) if n >= 3}
    return bool(want - _numbers(out))


_T2S = None


def to_simplified(text: str) -> str:
    """Traditional -> Simplified (OpenCC t2s, Apache-2.0). The retry prompt in
    particular drifted into Traditional forms (説話 / 這個)."""
    global _T2S
    try:
        if _T2S is None:
            import opencc
            _T2S = opencc.OpenCC("t2s")
        return _T2S.convert(text)
    except Exception:
        return text


_S2TW = None

# In a transliterated NAME these stay as is: 里 is the standard name syllable in
# Taiwan too (魯納里亞), but the converter reads it as "inside" (裡); 托 would become
# 託 ("entrust"). Every other name character takes its normal Traditional form.
_NAME_KEEP_TW = {"里", "托"}


def to_traditional_tw(text: str, names=()) -> str:
    """Simplified -> Taiwan Traditional (OpenCC s2tw: characters + Taiwan forms).

    zh-TW output is produced exactly like zh-CN — same prompt, names, glossary and
    guards, all of which work on Simplified text — and converted here as the very
    last step (华丽 -> 華麗). Asking the 4B model for Traditional directly would need
    its own prompt and re-testing, and `to_simplified` would undo it anyway.

    s2tw, not s2twp: the phrase-level "Taiwan vocabulary" mode is tuned for computer
    terms and broke ordinary prose in testing (通过 "pass through" -> 透過 "via",
    文件 documents -> 檔案 computer files, 连接 -> 連線 "online").

    `names` are the names in this line: a transliterated name (str) is converted
    character by character with `_NAME_KEEP_TW`, so 贝尔里内特 -> 貝爾里內特, not
    貝爾裡內特; a user's zh-TW pin, given as (simplified, pin), is written back
    exactly as pinned.
    """
    global _S2TW
    try:
        if _S2TW is None:
            import opencc
            _S2TW = opencc.OpenCC("s2tw")
        exact = {}
        for n in names:
            cn, pin = n if isinstance(n, tuple) else (n, None)
            if cn and cn in text and (pin or cn not in exact):
                exact[cn] = pin
        held = sorted(exact, key=len, reverse=True)
        for i, cn in enumerate(held):
            text = text.replace(cn, chr(0xE000 + i))
        text = _S2TW.convert(text)
        for i, cn in enumerate(held):
            tw = exact[cn] or "".join(c if c in _NAME_KEEP_TW else _S2TW.convert(c)
                                      for c in cn)
            text = text.replace(chr(0xE000 + i), tw)
        return text
    except Exception:
        return text


# English words that survived even the retry, with their Chinese.
_EN_LEAK_ZH = {"something": "什么", "maid": "女仆", "maids": "女仆们",
               "griffin": "狮鹫", "griffon": "狮鹫", "apparently": "似乎",
               "really": "真的", "okay": "好吧", "please": "请",
               "damn": "可恶",
               # Short replies the model sometimes answers in English (……そうよね -> "yeah").
               "yeah": "是啊", "yes": "是的", "yep": "嗯", "nope": "不", "hmm": "嗯",
               "well": "嗯", "sorry": "抱歉", "thanks": "谢谢", "right": "对吧",
               "debut": "初次亮相",
               # Negations: dropping one flips the meaning (弹回来 isn't 这样吗).
               "isn't": "不是", "not": "不", "don't": "不要", "can't": "不能", "won't": "不会", "shit": "可恶", "fuck": "妈的", "stick": "摇杆"}

# Characters from scripts Chinese never uses (Arabic, Cyrillic, Hangul, Thai…).
_FOREIGN = "[؀-ۿЀ-ӿ가-힯ᄀ-ᇿ฀-๿]"


def scrub_leaks(source: str, out: str) -> str:
    """Last resort when the retry also leaked.

    * stray foreign-script characters dropped (an Arabic comma ، -> ，);
    * katakana the model copied back into a name (米里ム) transliterated;
    * known English words mapped (maid -> 女仆), any other English word that is
      not in the source dropped (…却 strangely 并不觉得冷, …给你 isn't that simple?)
      — the Chinese around it carries the meaning, and a gap reads better than an
      English fragment in a Chinese line.
    """
    import re

    out = out.replace("،", "，").replace("؟", "？")
    out = re.sub(_FOREIGN, "", out)
    out = re.sub(r"[ァ-ヺー]+", lambda m: kata_to_zh(m.group(0)) or m.group(0), out)
    src = (source or "").lower()

    def word(m: "re.Match") -> str:
        w = m.group(0)
        if w.lower() in src:
            return w
        return _EN_LEAK_ZH.get(w.lower(), "")
    out = re.sub(r"[A-Za-z]+(?:'[A-Za-z]+)?", word, out)
    out = re.sub(r"\s+(?=[，。？！、…,.?!]|$)", "", out)            # space left before punctuation
    out = re.sub(r"(?<=[一-鿿，。？！])\s+(?=[一-鿿])", "", out)  # space between hanzi
    return _fix_zh_punct(out)


# Terms the model renders inconsistently or wrongly, substituted into the SOURCE
# before translation (zh only). Kept small and literal: each was seen wrong in
# testing (グリフォン -> Griffin / 龙骑士 / 龙 on three lines; 奥様 -> 先生).
# A user can add or override terms in names.json.
GLOSSARY_ZH = {
    "グリフォン": "狮鹫", "御者": "车夫", "屋敷": "宅邸", "メイド": "女仆",
    "奥様": "夫人", "奥さま": "夫人",
    # Genre staple; came out as 丑恶女巫小姐 / 反派小姐 in testing.
    "悪役令嬢": "反派千金", "悪役令息": "反派少爷",
    # Web-novel slang the model misread: ネコミミ -> 内科米米 (taken as a name),
    # ざまぁ物 -> 垃圾作品 ("trash"), 自サバ女 -> SB女 (an insult).
    "ネコミミ": "猫耳", "ねこみみ": "猫耳",
    "ざまぁ物": "打脸文", "ざまぁ系": "打脸系", "ざまぁ": "打脸",
    "自サバ女": "自称爽快女",
    # Otaku loanwords the model transliterated (哈蕾姆, 拉诺贝).
    "ハーレム": "后宫", "ラノベ": "轻小说", "ライトノベル": "轻小说",
    # Fantasy staples: fixed standard terms instead of a per-line transliteration
    # (ゴーレム -> 戈莱姆, ミスリル -> 米斯里尔 in testing).
    "ゴーレム": "魔像", "ミスリル": "秘银", "オリハルコン": "山铜", "ゴブリン": "哥布林",
    "スライム": "史莱姆", "ドワーフ": "矮人", "エルフ": "精灵", "ポーション": "药水",
    "指輪": "戒指",                         # was left as the Japanese word 指轮
    # Noble-romance staples: デビュタント came out as English "debut" (then dropped),
    # 貴族令嬢 as 贵公子小姐 ("young nobleman miss").
    "デビュタント": "社交界初次亮相", "貴族令嬢": "贵族千金",
    # Foods the model swapped for a different food (ravioli -> 拉面, celery -> 胡萝卜).
    "ラビオリ": "意式饺子", "セロリ": "芹菜", "フィナンシェ": "费南雪",
    "サムズアップ": "点赞", "ミニロボ": "迷你机器人",   # loanwords taken as names (萨姆兹亚普)
    "おち○ちん": "小鸡鸡", "おちん○ん": "小鸡鸡", "ち○こ": "鸡巴",
}

# Rough katakana pronouns -> 你, only when standing alone (not オメーポンギ).
SLANG_YOU_ZH = ("テメー", "テメエ", "オメー", "オメエ")


_NAME_END = r"(?=[，。！？、…”」 的了和与是在而对说也就都还又却把被让给跟向从们]|$)"


def restore_names(out: str, names: list[str]) -> str:
    """Put back a transliterated name the model respelled into a "familiar" form.

    We insert a fixed spelling (鲁纳里亚, 富兰切斯卡, 玛里亚) but the model sometimes
    swaps one character for a common variant (卢纳里亚, 弗兰切斯卡, 玛丽亚) — so the
    same character drifted between lines again. A same-length run of Han characters
    differing from an inserted name in exactly one position is restored. A 3-char
    name must keep its first AND last character (玛丽亚), so ordinary words that
    happen to share two characters with a short name are left alone.
    """
    for name in sorted(set(names), key=len, reverse=True):
        n = len(name)
        if n >= 3 and name not in out:
            # Last character dropped (斯巴 for 斯巴尔) or two neighbours swapped
            # (西塞尔 for 塞西尔): only when our full spelling is absent from the line.
            swaps = {name[:i] + name[i + 1] + name[i] + name[i + 2:] for i in range(n - 1)}
            swaps.discard(name)
            for v in sorted(swaps):
                out = out.replace(v, name)
            if name not in out:
                import re
                # The shortened form must end where a name would: before punctuation
                # or a common function word, so 斯巴达 (Sparta) isn't touched.
                out = re.sub(re.escape(name[:-1]) + _NAME_END, name, out)
        i = 0
        while i <= len(out) - n:
            w = out[i:i + n]
            ends = (w[0] == name[0] and w[-1] == name[-1]) if n == 3                 else (w[0] == name[0] or w[-1] == name[-1])
            if (w != name and ends and all("一" <= c <= "鿿" for c in w)
                    and sum(a != b for a, b in zip(w, name)) == 1):
                out = out[:i] + name + out[i + n:]
                i += n
            else:
                i += 1
    return out
