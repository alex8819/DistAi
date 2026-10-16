#!/usr/bin/env bash
# uso: split3b.sh NGL_REMOTI "argomenti extra" ETICHETTA  -> 3B: coordinatore Huawei (adb) + worker Pi diretto in Wi-Fi
NGL=$1; EXTRA=$2; L=$3
M=${M:-/data/local/tmp/qwen2.5-3b-instruct-q4_k_m.gguf}
"$(dirname "$0")/srv.sh" $(adb get-serialno) v8 -m $M -t 4 -tb 8 -c 2048 --rpc 192.168.1.53:50052 -ngl $((NGL+1)) \
  -ot '"^(output|output_norm|token_embd)[.]weight$=CPU"' --poll 100 --cpu-strict 1 -C 0xF0 $EXTRA >/dev/null 2>&1
sleep 3
timeout 400 python3 "$(dirname "$0")/tokbench.py" http://127.0.0.1:18080 ${N:-96} 2 "$L" | grep -v istogramma
adb shell "grep -E 'draft acceptance|accept' /data/local/tmp/srv.log | tail -2"
