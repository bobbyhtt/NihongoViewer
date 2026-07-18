#!/usr/bin/env bash
# Steam / Option A build for NihongoViewer (commercial-safe, MeikiOCR + MADLAD).
# Produces a FOLDER build at build/main.dist/ (NOT onefile — Steam ships folders,
# and onefile would re-extract the bundled models to temp on every launch).
# Model weights are shipped in build/main.dist/models/ (see copy_models step),
# so the app runs fully offline with no first-run download.
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
  --include-data-dir=ui=ui \
  --include-data-dir=fonts=fonts \
  --include-package=ocr \
  --include-package=translate \
  --include-package=ctranslate2 \
  --include-package=meikiocr \
  --include-package-data=meikiocr \
  --include-package=sentencepiece \
  --include-package=huggingface_hub \
  --output-dir=build \
  --output-filename=NihongoViewer.exe \
  main.py
echo "EXIT_CODE=$?"
