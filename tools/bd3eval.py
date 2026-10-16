#!/usr/bin/env python3
"""Valuta llama-bd3-cli (ggml, cache KV) sui compiti di codeeval: qualita' + token/s.
uso: bd3eval.py ETICHETTA MODELLO.gguf [--thr X | --steps N] [--block B] [-n T] [-t K] [--tasks easy|hard|all] [--adb SERIAL]"""
import argparse, json, os, re, shlex, subprocess, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))
ap = argparse.ArgumentParser()
ap.add_argument("label"); ap.add_argument("model")
ap.add_argument("--thr", type=float, default=None); ap.add_argument("--steps", type=int, default=0)
ap.add_argument("--block", type=int, default=32); ap.add_argument("-n", type=int, default=192)
ap.add_argument("-t", type=int, default=4); ap.add_argument("--tasks", default="all"); ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--adb", default=None); ap.add_argument("--extra", default="")
a = ap.parse_args()
os.environ["CODEEVAL_SET"] = a.tasks
os.makedirs("/tmp/claude-1000/m", exist_ok=True)
import codeeval
tasks = codeeval.TASKS[: a.limit or None]
ok = tok = 0; nfe = 0; wall = 0.0; msn = []
for name, sig, desc, test in tasks:
    prompt = f"Scrivi in Python `{sig}`: {desc}. Rispondi solo con un blocco di codice ```python, senza spiegazioni."
    mode = f"--steps {a.steps}" if a.steps else f"--thr {a.thr if a.thr is not None else 0.9}"
    args = f"-n {a.n} -b {a.block} {mode} -t {a.t} {a.extra}"
    if a.adb:
        cmd = ["adb", "-s", a.adb, "shell", f"cd /data/local/tmp && ./bd3 -m {os.path.basename(a.model)} {args} -p {shlex.quote(prompt)}"]
    else:
        cmd = [f"{HERE}/pcbin/llama-bd3-cli", "-m", a.model] + args.split() + ["-p", prompt]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=120, env={**os.environ, 'LD_LIBRARY_PATH': HERE + '/pcbin'}).stdout
    except subprocess.TimeoutExpired:
        out = ""
    m = re.search(r"RESULT tokens=(\d+) nfe=(\d+) commit=(\d+) prefill_ms=(\d+) total_ms=(\d+) tok_s=([\d.]+) tok_per_nfe=([\d.]+) ms_per_nfe=(\d+)", out)
    text = out[: out.rfind("RESULT")] if m else out
    passed, err = codeeval.check(codeeval.extract(text), test)
    ok += passed
    if m:
        t, n, tm = int(m[1]), int(m[2]), int(m[5]); tok += t; nfe += n; wall += tm / 1000; msn.append(int(m[8]))
    print(f"  {'OK ' if passed else 'NO '} {name:<18} {m.group(0)[7:70] if m else 'no result'} {err}", flush=True)
res = {"label": a.label, "engine": "ggml-bd3", "thr": a.thr, "steps": a.steps, "block": a.block, "threads": a.t, "passed": ok, "total": len(tasks),
       "tokens": tok, "nfe": nfe, "tok_per_nfe": round(tok / max(1, nfe), 2), "tok_s": round(tok / max(1e-9, wall), 2),
       "ms_per_pass": round(sum(msn) / max(1, len(msn))), "wall_s": round(wall, 1)}
print(json.dumps(res))
open(os.path.join(HERE, "results-dllm.jsonl"), "a").write(json.dumps(res) + "\n")
