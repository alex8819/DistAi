# uso: tokbench.py URL [n_predict] [runs] [etichetta]
# Misura la generazione in streaming: tok/s del server e distribuzione dei tempi tra un token e l'altro (lato client).
import json, sys, time, statistics, urllib.request
url = sys.argv[1].rstrip('/'); N = int(sys.argv[2]) if len(sys.argv) > 2 else 256
RUNS = int(sys.argv[3]) if len(sys.argv) > 3 else 3; LABEL = sys.argv[4] if len(sys.argv) > 4 else ''
P = ("Spiega in modo dettagliato come funziona un motore a combustione interna, descrivendo i quattro tempi "
     "del ciclo Otto, i componenti principali e i vantaggi e gli svantaggi rispetto a un motore elettrico.")
for _ in range(600):
    try:
        if b'ok' in urllib.request.urlopen(url + '/health', timeout=2).read(): break
    except Exception: pass
    time.sleep(1)
def run():
    body = json.dumps({"prompt": P, "n_predict": N, "temperature": 0, "cache_prompt": False,
                       "ignore_eos": True, "stream": True}).encode()
    req = urllib.request.Request(url + '/completion', body, {'Content-Type': 'application/json'})
    stamps, timings = [], None
    with urllib.request.urlopen(req, timeout=900) as r:
        for line in r:
            if not line.startswith(b'data: '): continue
            d = json.loads(line[6:]); stamps.append(time.perf_counter())
            if d.get('stop'): timings = d.get('timings')
    gaps = [(b - a) * 1000 for a, b in zip(stamps[1:], stamps[2:-1])]  # salta il primo token (prompt) e il messaggio finale
    return timings, gaps
def pct(v, p): s = sorted(v); return s[min(len(s) - 1, int(p / 100 * len(s)))]
run()  # riscaldamento scartato
res = []
for i in range(RUNS):
    t, g = run()
    res.append(t['predicted_per_second'])
    hist = {}
    for x in g: hist[min(int(x // 25) * 25, 500)] = hist.get(min(int(x // 25) * 25, 500), 0) + 1
    print(f"{LABEL} run {i+1}: gen {t['predicted_per_second']:.2f} tok/s ({t['predicted_per_token_ms']:.1f} ms/tok) | "
          f"gap ms media {statistics.mean(g):.1f} mediana {statistics.median(g):.1f} p90 {pct(g,90):.1f} "
          f"p99 {pct(g,99):.1f} max {max(g):.1f} | prompt {t['prompt_per_second']:.1f} tok/s")
    print("   istogramma (25 ms):", " ".join(f"{k}:{v}" for k, v in sorted(hist.items())))
print(f"{LABEL} MEDIA gen {statistics.mean(res):.2f} tok/s")
