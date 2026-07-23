#!/usr/bin/env bash
# Steam / Option A build for Yakutori (commercial-safe: PaddleOCR/RapidOCR + MADLAD).
# Produces a FOLDER build at build/main.dist/ (NOT onefile — Steam ships folders,
# and onefile would re-extract the bundled models to temp on every launch).
# Model weights are shipped locally (no Hugging Face at runtime): the OCR ONNX is
# bundled via --include-data-dir below, and the MADLAD CTranslate2 model is copied
# into build/main.dist/models/madlad/ as a post-build step (it's ~3 GB, so it's
# copied into the dist rather than run through Nuitka). The app then runs fully
# offline with no first-run download.
# NOTE: unidic_lite needs --include-package-data (not just --include-package) — the
# module alone is useless without its dicdir payload, and fugashi.Tagger() then fails
# behind a guarded import, silently disabling name protection + furigana/Read Mode.
cd "C:/Users/bobby/Documents/NihongoViewer/NihongoViewer" || exit 1
.venv/Scripts/python.exe -m nuitka \
  --standalone \
  --assume-yes-for-downloads \
  --windows-console-mode=disable \
  --nofollow-import-to=torch \
  --nofollow-import-to=transformers \
  --disable-plugin=pywebview \
  --include-module=clr \
  --include-package=pythonnet \
  --include-package-data=pythonnet \
  --include-package=clr_loader \
  --include-package-data=clr_loader \
  --include-package=webview \
  --include-package-data=webview \
  --windows-icon-from-ico=icon.ico \
  --include-data-files=icon.ico=icon.ico \
  --include-data-dir=ui=ui \
  --include-data-dir=fonts=fonts \
  --include-data-dir=dict=dict \
  --include-package=ocr \
  --include-data-dir=ocr/models=ocr/models \
  --include-package=translate \
  --include-package=ctranslate2 \
  --include-package=rapidocr_onnxruntime \
  --include-package-data=rapidocr_onnxruntime \
  --include-package=sentencepiece \
  --include-package=fugashi \
  --include-package=unidic_lite \
  --include-package-data=unidic_lite \
  --include-package=jaconv \
  --output-dir=build \
  --output-filename=Yakutori.exe \
  main.py
echo "EXIT_CODE=$?"
