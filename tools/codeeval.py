#!/usr/bin/env python3
"""Valutatore di qualità del codice: 14 piccoli compiti Python con test automatici (stile HumanEval, in italiano).

uso: [LABEL=nome] codeeval.py URL[@slot] [URL[@slot] ...]   (llama-server o router con API OpenAI; più endpoint = coda di agenti)
Per ogni compito chiede la funzione (temperatura 0), estrae il primo blocco ``` e lo esegue con i test in un processo
isolato (python3 -I, timeout 5 s). Stampa i compiti superati, i token/s e salva il dettaglio in /tmp/claude-1000/m/codeeval.jsonl.
"""
import json, os, re, subprocess, sys, tempfile, time, urllib.request

TASKS = [
    ("is_prime", "is_prime(n)", "restituisce True se n è un numero primo, altrimenti False",
     "assert is_prime(2) and is_prime(97) and not is_prime(1) and not is_prime(0) and not is_prime(91) and not is_prime(-7)"),
    ("fizzbuzz", "fizzbuzz(n)", "restituisce la lista di stringhe da 1 a n con le regole FizzBuzz classiche",
     "assert fizzbuzz(15)[2]=='Fizz' and fizzbuzz(15)[4]=='Buzz' and fizzbuzz(15)[14]=='FizzBuzz' and fizzbuzz(3)==['1','2','Fizz']"),
    ("reverse_words", "reverse_words(s)", "inverte l'ordine delle parole di una frase separate da spazi singoli",
     "assert reverse_words('ciao bel mondo')=='mondo bel ciao' and reverse_words('uno')=='uno'"),
    ("gcd", "gcd(a, b)", "restituisce il massimo comun divisore di due interi non negativi",
     "assert gcd(12,18)==6 and gcd(7,13)==1 and gcd(0,5)==5 and gcd(100,10)==10"),
    ("flatten", "flatten(lst)", "appiattisce una lista di liste annidate a qualunque profondità in una lista semplice",
     "assert flatten([1,[2,[3,[4]],5]])==[1,2,3,4,5] and flatten([])==[] and flatten([[],[[]]])==[]"),
    ("roman_to_int", "roman_to_int(s)", "converte un numero romano in intero",
     "assert roman_to_int('III')==3 and roman_to_int('IX')==9 and roman_to_int('MCMXCIV')==1994 and roman_to_int('LVIII')==58"),
    ("merge_sorted", "merge_sorted(a, b)", "unisce due liste ordinate in una sola lista ordinata senza usare sorted",
     "assert merge_sorted([1,3,5],[2,4,6])==[1,2,3,4,5,6] and merge_sorted([],[1])==[1] and merge_sorted([1,1],[1])==[1,1,1]"),
    ("count_vowels", "count_vowels(s)", "conta le vocali (a, e, i, o, u, anche maiuscole) in una stringa",
     "assert count_vowels('Ciao Mondo')==5 and count_vowels('xyz')==0 and count_vowels('AEIOU')==5"),
    ("is_anagram", "is_anagram(a, b)", "restituisce True se due stringhe sono anagrammi ignorando maiuscole e spazi",
     "assert is_anagram('Roma','Amor') and is_anagram('a b c','cba') and not is_anagram('ciao','cia')"),
    ("fib", "fib(n)", "restituisce l'n-esimo numero di Fibonacci con fib(0)=0 e fib(1)=1, in modo efficiente",
     "assert fib(0)==0 and fib(1)==1 and fib(10)==55 and fib(80)==23416728348467685"),
    ("two_sum", "two_sum(nums, target)", "restituisce la lista [i, j] con i < j degli indici di due numeri che sommano a target (esiste una sola soluzione)",
     "assert two_sum([2,7,11,15],9)==[0,1] and two_sum([3,2,4],6)==[1,2] and two_sum([3,3],6)==[0,1]"),
    ("valid_parentheses", "valid_parentheses(s)", "restituisce True se le parentesi (), [] e {} nella stringa sono bilanciate e annidate correttamente",
     "assert valid_parentheses('()[]{}') and valid_parentheses('{[()]}') and not valid_parentheses('(]') and not valid_parentheses('((') and valid_parentheses('')"),
    ("chunk", "chunk(lst, n)", "divide una lista in sottoliste di lunghezza n (l'ultima può essere più corta)",
     "assert chunk([1,2,3,4,5],2)==[[1,2],[3,4],[5]] and chunk([],3)==[] and chunk([1,2],5)==[[1,2]]"),
    ("dedupe", "dedupe(lst)", "rimuove i duplicati mantenendo l'ordine della prima occorrenza",
     "assert dedupe([3,1,3,2,1])==[3,1,2] and dedupe([])==[] and dedupe(['a','a'])==['a']"),
]

