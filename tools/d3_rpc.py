#!/usr/bin/env python3
"""D3 attivita' 2-4: 8B Q4_K_M, PC (server RPC) + un telefono (server RPC), llama-bench pp4, split layer/tensor/row.
Server con GGML_RPC_STATS=1 (tempo per comando). Per ogni invocazione di llama-bench (reload del modello) si registrano: pp4, tempi server, RAM telefono.
uso: d3_rpc.py TELEFONO(xiaomi|huawei) MODO(layer|tensor|row) TS_PC/TS_TEL [reps] [--path adb|teth] [--model PATH]
"""
import json, os, re, subprocess, sys, time, argparse
from d3 import quiet, env_snapshot
HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.join(HERE, "..", "native", "build-pc-rpc", "bin")
PH = {"xiaomi": (os.environ.get("SERIAL_XIAOMI", "SERIALE_XIAOMI"), 8), "huawei": (os.environ.get("SERIAL_HUAWEI", "SERIALE_HUAWEI"), 4), "samsung": (os.environ.get("SERIAL_SAMSUNG", "SERIALE_SAMSUNG"), 4)}
ap = argparse.ArgumentParser()
ap.add_argument("phone"); ap.add_argument("mode"); ap.add_argument("ts"); ap.add_argument("reps", nargs="?", type=int, default=3)
ap.add_argument("--path", default="adb"); ap.add_argument("--tel-ip", default=None); ap.add_argument("--pc-ip", default="192.168.x.x")
ap.add_argument("--model", default=os.path.expanduser("~/distai-models/Stable-DiffCoder-8B-Instruct.Q4_K_M.gguf"))
ap.add_argument("--mask", default=""); ap.add_argument("--threads-tel", type=int, default=0); ap.add_argument("--p", type=int, default=4); ap.add_argument("--r", type=int, default=5)
a = ap.parse_args()
serial, nthr = PH[a.phone]
nthr = a.threads_tel or nthr
assert a.phone != "samsung", "il Samsung e' riservato all'utente"


def sh(cmd, t=30):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=t)


def kill_all():
    sh("for p in $(pgrep -x ggml-rpc-server) $(pgrep -x llama-bench); do kill $p; done")
    sh(f"adb -s {serial} shell \"for p in \\$(pgrep -f 'rpcs -H'); do kill \\$p; done\"", 15)


