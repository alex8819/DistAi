#!/usr/bin/env python3
"""D3: velocita' del solo telefono (server RPC, un solo dispositivo) contro il PC locale, stesso modello e stesso pp, per ricavare il rapporto di velocita' r = T_tel / T_pc.
uso: d3_solo.py xiaomi|huawei MODELLO.gguf [pp] [threads_tel]"""
import json, os, re, subprocess, sys, time
from d3 import quiet, env_snapshot
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "native", "build-pc-rpc", "bin")
PH = {"xiaomi": os.environ.get("SERIAL_XIAOMI", "SERIALE_XIAOMI"), "huawei": os.environ.get("SERIAL_HUAWEI", "SERIALE_HUAWEI")}
phone, model = sys.argv[1], os.path.expanduser(sys.argv[2]); pp = sys.argv[3] if len(sys.argv) > 3 else "4"; thr = sys.argv[4] if len(sys.argv) > 4 else "8"; mask = sys.argv[5] if len(sys.argv) > 5 else ""; REPS = int(os.environ.get("REPS", "3"))
ser = PH[phone]
def sh(c, t=30): return subprocess.run(c, shell=True, capture_output=True, text=True, timeout=t)
def bench(args):
    p = subprocess.run([f"{BIN}/llama-bench", "-m", model, "-p", pp, "-n", "0", "-fa", "1", "-r", "5"] + args, capture_output=True, text=True, timeout=400, env={**os.environ, "LD_LIBRARY_PATH": BIN})
    m = re.search(rf"pp{pp}\s*\|\s*([\d.]+) ± ([\d.]+)", p.stdout); return (float(m.group(1)), float(m.group(2))) if m else (None, None)
out = []
with quiet():
    for rep in range(REPS):
        sh("for p in $(pgrep -x ggml-rpc-server); do kill $p; done"); sh(f"adb -s {ser} shell \"for p in \\$(pidof rpcs); do kill \\$p; done\"", 15); time.sleep(1)
        sh(f"adb -s {ser} forward tcp:50212 tcp:50062")
        subprocess.Popen(f"adb -s {ser} shell 'cd /data/local/tmp && GGML_RPC_POLL=100 {('GGML_RPC_CPUMASK=' + mask) if mask else ''} ./rpcs -H 0.0.0.0 -p 50062 -t {thr}' > /tmp/claude-1000/d3-solo.log 2>&1", shell=True, start_new_session=True)
        time.sleep(3)
        tel = bench(["-t", "1", "-rpc", "127.0.0.1:50212", "-dev", "RPC0"])
        sh(f"adb -s {ser} shell \"for p in \\$(pidof rpcs); do kill \\$p; done\"", 15)
        loc = bench(["-t", "4"])
        r = {"phone": phone, "model": os.path.basename(model), "pp": pp, "thr_tel": thr, "cpumask": mask, "rep": rep + 1, "tel_tok_s": tel[0], "tel_sd": tel[1], "pc_local_tok_s": loc[0], "pc_sd": loc[1],
             "ratio_tel_over_pc": round(loc[0] / tel[0], 2) if tel[0] and loc[0] else None, "pc_temp": env_snapshot()["temp_c"]}
        print(json.dumps(r), flush=True); out.append(r)
open(HERE + "/results-d3.jsonl", "a").write("\n".join(json.dumps({"task": "solo", **r}) for r in out) + "\n")
