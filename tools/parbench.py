# uso: parbench.py URL n_predict "1 2 4"  -> tok/s aggregati con K richieste contemporanee (batching)
import json, sys, time, threading, urllib.request
url = sys.argv[1].rstrip('/'); N = int(sys.argv[2]); KS = [int(k) for k in sys.argv[3].split()]
P = "Scrivi un racconto breve su un faro in tempesta, numero {}."
def one(i, out):
    body = json.dumps({"prompt": P.format(i), "n_predict": N, "temperature": 0, "ignore_eos": True, "cache_prompt": False}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url + '/completion', body, {'Content-Type': 'application/json'}), timeout=1800))
    out.append(r['timings'])
one(0, [])  # riscaldamento
for k in KS:
    out = []; th = [threading.Thread(target=one, args=(i, out)) for i in range(k)]
    t0 = time.time(); [t.start() for t in th]; [t.join() for t in th]; dt = time.time() - t0
    per = sum(t['predicted_per_second'] for t in out) / k
    print(f"K={k}: aggregato {k * N / dt:.1f} tok/s (muro, incluso prompt) | per utente {per:.2f} tok/s | somma {per * k:.1f} tok/s", flush=True)
