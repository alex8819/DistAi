#!/usr/bin/env python3
"""Prova distribuita: llama-server sul PC con una parte dei layer su Xiaomi e Huawei via RPC (cavo USB), misura tok/s.

uso: distrun.py MODELLO LAYER_REMOTI TS [etichetta]      es.: distrun.py Qwen3-...gguf 12 6,6
I telefoni eseguono librpcserver (stessa versione del PC) con thread e core fissati; il PC tiene l'embedding e l'uscita, così in rete
viaggia solo lo stato nascosto (vedi NodeService.startCoordinator). Layer remoti = ultimi N; TS = ripartizione tra Xiaomi e Huawei.
"""
import json, os, subprocess, sys, time
import lbench

HERE = os.path.dirname(os.path.abspath(__file__))
PCBIN = os.path.join(HERE, "..", "native", "build-x86c", "bin", "llama-server")
NODES = [("xiaomi", 50152, "c0", 2), ("huawei", 50153, "f0", 4)]


def start_rpc(dev, local_port, mask, threads):
    serial, variant, _ = lbench.DEV[dev]
    lib = lbench.sh(["adb", "-s", serial, "shell", "pm path com.distai.app"]).strip().replace("package:", "").replace("base.apk", "") + "lib/arm64"
    lbench.sh(["adb", "-s", serial, "shell", "pkill -f librpcserver"], 10)
    time.sleep(1)
    line = (f"cd /data/local/tmp && mkdir -p rpc-cache && LLAMA_CACHE=/data/local/tmp/rpc-cache GGML_RPC_CPUMASK={mask} GGML_RPC_POLL=100 "
            f"setsid {lib}/librpcserver_{variant}.so -H 127.0.0.1 -p 50052 -t {threads} -c </dev/null >/data/local/tmp/rpc.log 2>&1 &")
    try:
        subprocess.run(["adb", "-s", serial, "shell", line], timeout=4, capture_output=True)
    except subprocess.TimeoutExpired:
        pass
    lbench.sh(["adb", "-s", serial, "forward", f"tcp:{local_port}", "tcp:50052"], 10)


def main():
    model, remote, ts = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    label = sys.argv[4] if len(sys.argv) > 4 else f"{remote} layer remoti ts {ts}"
    subprocess.run("pkill -x llama-server", shell=True)
    for dev, port, mask, th in NODES:
        start_rpc(dev, port, mask, th)
    time.sleep(2)
    port = 18104
    cmd = [PCBIN, "-m", os.path.expanduser("~/distai-models/") + model, "--rpc", ",".join(f"127.0.0.1:{p}" for _, p, _, _ in NODES),
           "-ngl", str(remote + 1), "-ot", "^(output|output_norm|token_embd)[.]weight$=CPU", "-ts", ts, "-c", "2048", "-t", "4", "--poll", "100",
           "--host", "127.0.0.1", "--port", str(port)]
    log = open("/tmp/claude-1000/m/srv-dist.log", "w")
    subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    url = f"http://127.0.0.1:{port}"
    t = lbench.ready(url, 900)
    rec = {"dev": "dist", "label": label, "model": model, "args": " ".join(cmd[3:]), "time": time.strftime("%H:%M:%S")}
    if t is None:
        rec["error"] = "avvio senza risposta"
    else:
        rec["load_s"] = round(t, 1)
        try:
            lbench.complete(url, "Ciao", 8)
            g = [lbench.complete(url, lbench.CODE_PROMPT, 64)["predicted_per_second"] for _ in range(3)]
            rec.update(gen=[round(x, 2) for x in g], gen_mean=round(sum(g) / len(g), 2))
            tm = lbench.complete(url, lbench.LONG_PROMPT, 1)
            rec["prompt_tps"] = round(tm["prompt_per_second"], 1)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
    with open(lbench.RESULTS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[dist] {label}: " + (rec.get("error") or f"gen {rec['gen_mean']} {rec['gen']} prompt {rec.get('prompt_tps')} tok/s (caricamento {rec['load_s']} s)"), flush=True)
    subprocess.run("pkill -x llama-server", shell=True)
    for dev, *_ in NODES:
        lbench.sh(["adb", "-s", lbench.DEV[dev][0], "shell", "pkill -f librpcserver"], 10)


if __name__ == "__main__":
    main()
