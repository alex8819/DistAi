#!/usr/bin/env python3
"""Banco di prova per llama-server su un telefono (via adb) o sul PC: generazione e prompt su un prompt di codice.

uso come modulo:  from lbench import run;  run("xiaomi", "Q4_K_M base", "model.gguf", ["-t","2","-C","c0"])
uso da riga:      lbench.py DISPOSITIVO ETICHETTA MODELLO [argomenti llama-server...]
DISPOSITIVO: xiaomi | huawei | samsung | pc.  Il modello è cercato in .../files/models (telefoni) o ~/distai-models (PC).
Ogni misura è aggiunta a /tmp/claude-1000/m/results.jsonl (generazione tok/s di 3 esecuzioni dopo riscaldamento, prompt tok/s,
accettazione se c'è la bozza, temperatura batteria prima/dopo). Timeout brevi: un avvio che non risponde si abbandona.
"""
import json, os, statistics as st, subprocess, sys, time, urllib.request

DEV = {"xiaomi": (os.environ.get("SERIAL_XIAOMI", "SERIALE_XIAOMI"), "v82", 18101), "huawei": (os.environ.get("SERIAL_HUAWEI", "SERIALE_HUAWEI"), "v8", 18102),
       "samsung": (os.environ.get("SERIAL_SAMSUNG", "SERIALE_SAMSUNG"), "v82", 18103), "pc": (None, None, 18104)}
HERE = os.path.dirname(os.path.abspath(__file__))
PCBIN = os.path.join(HERE, "..", "native", "build-x86c", "bin", "llama-server")
RESULTS = "/tmp/claude-1000/m/results.jsonl"
PHONE_MODELS = "/sdcard/Android/data/com.distai.app/files/models"

CODE_PROMPT = ("Scrivi una classe PHP UserRepository con i metodi getUserById, getUserByEmail, createUser, updateUser e "
               "deleteUser che usano PDO con prepared statements, gestione delle eccezioni e commenti in italiano.")
SNIPPET = ("function getUserById(PDO $pdo, int $id): ?array {\n    $st = $pdo->prepare('SELECT * FROM users WHERE id = :id');\n"
           "    $st->execute([':id' => $id]);\n    return $st->fetch(PDO::FETCH_ASSOC) ?: null;\n}\n\n")
LONG_PROMPT = "Rivedi questo codice PHP e spiega i problemi:\n\n" + SNIPPET * 12 + "\nElenca i problemi in tre punti."


def sh(args, timeout=30):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout).stdout


def battery(serial):
    if not serial:
        return None
    out = sh(["adb", "-s", serial, "shell", "dumpsys battery"], 10)
    return {l.split(":")[0].strip(): l.split(":")[1].strip() for l in out.splitlines() if ":" in l and l.strip().split(":")[0] in ("level", "temperature")}


def start(dev, model, args):
    serial, variant, port = DEV[dev]
    base = os.path.expanduser("~/distai-models/") if dev == "pc" else PHONE_MODELS + "/"
    args = [base + a if a.endswith(".gguf") else a for a in args]  # "-md x.gguf": percorso completo del modello bozza
    if dev == "pc":
        subprocess.run("pkill -x llama-server", shell=True)
        time.sleep(1)
        cmd = [PCBIN, "-m", os.path.expanduser("~/distai-models/") + model] + args + ["--host", "127.0.0.1", "--port", str(port)]
        subprocess.Popen(cmd, stdout=open("/tmp/claude-1000/m/srv-pc.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
        return f"http://127.0.0.1:{port}"
    lib = sh(["adb", "-s", serial, "shell", "pm path com.distai.app"]).strip().replace("package:", "").replace("base.apk", "") + "lib/arm64"
    sh(["adb", "-s", serial, "shell", "pkill -f libllamaserver"], 10)
    time.sleep(1)
    line = f"cd /data/local/tmp && setsid {lib}/libllamaserver_{variant}.so -m {PHONE_MODELS}/{model} {' '.join(args)} --host 0.0.0.0 --port 8080 </dev/null >/data/local/tmp/srv.log 2>&1 &"
    try:
        subprocess.run(["adb", "-s", serial, "shell", line], timeout=4, capture_output=True)
    except subprocess.TimeoutExpired:
        pass  # adb resta agganciato al processo in background: il server è comunque partito
    sh(["adb", "-s", serial, "forward", f"tcp:{port}", "tcp:8080"], 10)
    return f"http://127.0.0.1:{port}"


def stop(dev):
    serial = DEV[dev][0]
    if dev == "pc":
        subprocess.run("pkill -x llama-server", shell=True)
    else:
        sh(["adb", "-s", serial, "shell", "pkill -f libllamaserver"], 10)


def ready(url, wait=240):
    t0 = time.time()
    while time.time() - t0 < wait:
        try:
            if b'"ok"' in urllib.request.urlopen(url + "/health", timeout=2).read():
                return time.time() - t0
        except Exception:
            time.sleep(1.5)
    return None


def complete(url, prompt, n, timeout=300):
    body = json.dumps({"prompt": prompt, "n_predict": n, "temperature": 0, "cache_prompt": False, "ignore_eos": True}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url + "/completion", body, {"Content-Type": "application/json"}), timeout=timeout))
    return r["timings"]


