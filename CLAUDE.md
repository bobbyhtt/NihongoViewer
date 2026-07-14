# CLAUDE.md

Guidance for Claude Code (and human contributors) working in this repository.

## What we're building

**NihongoViewer** is an offline, real-time screen translator for Japanese games and
applications (inspired by [bquenin/interpreter](https://github.com/bquenin/interpreter)).
It watches a chosen window, captures frames at a configurable rate, runs OCR on each
captured frame, translates any detected Japanese text to English, and draws the
translation on a floating overlay on top of (or below) the source window.

**Everything runs locally** — no cloud APIs, no network calls at runtime, no telemetry.
The only network access is a one-time model-weights download on first run.

## Pipeline (must stay a clean 4-stage pipeline)

```
Screen Capture -> OCR -> Translation -> Display
```

1. **Screen Capture** — grabs the target window's pixels at the configured refresh
   rate (e.g. 1–10 fps). This is a polling loop, **not** a video pipeline.
2. **OCR** — extracts Japanese text from the captured frame. Two selectable engines:
   - **MeikiOCR** — horizontal text, trained on pixel/retro-game fonts.
   - **MangaOCR** — vertical Japanese text (visual novels, manga-style UI, older JRPGs).

   The active engine is a **user setting**, not auto-detected. (Orientation
   auto-detection is a stretch goal — see [Open decisions](#open-decisions); do not
   build it speculatively.)
3. **Translation** — MADLAD-400-3B (`google/madlad400-3b-mt`, Apache-2.0 —
   commercial-safe) translates JA → EN locally via CTranslate2. Identical/near-identical
   source text should hit a cache instead of re-translating (**fuzzy** match, not just
   exact match). Before the model sees the text it is (a) **name-protected** — katakana
   the analyzer tags as a proper noun is romanized so the model can't mistranslate it
   (`ヤツシロ` → `Yatsushiro`, not "you bitch"; loanwords like `コーヒー` are left alone),
   and a **furigana** term `漢字(かな)` is replaced by its romanized reading
   (`鬼戮(きりく)` → `Kiriku`) instead of the model echoing the untranslatable kanji, both
   with a user `names.json` override in the config dir — and (b) **sentence-split**, so
   a long multi-sentence line doesn't make the model drop a clause. Segments with no
   Japanese left (an already-romanized name) skip the model to avoid hallucinated
   padding.
4. **Display** — renders translated text on an overlay window using the user's
   configured mode, font, colors, and position.

Each stage must be **swappable independently**: OCR engines sit behind a common
interface, and the translation backend behind its own interface, even though we ship
one implementation of each right now. Keep this to a simple abstract base class per
stage — **do not** build a plugin system.

## Settings (all exposed in the GUI and persisted)

| # | Setting | Behavior |
|---|---------|----------|
| 1 | OCR engine | MeikiOCR vs MangaOCR. Restart the pipeline **stage** on change, not the whole app. |
| 2 | Hide/Show overlay hotkey | **Global** hotkey — must work when the overlay/app isn't focused. |
| 3 | Font family / size | Applies to overlay text. |
| 4 | Text color | Color picker. |
| 5 | Text background color | Color picker, independent of text color. |
| 6 | Background opacity | 0–100%, slider. |
| 7 | Text position offset | X/Y offset in pixels. Default: cover the original text location. **This is the only positioning control — the app is in-place-only, no separate banner mode.** |
| 8 | Text mode | Single sub vs Duo sub. Single = one line, replace-in-place. Duo = two lines (previous+current, or original+translation stacked) — **exact semantics TBD, see [Open decisions](#open-decisions).** |

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
  - MeikiOCR — https://github.com/rtr46/meikiocr
  - manga-ocr — PyTorch-based; pull in as an **optional extra** (heavy `torch` dep).
    **Guard the import** so MeikiOCR-only users aren't forced to install torch.
- **Translation**: MADLAD-400-3B (`google/madlad400-3b-mt`, Apache-2.0) via
  `ctranslate2` + `sentencepiece`, using the ready-made int8 CT2 export
  `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (~1.65 GB) — **no torch, ever** (no
  conversion step). It's a T5: single shared `spiece.model` vocab and a `<2en>`
  target-language prefix token. MADLAD pads/repeats short lines, so decode with a
  repetition penalty + no-repeat-ngram. Rejected predecessors (all removed): Sugoi V4
  (license forbids commercial use), OPUS-MT (`Helsinki-NLP/opus-mt-ja-en`, Apache-2.0
  — too weak), FuguMT (`staka/fugumt-ja-en`, CC-BY-SA — better than OPUS-MT but still
  not good enough, and needed a torch conversion step). Override the CT2 repo via
  `NIHONGOVIEWER_MADLAD_CT2_REPO=<hf-repo-id>`. Trade-off: 3B is heavier — ~0.5-2 s per
  *new* line on CPU and ~2 GB RAM, largely hidden by the fuzzy cache.

Model weights download on first run and cache under `~/.cache/huggingface/` (standard
HF cache). **Do not** bundle weights in the repo or installer.

## ⚠️ Current state vs. target

The target architecture above is the **intended** design. The scaffold currently in the
repo does **not** match it yet:

| Aspect | Target (this doc) | Current scaffold |
|--------|-------------------|------------------|
| UI toolkit | PySide6 (Qt) | **pywebview + HTML/CSS/JS** (`ui/`) |
| Screen capture | `mss` (cross-platform) | **Windows Graphics Capture** (`windows-capture`) — per-window, no game disturbance; window enum/geometry still Win32 |
| Pipeline | 4 stages wired | **All 4 wired**: Capture + OCR (MeikiOCR/MangaOCR) + MADLAD-400 translation (fuzzy-cached) + in-place overlay |
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
    meiki.py                  #   MeikiOCR (ONNX, horizontal) — default
    manga.py                  #   MangaOCR (torch, vertical) — guarded import
    group.py                  #   group line-regions into positioned sentence-chunks
    __init__.py               #   create_engine() factory + registry
  translate/                  # Translation stage — backends behind a common ABC
    base.py                   #   Translator ABC (load / translate)
    madlad.py                 #   MADLAD-400-3B (the only backend; int8 CT2, torch-free)
    segment.py                #   sentence split + has-Japanese test (pre-translate)
    names.py                  #   romanize proper-noun katakana (ヤツシロ->Yatsushiro)
    furigana.py               #   kana readings over kanji (天気->天気(てんき)) for cards
    cache.py                  #   FuzzyCache — near-identical source reuse
    __init__.py               #   create_translator() factory (fuzzy-cached)
  overlay.py                  # Stage 4 — native Win32 per-pixel click-through overlay
  config.py                   # settings persistence (platformdirs JSON, load/save)
  hotkey.py                   # global hide/show hotkey (Win32 RegisterHotKey thread)
  fonts/                      # bundled overlay fonts (OFL, JP+Latin): Noto Sans JP,
                              #   M PLUS Rounded 1c, Shippori Mincho (+ licenses)
  requirements.txt            # core: pywebview, pywin32, Pillow, numpy, platformdirs, meikiocr, ctranslate2
  requirements-mangaocr.txt   # optional heavy extra: manga-ocr (+ torch)
  run.bat                     # launcher (uses .venv)
  ui/                         # index.html, style.css, app.js, cards.js, furigana.js
```

## Run it

From the `NihongoViewer/` directory:

```powershell
# First time only
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
# Optional: add the MangaOCR engine (heavy — pulls in torch)
.venv\Scripts\python.exe -m pip install -r requirements-mangaocr.txt

# Every time (or double-click run.bat)
.venv\Scripts\python.exe main.py
```

## Conventions

- Keep the 4 pipeline stages decoupled and each independently swappable behind its ABC.
- Prefer live-applying setting changes; treat "restart required" as a last resort.
- Guard heavy/optional imports (e.g. `torch` for manga-ocr) so they don't break the
  MeikiOCR-only path.
- No runtime network calls beyond the first-run model download.

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
3. **OCR orientation auto-detection** (stretch goal) — only if it proves reliable;
   until then the OCR engine stays a manual user setting.
```
