# Third-Party Licenses and Attributions

NihongoViewer includes, links against, and/or downloads at runtime a number of
third-party components. Each is the property of its respective author(s) and is
licensed under its own terms, reproduced or referenced below. This file is
provided to satisfy the attribution and notice requirements of those licenses
(e.g. Apache-2.0 §4, the MIT/BSD reproduction requirement, and the SIL Open Font
License bundling requirement).

Nothing in the NihongoViewer EULA (see `LICENSE`) limits any right granted to you
under a component's own license.

> **Legend — how each component reaches the user**
> **Bundled**: shipped inside the NihongoViewer distribution.
> **Optional**: shipped only if the MangaOCR extra is installed.
> **Runtime download**: not shipped by us; downloaded from a third party on first
> run into the user's local cache (`~/.cache/huggingface/`).

---

## 1. Bundled Python dependencies (code)

| Component | Version (min) | License | Notes |
|---|---|---|---|
| pywebview | ≥5.0 | BSD 3-Clause | Native window / UI shell |
| pywin32 | ≥306 | PSF License (BSD-style) | Win32 API access |
| Pillow | ≥10.0 | MIT-CMU / HPND | Imaging |
| numpy | ≥1.24 | BSD 3-Clause | Arrays |
| platformdirs | ≥4.0 | MIT | Config-dir resolution |
| windows-capture | ≥2.0 | MIT | Windows Graphics Capture binding |
| meikiocr (code) | ≥0.3.4 | Apache-2.0 | OCR engine code — **see model note in §4** |
| ctranslate2 | ≥4.0 | MIT | Translation inference runtime |
| sentencepiece | ≥0.2.0 | Apache-2.0 | Tokenizer |
| fugashi | ≥1.3 | MIT | MeCab wrapper (name protection) |
| unidic-lite | ≥1.0.8 | MIT / WTFPL | Wrapper; **bundles UniDic 2.1.2 dictionary data — BSD License** (© UniDic Consortium) |
| jaconv | ≥0.3 | MIT | kana ↔ romaji |

> **Anki `.apkg` export** is built entirely with the Python **standard library**
> (`sqlite3` + `zipfile` + `json`) — see `anki_export.py`.

## 2. Optional dependencies (MangaOCR extra only)

| Component | Version (min) | License | Notes |
|---|---|---|---|
| manga-ocr (code) | ≥0.1.14 | Apache-2.0 | Vertical-text OCR engine code — **see model note in §4** |
| transformers | <5 | Apache-2.0 | MangaOCR tokenizer/model runtime |
| torch (PyTorch) | (transitive) | BSD-style (PyTorch license) | Pulled in by manga-ocr |

## 3. Bundled fonts — SIL Open Font License 1.1

All three fonts are licensed under the **SIL Open Font License, Version 1.1**
(`OFL-1.1`). Their full license texts ship alongside the fonts in
`NihongoViewer/fonts/`.

| Font | Source | License file |
|---|---|---|
| Noto Sans JP | google/fonts | `fonts/NotoSansJP-OFL.txt` |
| M PLUS Rounded 1c | google/fonts | (OFL-1.1) |
| Shippori Mincho | google/fonts | `fonts/ShipporiMincho-OFL.txt` |

**OFL compliance notes (important for a commercial release):**
- OFL fonts **may** be bundled with, and sold as part of, application software.
- The OFL license text **must** ship with the fonts (it does).
- You may **not** sell the fonts on their own, and you may not use the fonts'
  Reserved Font Names on any modified version.

## 4. Machine-learning model weights (downloaded at runtime — NOT bundled)

These weights are **not distributed by NihongoViewer**. They are downloaded from
the publishers below on first run and are governed solely by the publisher's stated
license.

| Model | Publisher / repo | Stated license | Used by |
|---|---|---|---|
| MADLAD-400-3B MT (int8 CT2 export) | `Nextcloud-AI/madlad400-3b-mt-ct2-int8` (from Google `google/madlad400-3b-mt`) | **Apache-2.0** | Translation |
| MangaOCR base model | `kha-white/manga-ocr-base` | **Apache-2.0** | OCR (vertical, optional) |
| MeikiOCR text-recognition | `rtr46/meiki.txt.recognition.v0` | **LGPL-3.0** ⚠️ | OCR (default) |
| MeikiOCR text-detection | `rtr46/meiki.text.detect.v0` | **LGPL-3.0** ⚠️ | OCR (default) |

### ⚠️ LGPL-3.0 notice — MeikiOCR model weights

The MeikiOCR **model weights** (used by the default OCR engine) are published under
the **GNU Lesser General Public License v3.0**, even though the MeikiOCR **code** is
Apache-2.0. Key consequences for a commercial product:

- LGPL-3.0 **permits** commercial use and distribution.
- If you ever **redistribute** these weights (e.g. bundle them in the Steam build or
  a patch), LGPL obligations attach: you must supply the LGPL text, give notice that
  the component is used and LGPL-covered, and ensure the end user can **replace**
  that component with a modified version and run the result. You must not add terms
  that forbid reverse-engineering for that purpose.
- NihongoViewer's current design **downloads these weights at runtime rather than
  bundling them**, so the LGPL work is conveyed to the user by Hugging Face, not by
  you — which substantially reduces (but does not automatically eliminate) your
  obligations. Keeping the models as a runtime download, and keeping the OCR engine
  swappable so a user can substitute their own model, is the safest posture.
- **This is a genuine legal question for a paid release. Have qualified counsel
  confirm your specific distribution model before shipping on Steam.**

## 5. Forward-looking note — PySide6 / Qt

The target architecture in `CLAUDE.md` calls for migrating the control panel to
**PySide6 (Qt)**. The current build does **not** use it (the UI runs on pywebview,
BSD-3). PySide6/Qt is offered under **LGPL-3.0** (or a paid commercial Qt license).
If you migrate, the same LGPL considerations above apply to Qt, and you should
evaluate whether the LGPL dynamic-linking/relinking conditions or a commercial Qt
license best fit a closed-source Steam product.

---

## Full license texts

The complete texts of the licenses referenced above (Apache-2.0, MIT, BSD-3-Clause,
LGPL-3.0, PSF, and SIL OFL-1.1) are available from each project's repository. The
OFL-1.1 texts for the bundled fonts are included in `NihongoViewer/fonts/`. Before
distributing on Steam, verify that the exact version of each dependency you ship
matches the license stated here (licenses can change between releases).

_Last reviewed: 2026-07-15._
