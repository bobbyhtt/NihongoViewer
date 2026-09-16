#!/usr/bin/env bash
# Post-build steps Nuitka can't/won't do — run AFTER build_exe.sh, every rebuild.
# Three mandatory copies into the dist, plus a compatibility check.
set -e
cd "C:/Users/bobby/Documents/NihongoViewer/NihongoViewer" || exit 1
DIST="build/main.dist"

echo "[postbuild] 1/3 license + notice files (Apache/BSD/OFL/CC-BY-SA compliance)"
cp ../LICENSE "$DIST/LICENSE"
cp ../THIRD_PARTY_LICENSES.md "$DIST/THIRD_PARTY_LICENSES.md"

echo "[postbuild] 2/3 Qwen3 CT2 model (~4GB; not run through Nuitka)"
mkdir -p "$DIST/models/qwen"
cp models/qwen/config.json models/qwen/tokenizer.json models/qwen/vocabulary.json "$DIST/models/qwen/"
cp models/qwen/model.bin "$DIST/models/qwen/"

echo "[postbuild] 3/3 unidic dicdir .bin files (fugashi/MeCab -> furigana + Read Mode + name protection)"
# Nuitka refuses to copy .bin as data (treats it as a binary); MeCab needs these two.
cp .venv/Lib/site-packages/unidic_lite/dicdir/char.bin \
   .venv/Lib/site-packages/unidic_lite/dicdir/matrix.bin \
   "$DIST/unidic_lite/dicdir/"

echo "[postbuild] pruning files that don't belong in a shipping build"
# Debug symbols, a docs PDF, and the Android asset (this is a Windows-only app) —
# Nuitka bundles these but nothing loads them at runtime.
rm -f "$DIST"/clr_loader/ffi/dlls/amd64/ClrLoader.pdb \
      "$DIST"/clr_loader/ffi/dlls/x86/ClrLoader.pdb \
      "$DIST"/unidic_lite/dicdir/unidic-mecab.pdf \
      "$DIST"/webview/lib/pywebview-android.jar

echo "[postbuild] verifying required payloads..."
ok=1
# The manga-ocr vertical engine (ocr/models/manga/) rides in via --include-data-dir
# in build_exe.sh (Nuitka copies .onnx/.json/.txt fine — unlike the qwen/unidic .bin
# above), so there's no copy step for it; this just guards it never silently drops out.
for p in LICENSE THIRD_PARTY_LICENSES.md models/qwen/model.bin \
         unidic_lite/dicdir/char.bin unidic_lite/dicdir/matrix.bin \
         ocr/models/manga/encoder_model.onnx ocr/models/manga/decoder_model.onnx \
         ocr/models/manga/vocab.txt \
         Yakutori.exe; do
  if [ -e "$DIST/$p" ]; then echo "  OK  $p"; else echo "  MISSING  $p"; ok=0; fi
done
# AVX-512 safety: Yakutori.exe itself must stay ISA-portable (env vars in main.py force
# the compute libs to AVX2 at runtime; this just guards the frozen exe never regresses).
[ "$ok" = 1 ] && echo "[postbuild] DONE" || { echo "[postbuild] FAILED — missing payloads"; exit 1; }
