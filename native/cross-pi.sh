#!/usr/bin/env bash
# Cross-compila llama-server e ggml-rpc-server statici aarch64 per nodi Linux (Raspberry Pi 4 come "telefono" di test).
# Il Pi ha userland armhf ma kernel a 64 bit: un binario statico aarch64 gira lo stesso. Uso: ./cross-pi.sh utente@IP
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"; SRC="$HERE/llama.cpp"
for p in "$HERE"/patches/*.patch; do git -C "$SRC" apply --reverse --check "$p" 2>/dev/null || git -C "$SRC" apply "$p"; done
cmake -S "$SRC" -B "$HERE/build-pi" -DCMAKE_SYSTEM_NAME=Linux -DCMAKE_SYSTEM_PROCESSOR=aarch64 \
  -DCMAKE_C_COMPILER=aarch64-linux-gnu-gcc -DCMAKE_CXX_COMPILER=aarch64-linux-gnu-g++ \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=OFF -DCMAKE_EXE_LINKER_FLAGS="-static" \
  -DGGML_RPC=ON -DGGML_OPENMP=OFF -DGGML_NATIVE=OFF -DGGML_LLAMAFILE=ON -DGGML_CPU_ARM_ARCH=armv8-a \
  -DLLAMA_OPENSSL=OFF -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_UI=OFF \
  -DLLAMA_USE_PREBUILT_UI=OFF -DLLAMA_SUBPROCESS=OFF > "$HERE/build-pi.cmake.log"
cmake --build "$HERE/build-pi" --target llama-server ggml-rpc-server -j"$(nproc)" > "$HERE/build-pi.build.log"
if [ $# -ge 1 ]; then
  T=$(mktemp -d)
  aarch64-linux-gnu-strip -o "$T/llama-server-pi" "$HERE/build-pi/bin/llama-server"
  aarch64-linux-gnu-strip -o "$T/rpc-server-pi" "$HERE/build-pi/bin/ggml-rpc-server"
  ssh "$1" 'mkdir -p distai; pkill -f "^./(llama|rpc)-server-pi"; true'
  scp -q "$T/llama-server-pi" "$T/rpc-server-pi" "$1:distai/"; rm -r "$T"
fi
