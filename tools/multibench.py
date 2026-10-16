# uso: multibench.py n_predict URL:K [URL:K ...]  -> K utenti contemporanei su ogni nodo (repliche), tok/s totali del cluster
import json, sys, time, threading, urllib.request
N = int(sys.argv[1]); nodes = [(a.rsplit(':', 1)[0], int(a.rsplit(':', 1)[1])) for a in sys.argv[2:]]
P = "Scrivi un racconto breve su un faro in tempesta, numero {}."
res = {}
def one(url, i):
    body = json.dumps({"prompt": P.format(i), "n_predict": N, "temperature": 0, "ignore_eos": True, "cache_prompt": False}).encode()
    t = json.load(urllib.request.urlopen(urllib.request.Request(url + '/completion', body, {'Content-Type': 'application/json'}), timeout=1800))['timings']
    res.setdefault(url, []).append(t['predicted_per_second'])
th = [threading.Thread(target=one, args=(u, i)) for u, k in nodes for i in range(k)]
t0 = time.time(); [t.start() for t in th]; [t.join() for t in th]; dt = time.time() - t0
tot = 0
for u, v in res.items():
    tot += sum(v); print(f"{u}: {len(v)} utenti, {sum(v):.1f} tok/s (per utente {sum(v)/len(v):.1f})")
print(f"TOTALE cluster: {tot:.1f} tok/s di generazione | {sum(k for _, k in nodes) * N / dt:.1f} tok/s a muro ({dt:.0f} s)")