HARD = [
    ("LRUCache", "class LRUCache(capacity) con i metodi get(key) (restituisce -1 se assente) e put(key, value)", "implementa una cache LRU con evizione dell'elemento usato meno di recente, get e put in O(1)",
     "c=LRUCache(2); c.put(1,1); c.put(2,2); assert c.get(1)==1; c.put(3,3); assert c.get(2)==-1; c.put(4,4); assert c.get(1)==-1 and c.get(3)==3 and c.get(4)==4; c.put(4,9); assert c.get(4)==9"),
    ("eval_rpn", "eval_rpn(tokens)", "valuta un'espressione in notazione polacca inversa (lista di stringhe con + - * / e interi, anche negativi; la divisione è intera troncata verso zero)",
     "assert eval_rpn(['2','1','+','3','*'])==9 and eval_rpn(['4','13','5','/','+'])==6 and eval_rpn(['10','6','9','3','+','-11','*','/','*','17','+','5','+'])==22 and eval_rpn(['7','-2','/'])==-3"),
    ("decode_string", "decode_string(s)", "decodifica stringhe del tipo k[testo] annidate, per esempio '3[a2[c]]' diventa 'accaccacc'",
     "assert decode_string('3[a]2[bc]')=='aaabcbc' and decode_string('3[a2[c]]')=='accaccacc' and decode_string('2[abc]3[cd]ef')=='abcabccdcdcdef' and decode_string('abc')=='abc'"),
    ("group_anagrams", "group_anagrams(words)", "raggruppa le parole che sono anagrammi tra loro e restituisce la lista dei gruppi",
     "r=group_anagrams(['eat','tea','tan','ate','nat','bat']); assert sorted(sorted(g) for g in r)==[['ate','eat','tea'],['bat'],['nat','tan']]"),
    ("rotate_matrix", "rotate_matrix(m)", "restituisce una nuova matrice quadrata ruotata di 90 gradi in senso orario",
     "assert rotate_matrix([[1,2,3],[4,5,6],[7,8,9]])==[[7,4,1],[8,5,2],[9,6,3]] and rotate_matrix([[1]])==[[1]] and rotate_matrix([[1,2],[3,4]])==[[3,1],[4,2]]"),
    ("max_subarray", "max_subarray(nums)", "restituisce la somma massima di un sottoarray contiguo non vuoto",
     "assert max_subarray([-2,1,-3,4,-1,2,1,-5,4])==6 and max_subarray([1])==1 and max_subarray([-3,-1,-2])==-1 and max_subarray([5,4,-1,7,8])==23"),
    ("num_islands", "num_islands(grid)", "conta le isole in una griglia (lista di stringhe di '1' terra e '0' acqua) con connessione orizzontale e verticale",
     "assert num_islands(['11110','11010','11000','00000'])==1 and num_islands(['11000','11000','00100','00011'])==3 and num_islands([])==0"),
    ("longest_palindrome", "longest_palindrome(s)", "restituisce la sottostringa palindroma più lunga (in caso di parità la prima)",
     "assert longest_palindrome('babad') in ('bab','aba') and longest_palindrome('cbbd')=='bb' and longest_palindrome('a')=='a' and longest_palindrome('forgeeksskeegfor')=='geeksskeeg'"),
]
import os as _os
_SET = _os.environ.get("CODEEVAL_SET", "all")
TASKS = {"easy": TASKS, "hard": HARD, "all": TASKS + HARD}[_SET]


def ask(url, content, max_tokens=500):
    body = json.dumps({"messages": [{"role": "user", "content": content}], "temperature": 0, "max_tokens": max_tokens}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(url + "/v1/chat/completions", body, {"Content-Type": "application/json"}), timeout=600))
    return r["choices"][0]["message"]["content"], r.get("timings", {})


def extract(text):
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    if m:
        return m.group(1)
    m = re.search(r"```(?:python|py)?\s*\n(.*)", text, re.S)  # blocco non chiuso (troncato)
    return m.group(1) if m else text


def check(code, test):
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir="/tmp/claude-1000/m") as f:
        f.write(code + "\n\n" + test + "\n")
    try:
        r = subprocess.run(["python3", "-I", f.name], capture_output=True, text=True, timeout=5)
        return r.returncode == 0, (r.stderr.strip().splitlines() or [""])[-1][:100]
    except subprocess.TimeoutExpired:
        return False, "timeout"


def main():
    """Uno o più endpoint: `URL` oppure `URL@slot` (slot = richieste contemporanee). Con più slot i compiti formano una coda
    da cui pescano tutti i lavoratori (uno per slot): i dispositivi più veloci ne fanno di più. Etichetta: variabile LABEL."""
    import queue, threading
    eps = []
    for arg in sys.argv[1:]:
        url, _, n = arg.partition("@")
        eps.append((url.rstrip("/"), int(n or 1)))
    label = os.environ.get("LABEL") or ",".join(u for u, _ in eps)
    q = queue.Queue()
    for t in TASKS:
        q.put(t)
    rows, toks, lock, t0 = {}, [], threading.Lock(), time.time()
    done_by = {}

    def worker(url):
        while True:
            try:
                name, sig, desc, test = q.get_nowait()
            except queue.Empty:
                return
            prompt = f"Scrivi in Python `{sig}`: {desc}. Rispondi solo con un blocco di codice ```python, senza spiegazioni."
            try:
                text, tm = ask(url, prompt)
                passed, err = check(extract(text), test)
            except Exception as e:
                passed, err, tm = False, f"{type(e).__name__}: {e}", {}
            with lock:
                rows[name] = {"task": name, "pass": passed, "err": err}
                done_by[url] = done_by.get(url, 0) + 1
                if tm.get("predicted_per_second"):
                    toks.append(tm["predicted_per_second"])
                print(f"  {'OK ' if passed else 'NO '} {name:<18} {err}  [{url}]", flush=True)

    ths = [threading.Thread(target=worker, args=(u,)) for u, n in eps for _ in range(n)]
    [t.start() for t in ths]; [t.join() for t in ths]
    ok = sum(r["pass"] for r in rows.values())
    res = {"label": label, "passed": ok, "total": len(TASKS), "tok_s": round(sum(toks) / len(toks), 2) if toks else None,
           "wall_s": round(time.time() - t0, 1), "per_endpoint": done_by, "tasks": list(rows.values())}
    open("/tmp/claude-1000/m/codeeval.jsonl", "a").write(json.dumps(res, ensure_ascii=False) + "\n")
    print(f"== {label}: {ok}/{len(TASKS)} superati | {res['tok_s']} tok/s per richiesta | {res['wall_s']} s | compiti per endpoint {done_by}")


if __name__ == "__main__":
    main()
