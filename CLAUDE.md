# CLAUDE.md

Guidance for Claude Code (and human contributors) working in this repository.

## What we're building

**NihongoViewer** is an offline, real-time screen translator for Japanese games and
applications (inspired by [bquenin/interpreter](https://github.com/bquenin/interpreter)).
It watches a chosen window, captures frames at a configurable rate, runs OCR on each
captured frame, translates any detected Japanese text to English, and draws the
translation on a floating overlay on top of (or below) the source window.

**Everything runs locally** — no cloud APIs, no network calls at runtime, no telemetry.
The ML models (OCR + Qwen3 translation) are **bundled and loaded from local
directories** — no Hugging Face at runtime. The only optional network access is a
one-time JMdict Read-Mode dictionary download (~11 MB, stdlib `urllib`), which is
also bundleable (`dict/jmdict.sqlite`).

## Pipeline (must stay a clean 4-stage pipeline)

```
Screen Capture -> OCR -> Translation -> Display
```

1. **Screen Capture** — grabs the target window's pixels at the configured refresh
   rate (e.g. 1–10 fps). This is a polling loop, **not** a video pipeline.
2. **OCR** — extracts Japanese text from the captured frame. Engines sit behind a
   common ABC so the stage stays swappable, but **RapidOCR is the only shipped
   engine** — horizontal text via the RapidOCR runtime + PP-OCRv5 ONNX models, all Apache-2.0
   and torch/paddle-free. (Two engines were removed: MangaOCR, a torch-based
   vertical-text engine; and MeikiOCR, whose model *weights* were LGPL-3.0 — both
   non-starters for the commercial Steam build. See [Open decisions](#open-decisions).)
3. **Translation** — Qwen3-4B (`Qwen/Qwen3-4B`, Apache-2.0 — commercial-safe)
   translates JA → EN locally via CTranslate2. Identical/near-identical
   source text should hit a cache instead of re-translating (**fuzzy** match, not just
   exact match). Before the model sees the text it is **name-protected** — katakana
   the analyzer tags as a proper noun is romanized so the model can't mistranslate it
   (`ヤツシロ` → `Yatsushiro`, not "you bitch"; loanwords like `コーヒー` are left alone),
   and a **furigana** term `漢字(かな)` is replaced by its romanized reading
   (`鬼戮(きりく)` → `Kiriku`) instead of the model echoing the untranslatable kanji, both
   with a user `names.json` override in the config dir. Each **sentence** is a
   translation-and-cache unit (`cache.SentenceCache`, splitting on `。！？` only —
   never the `、` clause comma, so a sentence's clauses still reach the model
   together). This is **not** the old MADLAD per-clause split (which dropped
   clauses); it exists so an accumulating **NVL** narration screen (text that grows
   one sentence per click) reuses the earlier sentences from the cache instead of
   re-generating the whole paragraph every frame — otherwise a long block costs
   ~100 s to regenerate *and* the block-level fuzzy match returns the shorter
   previous block, silently dropping the newest sentence. A single-sentence line
   (ordinary ADV dialogue) is one unit, unchanged. Lines with no Japanese left (an
   already-romanized name plate) skip the
   model entirely. **JMdict assist** (see `dictionary.py`): a single *word* (the Create-card
   Word field) is glossed from JMdict first — the model romanizes rare/compound words
   it doesn't know (`足コキ` → "Foot Koki" vs JMdict's "footjob"); only non-headwords
   fall back to the model. And when the model echoes an untranslated *kanji* word in a
   sentence (`刀剣` → "As a 刀剣 geek"), that word is replaced by its JMdict gloss
   ("sword") instead of being dropped. Both best-effort and non-blocking (skip if the
   index isn't built yet); kana-only residuals are still dropped (a hallucination's
   "entry" is a grammatical aux description).
4. **Display** — renders translated text on an overlay window using the user's
   configured mode, font, colors, and position.

Each stage must be **swappable independently**: OCR engines sit behind a common
interface, and the translation backend behind its own interface, even though we ship
one implementation of each right now. Keep this to a simple abstract base class per
stage — **do not** build a plugin system.

## Settings (all exposed in the GUI and persisted)

| # | Setting | Behavior |
|---|---------|----------|
| 1 | OCR engine | RapidOCR (only shipped engine; ABC keeps the stage swappable). Restart the pipeline **stage** on change, not the whole app. |
| 2 | Hide/Show overlay hotkey | **Global** hotkey — must work when the overlay/app isn't focused. |
| 3 | Font family / size | Applies to overlay text. |
| 4 | Text color | Color picker. |
| 5 | Text background color | Color picker, independent of text color. |
| 6 | Background opacity | 0–100%, slider. |
| 7 | Text position offset | X/Y offset in pixels. Default: cover the original text location. **This is the only positioning control — the app is in-place-only, no separate banner mode.** |
| 8 | Text mode | Single sub vs Duo sub. Single = one line, replace-in-place. Duo = two lines (previous+current, or original+translation stacked) — **exact semantics TBD, see [Open decisions](#open-decisions).** |
| 9 | Create-card capture hotkey | **Global** hotkey (like #2) — snapshots the current frame + its OCR/translation into the Create-card **capture stack** (max 20 entries; **persisted across restarts** via `capture_stack.py`, so a user who snaps a batch and closes the app before writing the cards doesn't lose them). The user flips through the stack (prev/next), deletes entries, and loads one into the card form to save. Works when the app isn't focused. |

- Persist settings across restarts in a user config file (JSON or TOML) in the
  platform-appropriate config directory via **`platformdirs`**.
- Apply changes **live** wherever feasible. Only fall back to "restart required" for
  things that genuinely can't be hot-swapped (e.g. swapping OCR model weights may
  justify a brief stage reload with a loading indicator). **Never** require relaunching
  the whole app for a setting change.

## Main window layout (the control panel, not the overlay)

A **three-panel** layout (deliberately different from the reference project's
single-column stack):

```
+-------------------+-------------------+
|      Capture      |      Setting      |
|  (preview +       | (every setting in |
|   start/refresh/  |  the table above) |
|   configure OCR)  |                   |
+-------------------+-------------------+
|                  Text                 |
|  (detected JA + translated EN text,   |
|   live, for the current frame)        |
+---------------------------------------+
```

- **Capture panel** (top-left): window/source picker; Start/Stop capture and Refresh
  **in the same control row** (not split apart); Configure OCR; live preview thumbnail
  of the captured region.
- **Setting panel** (top-right): every setting from the table above. Fold status
  indicators (OCR ready / translation ready) into a small status row here if there's
  room, otherwise a thin status bar at the bottom of the window.
- **Text panel** (bottom, full width): live raw OCR'd Japanese + English translation
  for the current frame, side by side or stacked. A debugging/visibility aid distinct
  from the overlay — lets the user confirm what's read/translated even when the overlay
  is hidden or the source window isn't visible.

Use a **responsive grid** (top row: two columns, capture ≳ setting in width; bottom
row: full-width). Implement with Qt layouts (`QGridLayout` / nested
`QHBoxLayout`/`QVBoxLayout`) — **not** fixed pixel positioning — so the window resizes
cleanly.

## Tech stack (target)

- **Language**: Python 3.14+
- **GUI & overlay**: **PySide6 (Qt)** — chosen over Tkinter for real per-pixel
  transparency, click-through windows, and multi-monitor geometry. Swapping this needs
  a strong reason and an update to this file.
- **Screen capture**: **`mss`** for cross-platform static frame capture. Fall back to
  platform-native capture only if `mss` proves insufficient for a specific OS (document
  why here if that happens).
- **OCR**:
  - PaddleOCR via **RapidOCR** (`rapidocr-onnxruntime`, Apache-2.0) — the only shipped
    engine. Runs PP-OCR **detection** (bundled in the rapidocr wheel) + **PP-OCRv5**
    Japanese **recognition** (`ocr/models/japan_ppocrv5_rec.onnx`, bundled; dict baked
    into the ONNX) on ONNX Runtime — **no PaddlePaddle and no torch at runtime**. Both
    code and weights are Apache-2.0 (commercial-clean). **Do not reintroduce any
    PyTorch-based OCR** (e.g. manga-ocr) or any model with copyleft weights (e.g.
    MeikiOCR's LGPL-3.0 weights): both are non-starters for the commercial Steam build.
- **Translation**: Qwen3-4B (`Qwen/Qwen3-4B`, Apache-2.0) via `ctranslate2`'s
  decoder-only `Generator` + `tokenizers` (byte-level BPE). **No torch at runtime**;
  torch/transformers are needed only *offline on the build machine* to produce the
  int8 CT2 export, and are never shipped. Two Qwen3 specifics: the prompt is ChatML
  built by hand (no `transformers`/`jinja2` at runtime), and it must suppress
  Qwen3's reasoning two ways — Qwen3 reasons out loud by default, which multiplies
  latency for no benefit here (one input, `いいよ`, generated ~360 reasoning tokens
  ≈ 66 s on CPU before a one-word answer, and the reasoning sometimes leaked into
  the output). The empty `<think></think>` block alone was NOT enough on some
  inputs, so the user turn also carries Qwen3's `/no_think` soft switch (appended
  after the text); that combination reliably drops the runaway to a normal call.
  The system prompt is passed as CT2's
  `static_prompt` so its KV cache is reused across lines. Decode greedily
  (`sampling_topk=1`) so the fuzzy cache's "same source → same translation"
  assumption holds. Rejected alternatives: **MADLAD-400-3B** (Apache-2.0 — replaced;
  a pure seq2seq MT model, it dropped clauses from multi-sentence lines, padded short
  ones, and lost register, which is why so much of `names.py`/`_recover_untranslated`
  exists), Sugoi V4 (license forbids commercial use), OPUS-MT (Apache-2.0 — too weak),
  FuguMT (CC-BY-SA), NLLB-200 / Aya / TowerInstruct (**CC-BY-NC — non-commercial**),
  and **Gemma / Llama** (their *use policies* restrict adult content and survive
  redistribution — a non-starter for this app's material; Apache-2.0 has no
  field-of-use terms). The model is **bundled and loaded from a local directory**
  (`models/qwen/`, override with `NIHONGOVIEWER_QWEN_DIR=<path>`) — **no
  `huggingface_hub` at runtime**. Trade-off: ~4 GB int8 on disk (CTranslate2 has no
  int4 on CPU — only `int8`, `int8_float32`, `float32`) and a slower per-line
  generation than MADLAD, largely hidden by the fuzzy cache.

Both ML models are **bundled** and loaded from local directories (OCR from
`ocr/models/`, Qwen3 from `models/qwen/`) — nothing is fetched from Hugging Face
at runtime. The large Qwen3 model (~4 GB) is **not** committed to git (it's copied
into the build/depot); the small Apache-2.0 OCR ONNX under `ocr/models/` **is**
committed. Keep model *loading* path-based, not `huggingface_hub`-based.

## ⚠️ Current state vs. target

The target architecture above is the **intended** design. The scaffold currently in the
repo does **not** match it yet:

| Aspect | Target (this doc) | Current scaffold |
|--------|-------------------|------------------|
| UI toolkit | PySide6 (Qt) | **pywebview + HTML/CSS/JS** (`ui/`) |
| Screen capture | `mss` (cross-platform) | **Windows Graphics Capture** (`windows-capture`) — per-window, no game disturbance; window enum/geometry still Win32 |
| Pipeline | 4 stages wired | **All 4 wired**: Capture + OCR (RapidOCR) + Qwen3-4B translation (fuzzy-cached) + in-place overlay |
| Layout | 3-panel Qt grid | Single HTML page |

The pywebview shell cannot provide the per-pixel-transparent, click-through overlay the
spec requires — that's the whole reason the target picks Qt. **Interim resolution:** the
overlay is implemented as a **native Win32 layered window** (`overlay.py`,
`UpdateLayeredWindow` + a premultiplied-alpha PIL bitmap), *not* on the pywebview shell —
so it does get real per-pixel alpha and `WS_EX_TRANSPARENT` click-through today, while
staying on the Win32 stack the capture code already uses. The **control-panel** toolkit
divergence (pywebview vs. PySide6) is still open — see [Open decisions](#open-decisions).
Don't assume the HTML control panel is the final architecture.

## Project layout (current)

All code lives in the `NihongoViewer/` subdirectory (the git root is one level up):

```
NihongoViewer/
  main.py                     # pywebview entry point; exposes Api to the UI
  capture.py                  # Win32 window enum/geometry + WGC per-window capture
  ocr/                        # OCR stage — engines behind a common ABC
    base.py                   #   OcrEngine ABC + OcrResult/OcrRegion (text + boxes)
    paddle.py                 #   RapidOCR engine (PP-OCRv5 ONNX, horizontal) — the only engine
    models/                   #   bundled PP-OCRv5 JA recognition ONNX (Apache-2.0)
    group.py                  #   group line-regions into positioned sentence-chunks
    __init__.py               #   create_engine() factory + registry
  translate/                  # Translation stage — backends behind a common ABC
    base.py                   #   Translator ABC (load / translate)
    qwen.py                   #   Qwen3-4B (the only backend; int8 CT2, torch-free)
    segment.py                #   has-Japanese test (pre-translate)
    names.py                  #   romanize proper-noun katakana (ヤツシロ->Yatsushiro)
    furigana.py               #   kana readings over kanji (天気->天気(てんき)); tokens() for Read Mode
    dictionary.py             #   offline JMdict index (Read Mode hover lookup; SQLite, CC BY-SA)
    cache.py                  #   FuzzyCache (near-identical reuse) + SentenceCache (per-sentence, for NVL)
    __init__.py               #   create_translator() factory (fuzzy-cached)
  overlay.py                  # Stage 4 — native Win32 per-pixel click-through overlay
  config.py                   # settings persistence (platformdirs JSON, load/save)
  capture_stack.py            # Create-card capture stack persistence (survives restarts)
  hotkey.py                   # global hide/show hotkey (Win32 RegisterHotKey thread)
  fonts/                      # bundled overlay fonts (OFL, JP+Latin): Noto Sans JP,
                              #   M PLUS Rounded 1c, Shippori Mincho (+ licenses)
  requirements.txt            # core: pywebview, pywin32, Pillow, numpy, platformdirs, rapidocr-onnxruntime, ctranslate2
  run.bat                     # launcher (uses .venv)
  ui/                         # index.html, style.css, app.js, cards.js, furigana.js
```

## Run it

From the `NihongoViewer/` directory:

```powershell
# First time only
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt

# Every time (or double-click run.bat)
.venv\Scripts\python.exe main.py
```

## Conventions

- Keep the 4 pipeline stages decoupled and each independently swappable behind its ABC.
- Prefer live-applying setting changes; treat "restart required" as a last resort.
- **No PyTorch at runtime.** The shipped toolchain is torch-free (PaddleOCR/RapidOCR is ONNX,
  Qwen3 is CT2); torch is allowed *offline on the build machine* only, to produce the CT2 export;
  keep it that way so the commercial Steam build stays clean of torch's licensing/size.
- No runtime network calls. Both ML models are bundled/local (no `huggingface_hub`);
  the only optional download is the JMdict dictionary (~11 MB, stdlib `urllib`).

## Open decisions

Resolve these early — **don't guess silently.** Propose in a design note, then record
the answer here.

1. **Toolkit divergence** — migrate the scaffold to PySide6, or revise the spec away
   from Qt? (See [Current state vs. target](#-current-state-vs-target).) *Partly resolved:*
   the **overlay** now uses a native Win32 layered window (`overlay.py`) instead of the
   pywebview shell, so it is no longer blocked on this. Still open for the **control
   panel** (currently HTML/pywebview). Cross-platform is also still open — capture and
   overlay are Windows-only today.
2. **Duo sub semantics** — *Resolved:* Duo shows the **original Japanese on top and the
   English translation stacked below** (per detected region), which fits a
   Japanese-learning use case. Single shows the English translation only. Implemented in
   `Api._overlay_text` (`main.py`). Note: the Japanese line needs a JP-capable overlay
   font (Meiryo / Noto Sans JP); Arial renders it as tofu.
3. **OCR engine choice / vertical text** — *Resolved:* Two engines were removed for
   the commercial Steam build — **MangaOCR** (PyTorch-based vertical-text engine; torch
   is a licensing/size non-starter) and **MeikiOCR** (Apache-2.0 code but **LGPL-3.0
   model weights** — copyleft the build must avoid). The shipped engine is now
   **PaddleOCR via RapidOCR** (horizontal; PP-OCR detection + PP-OCRv5 recognition,
   ONNX, **all Apache-2.0**, torch/paddle-free). Any future engine must stay both
   torch-free and free of copyleft weights. PP-OCRv5 does have some vertical-text
   capability, but the shipped detection path is horizontal; a dedicated vertical mode
   is still future work. OCR orientation auto-detection is moot for now (single engine).
```
