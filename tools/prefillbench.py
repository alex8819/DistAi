#!/usr/bin/env python3
"""Prefill contro generazione: quanto tempo va nella lettura del prompt e quanto nella scrittura.

uso: [LABEL=nome] prefillbench.py URL [LUNGHEZZE] [GEN]      es.: prefillbench.py http://127.0.0.1:18104 128,512,2048 64
Per ogni lunghezza manda un prompt di codice vero (i sorgenti di tools/) tagliato a quel numero di token, con un prefisso
diverso a ogni richiesta e cache_prompt spento (mai misurare rileggendo lo stesso prompt), e genera GEN token con ignore_eos.
Stampa prompt tok/s, generazione tok/s e la quota di tempo del prefill per un turno d'agente tipico (prompt lungo, risposta corta).
Ogni misura è aggiunta a tools/results-prefill.jsonl.
"""
import glob, json, os, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results-prefill.jsonl")


def post(url, path, body, timeout=900):
    req = urllib.request.Request(url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=timeout))


def source_tokens(url):
    text = "".join(open(f, encoding="utf-8").read() for f in sorted(glob.glob(os.path.join(HERE, "*.py"))))
    return post(url, "/tokenize", {"content": text})["tokens"]


def main():
    url = sys.argv[1].rstrip("/")
    sizes = [int(x) for x in (sys.argv[2] if len(sys.argv) > 2 else "128,256,512,1024,2048").split(",")]
    gen = int(sys.argv[3]) if len(sys.argv) > 3 else 64
    label = os.environ.get("LABEL", url)
    toks = source_tokens(url)
    post(url, "/completion", {"prompt": "riscaldamento", "n_predict": 4, "cache_prompt": False})
    rows = []
    for i, n in enumerate(sizes):
        nonce = post(url, "/tokenize", {"content": f"# richiesta {time.time_ns()} {i}\n"})["tokens"]
        prompt = nonce + toks[(i * 997) % max(1, len(toks) - n):][: n - len(nonce)]
        r = post(url, "/completion", {"prompt": prompt, "n_predict": gen, "ignore_eos": True, "cache_prompt": False,
                                      "temperature": 0})
        t = r["timings"]
        row = {"n": t["prompt_n"], "prompt_ms": round(t["prompt_ms"]), "prompt_tps": round(t["prompt_per_second"], 1),
               "gen_n": t["predicted_n"], "gen_tps": round(t["predicted_per_second"], 2)}
        rows.append(row)
        print(f"  prompt {row['n']:>5} token: {row['prompt_tps']:>7} tok/s ({row['prompt_ms']} ms) | generazione {row['gen_tps']} tok/s", flush=True)
    big = rows[-1]
    gen_tps = sum(r["gen_tps"] for r in rows) / len(rows)
    # turno d'agente tipico: legge il prompt più lungo misurato e scrive 300 token
    t_pre, t_gen = big["n"] / big["prompt_tps"], 300 / gen_tps
    share = t_pre / (t_pre + t_gen)
    print(f"== {label}: turno da {big['n']} letti + 300 scritti = {t_pre:.1f} s prefill + {t_gen:.1f} s generazione "
          f"-> prefill {share:.0%} del tempo")
    res = {"label": label, "time": time.strftime("%Y-%m-%d %H:%M"), "rows": rows, "agent_turn_prefill_share": round(share, 3)}
    open(RESULTS, "a", encoding="utf-8").write(json.dumps(res, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
