"""Offline JA->EN dictionary (JMdict) for the Read Mode hover popup.

Read Mode lets a learner hover any word in a captured sentence and see its
reading, part of speech, and English senses — the "deep dive" half of the
capture -> card flow. That needs a real dictionary, not the MT model: the model
gives one fluent gloss, but a learner wants the dictionary form, the readings,
and every sense. JMdict is that dictionary.

We ship no weights and no data in the repo (like the models — see main.py). On
first use this downloads the pre-parsed **jmdict-simplified** English release
(scriptin/jmdict-simplified, ~11 MB) and builds a small SQLite index in the
platform data dir; every later run just opens it. A `dict/jmdict.sqlite` bundled
next to the app (the Steam depot) wins and skips the download entirely, mirroring
the bundled `models/` HF cache.

    ┌ lookup(word, reading) ─ query the SQLite index ─ ranked entries
    └ ensure()             ─ open the DB, building it once if missing

LICENSE: the dictionary data is JMdict/JMnedict, from the Electronic Dictionary
Research and Development Group (EDRDG), used under the Creative Commons
Attribution-ShareAlike 4.0 licence. The ShareAlike term applies to *this data
file* (the built SQLite is a derivative of JMdict and stays CC BY-SA), NOT to the
application code. Attribution is surfaced in the Read Mode popup and the licenses
notice. See https://www.edrdg.org/ and https://github.com/scriptin/jmdict-simplified.

Everything is best-effort and guarded: if the download or build fails, lookups
return an empty/loading result and the rest of the app is unaffected.
"""

import io
import json
import sqlite3
import tarfile
import threading
import urllib.request
from pathlib import Path

import platformdirs

APP_NAME = "NihongoViewer"

# jmdict-simplified English full edition. The GitHub "latest release" exposes the
# asset as jmdict-eng-<version>.json.tgz (a single JSON inside a gzipped tar).
_RELEASE_API = "https://api.github.com/repos/scriptin/jmdict-simplified/releases/latest"
_ASSET_PREFIX = "jmdict-eng-"          # full edition (not "-common")
_ASSET_SUFFIX = ".json.tgz"

# SQLite schema version — bump to force a rebuild when the build logic changes.
_SCHEMA = 1

_DATA_DIR = Path(platformdirs.user_data_dir(APP_NAME, appauthor=False))
_DB_PATH = _DATA_DIR / "jmdict.sqlite"
# A dictionary bundled next to the app (Steam depot) is used as-is, so a shipped
# build never downloads. Mirrors main.py's bundled `models/` HF cache.
_BUNDLED_DB = Path(__file__).resolve().parent.parent / "dict" / "jmdict.sqlite"

# Human-readable attribution the UI shows at the point of use (satisfies the
# CC BY-SA attribution term inline, next to every lookup).
ATTRIBUTION = "JMdict · EDRDG · CC BY-SA 4.0"

# Non-content tokens we never build a lookup key for (punctuation / symbols).
_SKIP_POS1 = {"補助記号", "記号", "空白"}

# Compact labels for the JMdict part-of-speech codes, so the popup shows "noun"
# instead of "noun (common) (futsuumeishi)". Verb classes collapse by prefix
# (see _short_pos); anything unmapped falls back to the full JMdict tag text.
_POS_SHORT = {
    "n": "noun", "adv": "adverb", "adv-to": "adverb", "adj-na": "na-adj",
    "adj-i": "i-adj", "adj-ix": "i-adj", "adj-no": "adj", "adj-pn": "pre-noun adj",
    "adj-t": "adj", "adj-f": "adj", "exp": "expression", "int": "interjection",
    "prt": "particle", "conj": "conjunction", "pn": "pronoun", "pref": "prefix",
    "suf": "suffix", "ctr": "counter", "num": "numeric", "aux": "auxiliary",
    "aux-v": "aux verb", "aux-adj": "aux adj", "cop": "copula",
    "vt": "transitive", "vi": "intransitive", "vs": "+suru verb",
    "vs-i": "+suru verb", "vs-s": "+suru verb", "vk": "kuru verb",
    "vz": "zuru verb", "n-suf": "noun suffix", "n-pref": "noun prefix",
    "n-adv": "adverbial noun", "n-t": "temporal noun",
}

# Analyzer POS (UniDic pos1) -> the JMdict code prefixes that mean the same
# class. Used only to rank a homograph the way the sentence uses it: hovering
# sentence-ending ね should surface the *particle*, not the noun 根 ("root").
_POS1_TO_JMDICT = {
    "動詞": ("v",), "形容詞": ("adj-i", "adj-ix"), "形状詞": ("adj-na",),
    "副詞": ("adv",), "助詞": ("prt",), "助動詞": ("aux", "cop"),
    "接続詞": ("conj",), "連体詞": ("adj-pn",), "感動詞": ("int",),
    "代名詞": ("pn",), "接頭辞": ("pref", "n-pref"), "接尾辞": ("suf", "n-suf"),
    "名詞": ("n", "pn"),
}


