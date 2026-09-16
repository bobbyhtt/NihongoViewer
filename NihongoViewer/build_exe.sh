#!/usr/bin/env bash
# Steam / Option A build for Yakutori (commercial-safe: PaddleOCR/RapidOCR + Qwen3).
# Produces a FOLDER build at build/main.dist/ (NOT onefile — Steam ships folders,
# and onefile would re-extract the bundled models to temp on every launch).
# Model weights are shipped locally (no Hugging Face at runtime): the OCR ONNX is
# bundled via --include-data-dir below, and the Qwen3-4B CTranslate2 model is copied
# into build/main.dist/models/qwen/ as a post-build step (it's ~4 GB, so it's
# copied into the dist rather than run through Nuitka). The app then runs fully
# offline with no first-run download.
# NOTE: ctranslate2's wheel bundles cudnn64_9.dll (NVIDIA, proprietary) for the GPU
# path. We are CPU-only, so delete it from build/main.dist/ctranslate2/ — it drops
# an NVIDIA redistribution obligation we don't need.
# NOTE: unidic_lite needs its dicdir payload or fugashi.Tagger() fails behind a guarded
# import, silently disabling name protection + furigana/Read Mode. --include-package-data
# gets the 17 text/def files BUT Nuitka refuses to copy the two .bin files MeCab requires
# (char.bin, matrix.bin) through ANY data mechanism (--include-package-data AND
# --include-data-dir both drop them — it treats .bin as a binary, not data). So those two
# files MUST be copied into build/main.dist/unidic_lite/dicdir/ as a POST-BUILD step,
# alongside the LICENSE/THIRD_PARTY and models/qwen copies:
#   cp .venv/Lib/site-packages/unidic_lite/dicdir/{char.bin,matrix.bin} \
#      build/main.dist/unidic_lite/dicdir/
# Without them Read Mode silently degrades to plain text. Verify: the dist dicdir has *.bin.
cd "C:/Users/bobby/Documents/NihongoViewer/NihongoViewer" || exit 1
# CPU portability — MUST build with MSVC, NOT zig. The build machine is AMD Zen4
# (has AVX-512). The bundled zig compiler builds its OWN runtime memcpy for the
# host CPU and bakes an AVX-512 `vmovups zmm` into Yakutori.exe's .text, which
# crashes with STATUS_ILLEGAL_INSTRUCTION (0xc000001d) on any CPU without AVX-512
# (every 12th-gen+ Intel), at launch. This is INVISIBLE in-house (the build box
# has AVX-512). None of zig's knobs strip it (verified with a tight EVEX-opcode
# byte scan, not the unreliable .pdata/linear sweeps): -mcpu is ignored,
# -march=x86-64-v3 errors, -target x86_64-windows-gnu + --lto=no still leaves ~104
# zmm memcpy ops, and --mingw64 is rejected on Python 3.13+. MSVC is the fix: it
# defaults to an SSE2 baseline (no AVX-512 without /arch) and its memcpy is resolved
# from the system UCRT/vcruntime DLLs (runtime-dispatched), never baked into the exe.
# Requires Visual Studio Build Tools with the "Desktop development with C++" workload.
# ALWAYS re-verify: tight EVEX-512 vmov-opcode scan of the dist exe .text must be 0.
.venv/Scripts/python.exe -m nuitka \
  --standalone \
  --msvc=latest \
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
  --include-package=tokenizers \
  --include-package-data=tokenizers \
  --include-package=fugashi \
  --include-package=unidic_lite \
  --include-package-data=unidic_lite \
  --include-package=jaconv \
  --output-dir=build \
  --output-filename=Yakutori.exe \
  main.py
echo "EXIT_CODE=$?"
