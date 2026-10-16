#!/usr/bin/env bash
# Cross-compila llama-diffusion-cli (dLLM: Dream/LLaDA/a2d) per Android arm64, varianti v8 / v82 / v86.
# Uso: build-diffusion-android.sh [v8 v82 v86]   -> binari in native/dist-diffusion/diffusion-cli-<var>
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; SRC="$HERE/llama.cpp"; OUT="$HERE/dist-diffusion"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"; NDK="${ANDROID_NDK:-$(ls -d "$SDK"/ndk/* | sort -V | tail -1)}"
PRE="$(ls -d "$NDK"/toolchains/llvm/prebuilt/* | head -1)"
declare -A VARIANTS=([v8]="armv8-a" [v82]="armv8.2-a+dotprod+fp16" [v86]="armv8.6-a+dotprod+fp16+i8mm")
mkdir -p "$OUT"
for v in "${@:-v82 v8 v86}"; do
  for vv in $v; do
    bdir="$HERE/build-dif-$vv"
    cmake -S "$SRC" -B "$bdir" -DCMAKE_TOOLCHAIN_FILE="$NDK/build/cmake/android.toolchain.cmake" \
      -DANDROID_ABI=arm64-v8a -DANDROID_PLATFORM=android-24 -DANDROID_STL=c++_static -DCMAKE_BUILD_TYPE=Release \
      -DBUILD_SHARED_LIBS=OFF -DGGML_OPENMP=OFF -DGGML_NATIVE=OFF -DGGML_LLAMAFILE=ON \
      -DGGML_CPU_ARM_ARCH="${VARIANTS[$vv]}" -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_SERVER=OFF \
      -DLLAMA_BUILD_EXAMPLES=ON -DLLAMA_BUILD_TOOLS=OFF -DLLAMA_BUILD_UI=OFF -DLLAMA_USE_PREBUILT_UI=OFF > "$bdir.cmake.log" 2>&1 \
      || { tail -30 "$bdir.cmake.log"; exit 1; }
    cmake --build "$bdir" --target llama-diffusion-cli llama-bd3-cli -j4 > "$bdir.build.log" 2>&1 || { grep -m10 -A6 "error" "$bdir.build.log"; exit 1; }
    "$PRE/bin/llvm-strip" -o "$OUT/diffusion-cli-$vv" "$bdir/bin/llama-diffusion-cli"
    "$PRE/bin/llvm-strip" -o "$OUT/bd3-cli-$vv" "$bdir/bin/llama-bd3-cli"
    echo "ok $vv"
  done
done
