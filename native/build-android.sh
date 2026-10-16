#!/usr/bin/env bash
# Cross-compila llama.cpp (llama-server + ggml-rpc-server) per Android arm64
# in tre varianti ARM e copia i binari in app/src/main/jniLibs come lib*.so,
# così Android li estrae in nativeLibraryDir (unica cartella eseguibile per un'app).
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SRC="$HERE/llama.cpp"
OUT="$HERE/../app/src/main/jniLibs/arm64-v8a"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
# NDK: ANDROID_NDK, poi quello dell'SDK, poi quello della toolchain DistAI su Windows (~/.distai-toolchain)
NDK="${ANDROID_NDK:-$( (ls -d "$SDK"/ndk/* "$HOME"/.distai-toolchain/android-ndk-* 2>/dev/null || true) | sort -V | tail -1)}"
PRE="$(ls -d "$NDK"/toolchains/llvm/prebuilt/* | head -1)"   # linux-x86_64, windows-x86_64, darwin-x86_64
# su Windows (Git Bash) cmake vuole percorsi C:/...; cmake e ninja dalla toolchain DistAI se non sono nel PATH
winpath() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else echo "$1"; fi; }
for d in "$HOME"/.distai-toolchain "$HOME"/.distai-toolchain/cmake-*/bin; do [ -d "$d" ] && PATH="$PATH:$d"; done
GEN=(); command -v ninja >/dev/null && GEN=(-G Ninja)
API=24

# nome variante -> flag -march
declare -A VARIANTS=(
  [v8]="armv8-a"
  [v82]="armv8.2-a+dotprod+fp16"
  [v86]="armv8.6-a+dotprod+fp16+i8mm"
)

mkdir -p "$OUT"
# patch DistAI su llama.cpp (applicate una volta sola)
for p in "$HERE"/patches/*.patch; do
  git -C "$SRC" apply --reverse --check "$p" 2>/dev/null || git -C "$SRC" apply "$p"
done
# percorso senza spazi: CMAKE_EXE_LINKER_FLAGS non gestisce le virgolette
UNBUF="$HOME/.cache/distai-build/unbuffer.o"; mkdir -p "$(dirname "$UNBUF")"
"$PRE/bin/clang" --target=aarch64-linux-android$API -O2 -c "$HERE/unbuffer.c" -o "$UNBUF"
for v in "${!VARIANTS[@]}"; do
  arch="${VARIANTS[$v]}"
  bdir="$HERE/build-$v"
  echo "=== variante $v ($arch) ==="
  cmake -S "$SRC" -B "$bdir" "${GEN[@]}" \
    -DCMAKE_TOOLCHAIN_FILE="$(winpath "$NDK")/build/cmake/android.toolchain.cmake" \
    -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-$API -DANDROID_STL=c++_static \
    -DCMAKE_BUILD_TYPE=Release \
    -DBUILD_SHARED_LIBS=OFF \
    -DCMAKE_EXE_LINKER_FLAGS="$(winpath "$UNBUF")" \
    -DGGML_RPC=ON -DGGML_OPENMP=OFF -DGGML_NATIVE=OFF -DGGML_LLAMAFILE=ON \
    -DGGML_CPU_ARM_ARCH="$arch" \
    -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF \
    -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_SUBPROCESS=OFF \
    > "$bdir.cmake.log" 2>&1 || { tail -40 "$bdir.cmake.log"; exit 1; }
  cmake --build "$bdir" --target llama-server ggml-rpc-server -j"$(nproc)" > "$bdir.build.log" 2>&1 \
    || { grep -m20 -B2 -A8 "error" "$bdir.build.log"; exit 1; }
  "$PRE/bin/llvm-strip" -o "$OUT/libllamaserver_$v.so" "$bdir/bin/llama-server"
  "$PRE/bin/llvm-strip" -o "$OUT/librpcserver_$v.so"   "$bdir/bin/ggml-rpc-server"
done
ls -lh "$OUT"
