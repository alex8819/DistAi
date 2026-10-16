#!/usr/bin/env bash
# uso: pcrun.sh ETICHETTA [args llama-server]  -> avvia il coordinatore sul PC, attende /health (max 180 s), misura
L=$1; shift; H="$(cd "$(dirname "$0")" && pwd)"
"$H/pc.sh" srv "$@"
for i in $(seq 1 36); do sleep 5; curl -s -m 2 127.0.0.1:8090/health | grep -q '"ok"' && break
  pgrep -f "build-x86c/bin/llama-server" >/dev/null || { echo "$L: CADUTO"; grep -E " E |error" /tmp/distai-pc-srv.log | head -3; exit 1; }; done
timeout 300 python3 "$H/tokbench.py" http://127.0.0.1:8090 ${N:-128} 2 "$L" | grep -E "MEDIA|run" | sed 's/| gap.*| prompt/| prompt/'
grep -E "draft acceptance" /tmp/distai-pc-srv.log | tail -1 | sed 's/.*draft acceptance/   accettazione/'
