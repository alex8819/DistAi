#!/usr/bin/env bash
# uso: pc.sh stop | srv [args llama-server]  -> (ri)avvia il coordinatore x86 sul PC sulla porta 8090 (la 8080 è occupata)
HERE="$(cd "$(dirname "$0")/.." && pwd)"; LOG=${LOG:-/tmp/distai-pc-srv.log}
pkill -f "^$HERE/native/build-x86c/bin/llama-server" ; sleep 1
[ "$1" = srv ] || exit 0; shift
setsid "$HERE/native/build-x86c/bin/llama-server" "$@" --host 127.0.0.1 --port 8090 </dev/null >"$LOG" 2>&1 &
