import json, os, re, sys
from d3 import run_reps
HERE = os.path.dirname(os.path.abspath(__file__))
B = os.path.join(HERE, "..", "native", "build-pc-rpc", "bin", "llama-bench")
M8 = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1 else "~/distai-models/Stable-DiffCoder-8B-Instruct.Q4_K_M.gguf")
def parse(out):
    m = re.search(r"pp4 \|\s+([\d.]+) ± ([\d.]+)", out); return {"pp4_tok_s": float(m.group(1)), "sd": float(m.group(2))} if m else None
rows = run_reps([B, "-m", M8, "-t", "4", "-p", "4", "-n", "0", "-fa", "1", "-r", "5"], reps=3, parse=parse, timeout=300, label="llama-bench pp4 PC solo " + os.path.basename(M8),
                env={"LD_LIBRARY_PATH": os.path.dirname(B)})
for r in rows:
    p = r["parsed"] or {}
    print(f"rep{r['rep']} pp4 {p.get('pp4_tok_s')} tok/s -> {1000*4/p['pp4_tok_s']:.0f} ms/passo | {r['before']['temp_c']}->{r['after']['temp_c']} C {r['before']['mhz_avg']}->{r['after']['mhz_avg']} MHz" if p else r["tail"], flush=True)
open(HERE + "/results-d3.jsonl", "a").write("\n".join(json.dumps({"task": "1b", **r}) for r in rows) + "\n")
