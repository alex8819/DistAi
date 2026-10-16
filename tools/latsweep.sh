#!/usr/bin/env bash
# uso: latsweep.sh WORKER_IP "ritardi_ms" MODELLO NGL  -> coordinatore sul telefono (adb) -> adb reverse -> relay PC (+ritardo) -> worker
# Dalla pendenza tok/s vs ritardo si ricava il numero di attese sincrone di rete per token.
W=$1; DELAYS=$2; M=$3; NGL=$4; OUT=${OUT:-/tmp}
D=$(adb get-serialno); V=${V:-v8}
adb reverse tcp:50070 tcp:50070 >/dev/null
for d in $DELAYS; do
  python3 "$(dirname "$0")/rpcrelay.py" 50070 "$W" 50052 "$d" > "$OUT/relay-$d.log" 2>&1 & RP=$!
  sleep 1
  "$(dirname "$0")/srv.sh" "$D" "$V" -m "$M" -t 4 -tb 8 -c 2048 --rpc 127.0.0.1:50070 -ngl "$NGL" $EXTRA \
     -ot '"^(output|output_norm|token_embd)[.]weight$=CPU"' >/dev/null 2>&1
  sleep 3
  timeout 300 python3 "$(dirname "$0")/tokbench.py" http://127.0.0.1:18080 128 2 "relay+${d}ms" | grep -v istogramma
  kill $RP; sleep 0.5; tail -1 "$OUT/relay-$d.log"
done