def _short_pos(code: str) -> str:
    """Compact human label for a JMdict POS code (verb classes by prefix)."""
    if code in _POS_SHORT:
        return _POS_SHORT[code]
    if code.startswith("v5"):
        return "godan verb"
    if code.startswith("v1"):
        return "ichidan verb"
    if code.startswith("v2") or code.startswith("v4"):
        return "verb (arch.)"
    if code.startswith("v"):
        return "verb"
    return code


class Dictionary:
    """Lazy JMdict index backed by SQLite. Thread-safe (one connection per call)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()   # serializes the one-time build
        self._db_path: Path | None = None   # resolved on first ensure()
        self._status = "idle"           # idle | building | ready | error
        self._error: str | None = None

    # -- readiness ------------------------------------------------------------
    def status(self) -> dict:
        """Current build/load state for the UI ({state, error})."""
        return {"state": self._status, "error": self._error}

    def ensure(self) -> bool:
        """Make the index usable, building it once if needed. Returns readiness.

        Cheap after the first successful call. Safe to call from any thread and
        concurrently — the build is guarded by a lock and only runs once.
        """
        if self._status == "ready":
            return True
        with self._lock:
            if self._status == "ready":
                return True
            # A bundled DB wins and needs no download/build.
            if _BUNDLED_DB.is_file():
                self._db_path = _BUNDLED_DB
                self._status = "ready"
                return True
            if _DB_PATH.is_file():
                self._db_path = _DB_PATH
                self._status = "ready"
                return True
            self._status = "building"
            try:
                self._download_and_build(_DB_PATH)
                self._db_path = _DB_PATH
                self._status = "ready"
                return True
            except Exception as exc:  # network/parse/disk — degrade, don't crash
                self._error = str(exc)
                self._status = "error"
                return False

    # -- build ----------------------------------------------------------------
    def _download_and_build(self, db_path: Path) -> None:
        """Fetch the jmdict-simplified JSON and build the SQLite index at db_path."""
        url = self._resolve_asset_url()
        req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=180) as resp:
            blob = resp.read()
        with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tf:
            member = next((m for m in tf.getmembers() if m.name.endswith(".json")), None)
            if member is None:
                raise RuntimeError("no JSON found in the JMdict release archive")
            data = json.load(tf.extractfile(member))

        db_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = db_path.with_suffix(".sqlite.tmp")
        if tmp.exists():
            tmp.unlink()
        conn = sqlite3.connect(tmp)
        try:
            self._create_schema(conn)
            self._populate(conn, data)
            conn.commit()
        finally:
            conn.close()
        tmp.replace(db_path)  # atomic on the same filesystem

    def _resolve_asset_url(self) -> str:
        req = urllib.request.Request(_RELEASE_API, headers={"User-Agent": APP_NAME})
        with urllib.request.urlopen(req, timeout=60) as resp:
            release = json.load(resp)
        for asset in release.get("assets", []):
            name = asset.get("name", "")
            if name.startswith(_ASSET_PREFIX) and name.endswith(_ASSET_SUFFIX) \
                    and "common" not in name:
                return asset["browser_download_url"]
        raise RuntimeError("JMdict English release asset not found")

    @staticmethod
    def _create_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            PRAGMA journal_mode = OFF;
            PRAGMA synchronous = OFF;
            CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE entries (id INTEGER PRIMARY KEY, common INTEGER, data TEXT);
            CREATE TABLE lookup (key TEXT, entry INTEGER, kind INTEGER);
            """
        )

    def _populate(self, conn: sqlite3.Connection, data: dict) -> None:
        conn.execute("INSERT INTO meta VALUES ('schema', ?)", (str(_SCHEMA),))
        conn.execute("INSERT INTO meta VALUES ('version', ?)",
                     (str(data.get("version", "")),))
        conn.execute("INSERT INTO meta VALUES ('dictDate', ?)",
                     (str(data.get("dictDate", "")),))
        conn.execute("INSERT INTO meta VALUES ('tags', ?)",
                     (json.dumps(data.get("tags", {}), ensure_ascii=False),))

        entry_rows: list[tuple] = []
        lookup_rows: list[tuple] = []
        for entry in data.get("words", []):
            kanji = [k["text"] for k in entry.get("kanji", []) if k.get("text")]
            kana = [r["text"] for r in entry.get("kana", []) if r.get("text")]
            common = (any(k.get("common") for k in entry.get("kanji", []))
                      or any(r.get("common") for r in entry.get("kana", [])))
            senses = []
            for s in entry.get("sense", []):
                glosses = [g["text"] for g in s.get("gloss", [])
                           if g.get("lang", "eng") == "eng" and g.get("text")]
                if not glosses:
                    continue
                senses.append({"pos": s.get("partOfSpeech", []), "gloss": glosses})
            if not senses:
                continue
            try:
                eid = int(entry["id"])
            except (KeyError, ValueError, TypeError):
                continue
            payload = {"k": kanji, "r": kana, "common": bool(common), "senses": senses}
            entry_rows.append((eid, 1 if common else 0,
                               json.dumps(payload, ensure_ascii=False)))
            for text in kanji:
                lookup_rows.append((text, eid, 0))
            for text in kana:
                lookup_rows.append((text, eid, 1))

        conn.executemany("INSERT INTO entries VALUES (?, ?, ?)", entry_rows)
        conn.executemany("INSERT INTO lookup VALUES (?, ?, ?)", lookup_rows)
        conn.execute("CREATE INDEX idx_lookup_key ON lookup(key)")

    # -- query ----------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection | None:
        """Open a fresh read-only connection (one per call keeps threads simple)."""
        if self._db_path is None:
            return None
        try:
            uri = f"file:{self._db_path.as_posix()}?mode=ro"
            return sqlite3.connect(uri, uri=True)
        except sqlite3.Error:
            return None

    def lookup(self, word: str, reading: str | None = None,
               pos: str | None = None, limit: int = 6) -> list[dict]:
        """Ranked dictionary entries for `word` (falls back to `reading`).

        `reading` and `pos` (the analyzer's UniDic pos1 for the hovered token) are
        used only to rank homographs the way the sentence uses them. Each entry:
        {kanji, reading, common, senses:[{pos:[label], glosses:[str]}]}. Empty list
        when nothing matches or the index isn't ready.
        """
        word = (word or "").strip()
        if not word or not self.ensure():
            return []
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = self._match(conn, word)
            if not rows and reading:
                rows = self._match(conn, reading.strip())
        except sqlite3.Error:
            return []
        finally:
            conn.close()

        # Rank: prefer the entry whose word class matches how the analyzer tagged
        # the token (sentence-ending ね -> particle, not the noun 根), then common
        # words, then an entry whose reading matches the hovered token's reading.
        rd = (reading or "").strip()
        want = _POS1_TO_JMDICT.get(pos or "", ())
        def score(e: dict) -> tuple:
            codes = [c for s in e.get("senses", []) for c in s.get("pos", [])]
            class_match = int(bool(want) and any(
                c.startswith(want) for c in codes))
            return (class_match,
                    1 if e.get("common") else 0,
                    1 if rd and rd in e.get("r", []) else 0)
        rows.sort(key=score, reverse=True)

        out: list[dict] = []
        for e in rows[:limit]:
            senses = []
            for s in e.get("senses", []):
                # Compact + de-duped POS labels ("noun", "transitive"), order kept.
                labels: list[str] = []
                for code in s.get("pos", []):
                    label = _short_pos(code)
                    if label not in labels:
                        labels.append(label)
                senses.append({"pos": labels, "glosses": s.get("gloss", [])})
            out.append({
                "kanji": e["k"][0] if e.get("k") else "",
                "reading": e["r"][0] if e.get("r") else "",
                "common": bool(e.get("common")),
                "senses": senses,
            })
        return out

    def gloss(self, word: str, reading: str | None = None,
              pos: str | None = None, max_glosses: int = 3) -> str:
        """Concise English gloss for `word` if it's a JMdict headword, else "".

        Used by the translator to hand a bare vocabulary word its real dictionary
        meaning (a sentence-MT model romanizes rare/compound words it doesn't know,
        e.g. 足コキ -> "Foot Koki"). Non-blocking: returns "" unless the index is
        already built, so it never stalls a translation on the one-time build —
        the caller just falls back to the model.
        """
        if self._status != "ready":
            return ""
        entries = self.lookup(word, reading, pos, limit=1)
        if not entries:
            return ""
        senses = entries[0].get("senses") or []
        if not senses:
            return ""
        return "; ".join((senses[0].get("glosses") or [])[:max_glosses])

    @staticmethod
    def _match(conn: sqlite3.Connection, key: str) -> list[dict]:
        """Distinct entries whose kanji or kana form equals `key` exactly."""
        cur = conn.execute(
            "SELECT DISTINCT e.id, e.data FROM lookup l "
            "JOIN entries e ON e.id = l.entry WHERE l.key = ?", (key,))
        seen: set[int] = set()
        entries: list[dict] = []
        for eid, data in cur.fetchall():
            if eid in seen:
                continue
            seen.add(eid)
            try:
                entries.append(json.loads(data))
            except (json.JSONDecodeError, TypeError):
                continue
        return entries


# Module-level singleton so the whole app shares one index (and one build).
_INSTANCE: Dictionary | None = None


def get() -> Dictionary:
    global _INSTANCE
    if _INSTANCE is None:
        _INSTANCE = Dictionary()
    return _INSTANCE


def ensure() -> bool:
    return get().ensure()


def status() -> dict:
    return get().status()


def lookup(word: str, reading: str | None = None,
           pos: str | None = None, limit: int = 6) -> list[dict]:
    return get().lookup(word, reading, pos, limit)


def gloss(word: str, reading: str | None = None,
          pos: str | None = None, max_glosses: int = 3) -> str:
    return get().gloss(word, reading, pos, max_glosses)
