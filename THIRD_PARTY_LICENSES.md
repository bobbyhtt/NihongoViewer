# Third-Party Licenses and Attributions

Yakutori includes and/or links against the third-party components listed below.
Each is the property of its respective author(s) and is licensed under its own
terms. This file is provided to satisfy the attribution and notice requirements of
those licenses.

Nothing in the Yakutori EULA (see `LICENSE`) limits any right granted to you under
a component's own license.

---

## 1. Bundled Python dependencies (code)

| Component | Version (min) | License | Role |
|---|---|---|---|
| pywebview | ≥5.0 | BSD 3-Clause | Native window / UI shell |
| pywin32 | ≥306 | PSF License (BSD-style) | Win32 API access |
| Pillow | ≥10.0 | MIT-CMU / HPND | Imaging |
| numpy | ≥1.24 | BSD 3-Clause | Arrays |
| platformdirs | ≥4.0 | MIT | Config-dir resolution |
| windows-capture | ≥2.0 | MIT | Windows Graphics Capture binding |
| rapidocr-onnxruntime | ≥1.2.3 | Apache-2.0 | OCR engine (PP-OCR detection + recognition via ONNX) |
| onnxruntime | ≥1.17 | MIT | ONNX inference runtime (OCR) |
| opencv-python-headless | ≥4.5 | Apache-2.0 | Image operations for OCR |
| pyclipper | ≥1.3 | MIT (Clipper: Boost Software License 1.0) | OCR detection post-processing |
| shapely | ≥2.0 | BSD 3-Clause | OCR detection post-processing |
| PyYAML | ≥6.0 | MIT | rapidocr configuration parsing |
| six | ≥1.16 | MIT | rapidocr compatibility shim |
| ctranslate2 | ≥4.0 | MIT | Translation inference runtime |
| ↳ oneDNN (statically linked into `ctranslate2.dll`) | bundled | Apache-2.0 | CPU inference kernels |
| ↳ Intel oneAPI MKL (statically linked into `ctranslate2.dll`) | bundled | Intel Simplified Software License — binary redistribution permitted royalty-free | BLAS kernels |
| ↳ Intel OpenMP runtime (`ctranslate2/libiomp5md.dll`) | bundled | Intel Simplified Software License — binary redistribution permitted royalty-free | Thread pool |
| tokenizers | ≥0.20 | Apache-2.0 | Byte-level BPE tokenizer (Qwen3) |
| fugashi | ≥1.3 | MIT | MeCab wrapper |
| unidic-lite | ≥1.0.8 | MIT / WTFPL; bundles UniDic 2.1.2 data (© UniDic Consortium), released under the GPL, the LGPL, or the BSD License at the licensee's option — Yakutori elects the **BSD License** | Japanese morphological dictionary |
| jaconv | ≥0.3 | MIT | Kana ↔ romaji conversion |
| bottle | (transitive) | MIT | HTTP server used by the pywebview shell |
| Jinja2 | (transitive) | BSD 3-Clause | Templating (pywebview) |
| MarkupSafe | (transitive) | BSD 3-Clause | String escaping (Jinja2) |
| proxy_tools | (transitive) | MIT | Lazy proxies (pywebview) |
| pythonnet | (transitive) | MIT | .NET bridge for the WebView2 shell |
| clr_loader | (transitive) | MIT | .NET runtime loader (pythonnet) |
| cffi | (transitive) | MIT | C foreign-function interface (clr_loader) |
| protobuf | (transitive) | BSD 3-Clause | Serialization (onnxruntime) |
| typing_extensions | (transitive) | PSF License | Typing backports |

## 2. Bundled fonts

All bundled fonts are licensed under the **SIL Open Font License, Version 1.1**
(`OFL-1.1`). Their full license texts ship alongside the fonts in `fonts/`.

| Font | Source | License file |
|---|---|---|
| Noto Sans JP | google/fonts | `fonts/NotoSansJP-OFL.txt` |
| M PLUS Rounded 1c | google/fonts | OFL-1.1 |
| Shippori Mincho | google/fonts | `fonts/ShipporiMincho-OFL.txt` |

## 3. Machine-learning models (bundled)

| Model | Origin | License | Role |
|---|---|---|---|
| Qwen3-4B (int8 CTranslate2 export) | `Qwen/Qwen3-4B` (Alibaba Cloud), converted to CTranslate2 | Apache-2.0 | Translation |
| PP-OCRv5 mobile recognition (`ocr/models/japan_ppocrv5_rec.onnx`) | PaddleOCR `PP-OCRv5_mobile_rec`, converted to ONNX (`ilaylow/PP_OCRv5_mobile_onnx`); character dictionary from PaddleOCR `ppocrv5_dict.txt` | Apache-2.0 | OCR recognition |
| PP-OCR text-detection and angle-classification | Included in the `rapidocr-onnxruntime` package | Apache-2.0 | OCR detection |

## 4. Dictionary data

| Data | Source | License |
|---|---|---|
| JMdict / JMnedict | Electronic Dictionary Research and Development Group (EDRDG), via `scriptin/jmdict-simplified` | CC BY-SA 4.0 |

JMdict/JMnedict is © the Electronic Dictionary Research and Development Group and
is used under the Creative Commons Attribution-ShareAlike 4.0 International
licence. The SQLite index built or shipped by Yakutori is a reformatted derivative
of that data and remains licensed under CC BY-SA 4.0. Attribution is also shown at
the point of use in Read Mode. Licence text:
https://creativecommons.org/licenses/by-sa/4.0/ — see also https://www.edrdg.org/
and https://github.com/scriptin/jmdict-simplified.

## 5. Full license texts

The complete texts of the licenses referenced above (Apache-2.0, MIT, BSD-3-Clause,
CC BY-SA 4.0, PSF, Boost Software License 1.0, WTFPL, and SIL OFL-1.1) are
available from each project's repository. The OFL-1.1 texts for the bundled fonts
are included in `fonts/`.
