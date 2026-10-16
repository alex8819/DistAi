#!/usr/bin/env python3
"""Solo token/s del dLLM (ggml bd3): media su 4 prompt di codice. uso: tput.py ETICHETTA [--adb SER] [opzioni di llama-bd3-cli...]"""
import json, os, re, subprocess, sys, shlex
HERE = os.path.dirname(os.path.abspath(__file__))
PROMPTS = ["Write a Python function that checks if a number is prime, with a docstring.",
           "Write a Python class LRUCache with get and put methods using OrderedDict.",
           "Write a PHP class UserRepository with getUserById and createUser using PDO prepared statements.",
           "Write a Python function that merges two sorted lists and a few unit tests."]
label = sys.argv[1]; rest = sys.argv[2:]; adb = None
if rest[:1] == ["--adb"]: adb = rest[1]; rest = rest[2:]
tot_t = tot_ms = nfe = 0; passes = []
for p in PROMPTS:
    if adb:
        model = [r for r in rest if r.endswith(".gguf")]
        cmd = ["adb", "-s", adb, "shell", "cd /data/local/tmp && ./bd3 " + " ".join(shlex.quote(r) for r in rest) + " -q -p " + shlex.quote(p)]
    else:
        cmd = [HERE + "/pcbin/llama-bd3-cli"] + rest + ["-q", "-p", p]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=90, env={**os.environ, "LD_LIBRARY_PATH": HERE + "/pcbin"}).stdout
    except subprocess.TimeoutExpired:
        out = ""
    m = re.search(r"RESULT tokens=(\d+) nfe=(\d+) commit=(\d+) prefill_ms=(\d+) total_ms=(\d+) tok_s=([\d.]+) tok_per_nfe=([\d.]+) ms_per_nfe=(\d+)", out)
    if not m: continue
    tot_t += int(m[1]); tot_ms += int(m[5]); nfe += int(m[2]); passes.append(int(m[8]))
r = {"label": label, "args": " ".join(rest), "tokens": tot_t, "tok_s": round(tot_t / max(1, tot_ms) * 1000, 2),
     "tok_per_nfe": round(tot_t / max(1, nfe), 2), "ms_per_pass": round(sum(passes) / max(1, len(passes)))}
print(json.dumps(r)); open(HERE + "/results-tput.jsonl", "a").write(json.dumps(r) + "\n")
