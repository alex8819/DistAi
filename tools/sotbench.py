#!/usr/bin/env python3
"""Confronto rami paralleli / normale sulle stesse richieste, attraverso tools/sot.py.

uso: sotbench.py [URL=http://127.0.0.1:8085] [ripetizioni=2] [cartella_output=/tmp/sot]
Per ogni richiesta: modo normale (header X-DistAI-Parallel: 0) e parallelo (1), in streaming. Stampa tempo al primo
token, tempo totale, token (conteggio dei delta SSE, approssimato) e tok/s percepiti; salva le risposte per il
confronto di qualità a occhio. Le richieste "brevi" servono a verificare il fallback (DIRETTA) e il costo della scaletta.
"""
import json, os, statistics as st, sys, time, urllib.request

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8085").rstrip("/")
REP = int(sys.argv[2]) if len(sys.argv) > 2 else 2
OUT = sys.argv[3] if len(sys.argv) > 3 else "/tmp/sot"
LUNGHE = [
    "Spiega in dettaglio come funziona un motore a combustione interna, con i quattro tempi e i componenti principali.",
    "Confronta pro e contro di cinque linguaggi di programmazione per chi inizia: Python, JavaScript, Java, C e Rust.",
    "Scrivi una guida per aprire un negozio di informatica: permessi, fornitori, magazzino, assistenza, marketing.",
    "Descrivi sei cause della caduta dell'Impero romano d'Occidente.",
    "Proponi un piano di allenamento settimanale per principianti, giorno per giorno, con spiegazioni.",
    "Documenta in sezioni un'API REST per la gestione di un magazzino: autenticazione, prodotti, ordini, errori.",
]
CODICE = [
    "Scrivi una classe PHP UserRepository con getUserById, getUserByEmail, createUser, updateUser e deleteUser usando PDO e prepared statements, con commenti.",
    "Scrivi un modulo Python per gestire una rubrica: classe Contact, classe AddressBook con aggiungi, cerca, elimina, salva e carica da JSON, e test con pytest.",
    "Scrivi in JavaScript un piccolo router HTTP con Node.js (senza librerie) che gestisce GET, POST, parametri nel percorso e middleware, con esempi d'uso.",
]
BREVI = [
    "Quanto fa 17 per 23? Spiega il calcolo.",
    "Traduci in inglese: il gatto dorme sul divano.",
    "Scrivi una funzione Python che inverte una stringa.",
]


def run(prompt, parallel):
    body = json.dumps(dict(messages=[dict(role="user", content=prompt)], stream=True, temperature=0, max_tokens=900)).encode()
    req = urllib.request.Request(URL + "/v1/chat/completions", body,
                                 {"Content-Type": "application/json", "X-DistAI-Parallel": "1" if parallel else "0"})
    t0, first, n, text, info = time.time(), None, 0, [], None
    with urllib.request.urlopen(req, timeout=600) as r:
        for line in r:
            if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                continue
            d = json.loads(line[6:])
            if d.get("distai"):
                info = d["distai"]
            t = (d["choices"][0].get("delta") or {}).get("content")
            if t:
                first = first or time.time() - t0
                n += 1
                text.append(t)
    dt = time.time() - t0
    return dict(first=first or dt, total=dt, tokens=n, tps=n / dt if dt else 0, text="".join(text), branches=(info or {}).get("branches", 0))


os.makedirs(OUT, exist_ok=True)
print(f"{'richiesta':<44} {'modo':<9} {'1° tok s':>8} {'tot s':>7} {'token':>6} {'tok/s':>7}  rami")
tot = {True: [], False: []}
for kind, lst in [k for k in (("L", LUNGHE), ("C", CODICE), ("B", BREVI)) if k[0] in os.environ.get("SOT_KINDS", "LCB")]:
    for i, p in enumerate(lst):
        for par in (False, True):
            rs = []
            for k in range(REP):
                try:
                    rs.append(run(p, par))
                except Exception as e:
                    print(f"  errore {type(e).__name__}: {e}")
            if not rs:
                continue
            open(f"{OUT}/{kind}{i + 1}-{'par' if par else 'norm'}.md", "w").write(rs[-1]["text"])
            m = lambda key: st.mean(r[key] for r in rs)
            if kind in ("L", "C"):
                tot[par].append(m("tps"))
            print(f"{kind}{i + 1} {p[:40]:<41} {'parallelo' if par else 'normale':<9} {m('first'):8.1f} {m('total'):7.1f} "
                  f"{m('tokens'):6.0f} {m('tps'):7.1f}  {rs[-1]['branches'] or '-'}")
if tot[True] and tot[False]:
    a, b = st.mean(tot[False]), st.mean(tot[True])
    print(f"\nRichieste lunghe, media tok/s percepiti: normale {a:.1f} | parallelo {b:.1f} | rapporto {b / a:.2f}x")
print(f"Risposte salvate in {OUT}/ (confronta a occhio i file *-norm.md e *-par.md)")