SET = {
    "php": CODE_PROMPT,
    "py": "Scrivi un modulo Python con una classe Stack e una classe Queue, docstring in italiano e test unitari con pytest.",
    "js": "Scrivi in JavaScript le funzioni debounce, throttle e memoize con commenti ed esempi d'uso.",
    "sql": "Scrivi lo schema SQL di un e-commerce (utenti, prodotti, ordini, righe d'ordine) con indici e cinque query di esempio.",
    "prosa": "Spiega in italiano che cos'è l'inflazione e come influisce sui risparmi di una famiglia.",
    "edit_php": "Riscrivi questo codice PHP aggiungendo i tipi di ritorno, la gestione delle eccezioni PDOException e un commento in italiano a ogni funzione. Restituisci solo il codice completo.\n\n```php\n" + SNIPPET + """function getUserByEmail(PDO $pdo, string $email): ?array {
    $st = $pdo->prepare('SELECT * FROM users WHERE email = :email');
    $st->execute([':email' => $email]);
    return $st->fetch(PDO::FETCH_ASSOC) ?: null;
}

function deleteUser(PDO $pdo, int $id): bool {
    $st = $pdo->prepare('DELETE FROM users WHERE id = :id');
    return $st->execute([':id' => $id]);
}
```""",
    "edit_py": "Aggiungi type hint e docstring in italiano a questo codice Python, senza cambiare la logica. Restituisci solo il codice completo.\n\n```python\nclass Stack:\n    def __init__(self):\n        self.items = []\n\n    def push(self, x):\n        self.items.append(x)\n\n    def pop(self):\n        if not self.items:\n            raise IndexError('stack vuoto')\n        return self.items.pop()\n\n    def peek(self):\n        return self.items[-1] if self.items else None\n\n    def size(self):\n        return len(self.items)\n```",
}


def run_set(dev, label, model, args, n=160, note=""):
    """Come run(), ma su 5 prompt diversi (4 di codice e 1 di prosa): una esecuzione ciascuno, temperatura 0."""
    serial = DEV[dev][0]
    b0 = battery(serial)
    url = start(dev, model, args)
    rec = {"dev": dev, "label": label, "model": model, "args": " ".join(args), "n": n, "time": time.strftime("%H:%M:%S"), "note": note, "set": True}
    t = ready(url)
    if t is None:
        rec["error"] = "avvio senza risposta"
    else:
        rec["load_s"] = round(t, 1)
        try:
            complete(url, "Ciao", 8)
            per, acc = {}, []
            for k, p in SET.items():
                tm = complete(url, p, n)
                per[k] = round(tm["predicted_per_second"], 2)
                if tm.get("draft_n"):
                    acc.append(round(tm["draft_n_accepted"] / tm["draft_n"], 2))
            code = [v for k, v in per.items() if k in ("php", "py", "js", "sql")]
            edit = [v for k, v in per.items() if k.startswith("edit")]
            rec.update(per=per, edit_mean=round(st.mean(edit), 2), code_mean=round(st.mean(code), 2), all_mean=round(st.mean(per.values()), 2), accept=acc)
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
    stop(dev)
    rec["batt_before"], rec["batt_after"] = b0, battery(serial)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{dev}] {label}: " + (rec.get("error") or f"modifica {rec['edit_mean']} codice {rec['code_mean']} tutto {rec['all_mean']} | {rec['per']} acc {rec['accept']}"), flush=True)
    return rec


def run(dev, label, model, args, n=128, runs=3, long_prompt=True, note=""):
    serial = DEV[dev][0]
    b0 = battery(serial)
    url = start(dev, model, args)
    rec = {"dev": dev, "label": label, "model": model, "args": " ".join(args), "n": n, "time": time.strftime("%H:%M:%S"), "note": note}
    t = ready(url)
    if t is None:
        rec["error"] = "avvio senza risposta"
        stop(dev)
    else:
        rec["load_s"] = round(t, 1)
        try:
            complete(url, CODE_PROMPT, 16)  # riscaldamento
            gens, acc = [], []
            for _ in range(runs):
                tm = complete(url, CODE_PROMPT, n)
                gens.append(tm["predicted_per_second"])
                if tm.get("draft_n"):
                    acc.append(tm["draft_n_accepted"] / tm["draft_n"])
            rec.update(gen=[round(x, 2) for x in gens], gen_mean=round(st.mean(gens), 2), gen_med=round(st.median(gens), 2),
                       cv=round(100 * st.pstdev(gens) / st.mean(gens), 1))
            if acc:
                rec["accept"] = round(st.mean(acc), 2)
            if long_prompt:
                try:
                    tm = complete(url, LONG_PROMPT, 1)
                    rec["prompt_n"], rec["prompt_tps"] = tm["prompt_n"], round(tm["prompt_per_second"], 1)
                except Exception as e:
                    rec["prompt_error"] = f"{type(e).__name__}: {e}"
        except Exception as e:
            rec["error"] = f"{type(e).__name__}: {e}"
        stop(dev)
    rec["batt_before"], rec["batt_after"] = b0, battery(serial)
    with open(RESULTS, "a") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"[{dev}] {label}: " + (rec.get("error") or f"gen {rec['gen_mean']} (med {rec['gen_med']}, cv {rec['cv']}%) prompt {rec.get('prompt_tps')} tok/s"
          + (f" accett. {rec['accept']}" if 'accept' in rec else "")), flush=True)
    return rec


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4:])
