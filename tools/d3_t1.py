#!/usr/bin/env python3
"""D3 attivita' 1: 8B Q4_K_M sul PC da solo, blocco 4. 3 ripetizioni per configurazione, CPU libera, temperature/frequenze."""
import json, os, re, sys
from d3 import run_reps
HERE = os.path.dirname(os.path.abspath(__file__))
M8 = os.path.expanduser("~/distai-models/Stable-DiffCoder-8B-Instruct.Q4_K_M.gguf")
sys.path.insert(0, HERE); from sdc_prompt import prompt
P = prompt("Write a Python function that checks if a number is prime, with a docstring and two unit tests.")
KEYS = "tokens nfe commit prefill_ms total_ms tok_s tok_per_nfe ms_per_nfe gen_ms gen_tok_s pass_med_ms pass_p90_ms rss_mb cpu_ms".split()
def parse(out):
    m = re.search(r"RESULT (.*)", out)
    return dict(kv.split("=") for kv in m.group(1).split()) if m else None
env = {"LD_LIBRARY_PATH": HERE + "/pcbin"}
res = []
for label, extra in [("bd3 8B thr0.9", ["--thr", "0.9"]), ("bd3 8B thr0.5", ["--thr", "0.5"]), ("bd3 8B 1tok/passo", ["--steps", "64"])]:
    cmd = [HERE + "/pcbin/llama-bd3-cli", "-m", M8, "--no-mmap", "--raw", "--mask", "5", "--eos", "2", "-b", "4", "-n", "64", "-t", "4", "-c", "1024", "-q", "-p", P] + extra
    rows = run_reps(cmd, reps=3, parse=parse, env=env, timeout=200, label=label)
    for r in rows:
        p = r["parsed"] or {}
        print(f"{label:20s} rep{r['rep']} gen_tok/s {p.get('gen_tok_s')} pass_med {p.get('pass_med_ms')} ms p90 {p.get('pass_p90_ms')} tok/nfe {p.get('tok_per_nfe')} "
              f"rss {p.get('rss_mb')} MB cpu {p.get('cpu_ms')} ms | {r['before']['temp_c']}->{r['after']['temp_c']} C {r['before']['mhz_avg']}->{r['after']['mhz_avg']} MHz", flush=True)
    res += rows
open(HERE + "/results-d3.jsonl", "a").write("\n".join(json.dumps({"task": 1, **r}) for r in res) + "\n")