def phone_mem():
    t = sh(f"adb -s {serial} shell \"grep -E 'MemAvailable|MemFree' /proc/meminfo; pgrep -f 'rpcs -H' | head -1\"", 15).stdout.split("\n")
    d = {l.split(":")[0]: int(l.split()[1]) // 1024 for l in t if ":" in l}
    return d


def phone_therm():
    out = sh(f"adb -s {serial} shell \"dumpsys battery | grep -E 'temperature|level'; for c in 0 4 7; do cat /sys/devices/system/cpu/cpu$c/cpufreq/scaling_cur_freq 2>/dev/null; done; cat /sys/class/thermal/thermal_zone0/temp 2>/dev/null\"", 15).stdout
    return " ".join(out.split())


def phone_rss():
    out = sh(f"adb -s {serial} shell \"for p in \\$(pidof rpcs); do grep -E 'VmRSS|VmHWM' /proc/\\$p/status; done\"", 15).stdout
    return {l.split(":")[0]: int(l.split()[1]) // 1024 for l in out.strip().split("\n") if ":" in l}


def one_run():
    kill_all(); time.sleep(1)
    if a.path == "adb":
        sh(f"adb -s {serial} forward tcp:50212 tcp:50062; adb -s {serial} forward tcp:51212 tcp:51062; adb -s {serial} reverse tcp:51101 tcp:51101")
        pc_ep, tel_ep, pc_host = "127.0.0.1:50101", "127.0.0.1:50212", "127.0.0.1"
    else:
        pc_ep, tel_ep, pc_host = f"{a.pc_ip}:50101", f"{a.tel_ip}:50062", "0.0.0.0"
    pclog, tellog = f"/tmp/claude-1000/d3-pc.log", f"/tmp/claude-1000/d3-tel.log"
    subprocess.Popen(f"GGML_RPC_STATS=1 '{BIN}/ggml-rpc-server' -H 0.0.0.0 -t 4 -p 50101 > {pclog} 2>&1", shell=True, start_new_session=True)
    subprocess.Popen(f"adb -s {serial} shell 'cd /data/local/tmp && GGML_RPC_STATS=1 GGML_RPC_POLL=100 {('GGML_RPC_CPUMASK=' + a.mask) if a.mask else ''} ./rpcs -H 0.0.0.0 -p 50062 -t {nthr}' > {tellog} 2>&1",
                     shell=True, start_new_session=True)
    time.sleep(3)
    mem0 = phone_mem(); th0 = phone_therm()
    before = env_snapshot()
    cmd = [f"{BIN}/llama-bench", "-m", a.model, "-t", "1", "-p", str(a.p), "-n", "0", "-fa", "1", "-r", str(a.r), "-rpc", f"{pc_ep},{tel_ep}", "-dev", "RPC0/RPC1",
           "-sm", a.mode, "-ts", a.ts]
    t0 = time.perf_counter()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=420, env={**os.environ, "LD_LIBRARY_PATH": BIN})
        out = p.stdout + p.stderr
    except subprocess.TimeoutExpired as e:
        out = "TIMEOUT " + str(e.stdout or "")[-300:]
    wall = time.perf_counter() - t0
    after = env_snapshot()
    rss = phone_rss()
    mem1 = phone_mem(); th1 = phone_therm()
    time.sleep(1); kill_all(); time.sleep(1)
    pcs = re.findall(r"RPC_STATS.*", open(pclog).read()); tels = re.findall(r"RPC_STATS.*", open(tellog).read())
    m = re.search(rf"pp{a.p}\s*\|\s*([\d.]+) ± ([\d.]+)", out)
    err = re.findall(r"(?:error|Error|failed|not support)[^\n]{0,160}", out)[:3]
    comm = re.findall(r"[^\n]*(?:communicator|comm_init)[^\n]*", out)[:2]
    return {"phone": a.phone, "mode": a.mode, "ts": a.ts, "path": a.path, "mask": a.mask, "thr_tel": nthr, "pp_tok_s": float(m.group(1)) if m else None, "sd": float(m.group(2)) if m else None,
            "ms_per_pass": round(1000 * a.p / float(m.group(1))) if m else None, "wall_s": round(wall), "pc_stats": pcs[-1:] , "tel_stats": tels[-1:], "phone_mem_mb_before": mem0,
            "phone_mem_mb_after": mem1, "phone_rss_mb": rss, "phone_therm_before": th0, "phone_therm_after": th1, "pc_before": before, "pc_after": after, "errors": err, "comm_lines": comm, "tail": out[-250:] if not m else ""}


rows = []
with quiet():
    for r in range(a.reps):
        row = one_run(); row["rep"] = r + 1; rows.append(row)
        print(f"[{a.phone} {a.mode} ts={a.ts} {a.path}] rep{r+1}: pp{a.p} {row['pp_tok_s']} tok/s = {row['ms_per_pass']} ms/passo | PC {row['pc_before']['temp_c']}->{row['pc_after']['temp_c']}C "
              f"{row['pc_before']['mhz_avg']}->{row['pc_after']['mhz_avg']}MHz | phone avail {row['phone_mem_mb_before'].get('MemAvailable')}->{row['phone_mem_mb_after'].get('MemAvailable')}MB rss {row['phone_rss_mb']} | tel {row['phone_therm_before']} -> {row['phone_therm_after']}"
              + (f" | ERR {row['errors']} {row['tail']}" if not row['pp_tok_s'] else ""), flush=True)
        print("   pc_stats ", row["pc_stats"], "\n   tel_stats", row["tel_stats"], flush=True)
open(HERE + "/results-d3.jsonl", "a").write("\n".join(json.dumps({"task": "2-4", **r}) for r in rows) + "\n")
