#!/usr/bin/env python3
"""Router di prova con risposta in rami paralleli (Skeleton-of-Thought). Solo libreria standard.

uso: sot.py --replicas http://127.0.0.1:8091 http://192.168.1.34:8081 [--listen 8085] [--slots 2] [--mode on|off]
La modalità si sceglie all'AVVIO (default off = normale); poi si può cambiare a runtime o per singola richiesta.

Davanti a più repliche llama-server (API OpenAI):
  - MODALITÀ SPENTA (--mode off, header `X-DistAI-Parallel: 0`, o POST /sot/mode {"parallel":false}):
    proxy trasparente verso la replica meno carica, come il router di oggi. Nessun ramo, nessuna scaletta.
  - MODALITÀ ACCESA: su POST /v1/chat/completions chiede una scaletta (3-6 punti) alla replica meno carica, poi manda
    ogni punto a una replica diversa in parallelo e trasmette i rami in ordine. Se la scaletta dice DIRETTA, è troppo
    breve o fallisce, la richiesta passa com'è (fallback, motivo nel log).
Interruttori: --mode, header X-DistAI-Parallel (1/0, per singola richiesta), POST /sot/mode, GET /sot/status.
"""
import argparse, http.client, json, queue, re, sys, threading, time, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PLAN_INSTR = (
    "\n\n[ISTRUZIONE DI SERVIZIO, non rispondere ancora alla richiesta] Decidi come rispondere. Se la risposta migliore "
    "è breve (poche frasi), un calcolo, una traduzione, un unico blocco di codice, o un ragionamento in cui ogni passo "
    "dipende dal precedente, rispondi esattamente con la sola parola: DIRETTA. Altrimenti scrivi SOLO una scaletta di "
    "3-6 punti, uno per riga, nel formato \"1. titolo\" (titolo di massimo 12 parole), nella lingua della richiesta, "
    "senza introduzione né altro testo.")
BRANCH_INSTR = (
    "\n\n[Scaletta]\n{plan}\nScrivi SOLO il punto {n} (\"{title}\"), max {words} parole, senza introduzione né conclusione.")
POINT_RE = re.compile(r"^\s*(\d+)[.)]\s+(.{3,400})$", re.M)
HOP = {"transfer-encoding", "connection", "content-length", "keep-alive"}
CORS = {"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "*", "Access-Control-Allow-Methods": "*"}


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


class Backend:
    def __init__(self, url, slots):
        if "@" in url:
            url, slots = url.rsplit("@", 1)
            slots = int(slots)
        u = urllib.parse.urlparse(url)
        self.host, self.port, self.name, self.slots = u.hostname, u.port or 80, f"{u.hostname}:{u.port or 80}", slots
        self.inflight, self.healthy, self.served = 0, False, 0


class State:
    def __init__(self, backends, a):
        self.backends, self.a = backends, a
        self.parallel = a.mode == "on"
        self.lock = threading.Condition()
        self.stats = dict(parallel_ok=0, branches=0, fallback=0, passthrough=0, saved_s=0.0)
        self.fallbacks = {}

    def healthy(self):
        return [b for b in self.backends if b.healthy]

    def acquire(self, skip=(), wait=60.0, need_slot=True, prefer=None):
        """Replica sana meno carica (con uno slot libero, se possibile); aspetta fino a [wait] secondi."""
        t0 = time.time()
        with self.lock:
            while True:
                c = [b for b in self.healthy() if b not in skip]
                free = [b for b in c if b.inflight < b.slots]
                if prefer in free:
                    free = [prefer]
                pool = free or ([] if need_slot and time.time() - t0 < wait else c)
                if self.a.policy == "pack" and prefer is None:
                    pick = pool[0] if pool else None  # riempie la prima replica (le più veloci per prime): lotti da 4 efficienti
                else:
                    pick = min(pool, key=lambda b: b.inflight, default=None)
                if pick:
                    pick.inflight += 1
                    return pick
                if not c:
                    return None
                self.lock.wait(0.2)

    def release(self, b, ok=True):
        with self.lock:
            b.inflight -= 1
            b.served += 1 if ok else 0
            self.lock.notify_all()

    def count_fallback(self, why):
        self.stats["fallback"] += 1
        self.fallbacks[why] = self.fallbacks.get(why, 0) + 1
        log("fallback:", why)

    def status(self):
        return dict(parallel=self.parallel, stats=self.stats, fallbacks=self.fallbacks, replicas=[
            dict(host=b.name, healthy=b.healthy, inflight=b.inflight, served=b.served, slots=b.slots) for b in self.backends])


def health_loop(st):
    while True:
        for b in st.backends:
            try:
                c = http.client.HTTPConnection(b.host, b.port, timeout=1.5)
                c.request("GET", "/health")
                ok = c.getresponse().status == 200
                c.close()
            except Exception:
                ok = False
            if ok != b.healthy:
                b.healthy = ok
                log(b.name, "pronta" if ok else "persa")
        time.sleep(1)


def open_stream(b, path, body, timeout=180):
    c = http.client.HTTPConnection(b.host, b.port, timeout=timeout)
    c.request("POST", path, body, {"Content-Type": "application/json"})
    return c, c.getresponse()


def chat(b, payload, timeout):
    c, r = open_stream(b, "/v1/chat/completions", json.dumps(payload).encode(), timeout)
    try:
        return json.loads(r.read())
    finally:
        c.close()


def with_suffix(msgs, suffix):
    m = [dict(x) for x in msgs]
    m[-1]["content"] = m[-1]["content"] + suffix
    return m


def short_title(t):
    """I modelli piccoli scrivono frasi intere al posto dei titoli: tengo la prima frase, al massimo ~100 caratteri."""
    t = t.strip().strip("*").strip()
    t = re.split(r"(?<=[.!?:])\s", t)[0]
    if len(t) > 100:
        t = t[:100].rsplit(" ", 1)[0] + "…"
    return t.rstrip(".")


def make_plan(st, msgs, why):
    b = st.acquire(prefer=next((x for x in st.backends if x.healthy), st.backends[0]))
    if b is None:
        return None
    try:
        instr = PLAN_INSTR if not st.a.points else PLAN_INSTR.replace("3-6 punti", f"esattamente {st.a.points} punti")
        j = chat(b, dict(messages=with_suffix(msgs, instr), max_tokens=st.a.plan_tokens, temperature=0, stream=False), st.a.plan_timeout)
        text = j["choices"][0]["message"]["content"].strip()
        st.release(b)
    except Exception as e:
        st.release(b, False)
        why.append(f"scaletta fallita ({type(e).__name__})")
        return None
    if text.upper().startswith("DIRETTA"):
        why.append("scaletta: DIRETTA")
        return None
    pts = [(int(n), short_title(t)) for n, t in POINT_RE.findall(text)][:st.a.max_points]
    if st.a.points and len(pts) > st.a.points:
        pts = pts[:st.a.points]
    if len(pts) < 2:
        why.append("scaletta non valida")
        return None
    return [t for _, t in pts]


def run_branch(st, i, title, plan, msgs, params, q, cancel, conns):
    words = int(st.a.branch_tokens * 0.6)
    ptxt = "\n".join(f"{k + 1}. {t}" for k, t in enumerate(plan))
    m = with_suffix(msgs, BRANCH_INSTR.format(plan=ptxt, n=i + 1, title=title, words=words))
    body = json.dumps(dict(params, messages=m, max_tokens=st.a.branch_tokens, stream=True)).encode()
    skip, got = [], False
    for _ in range(2):
        b = st.acquire(skip)
        if b is None or cancel.is_set():
            break
        ok = False
        try:
            c, r = open_stream(b, "/v1/chat/completions", body)
            conns.append(c)
            for line in r:
                if cancel.is_set():
                    break
                if not line.startswith(b"data: ") or line.startswith(b"data: [DONE]"):
                    continue
                d = json.loads(line[6:])
                t = (d["choices"][0].get("delta") or {}).get("content")
                if t:
                    q.put(t)
                    got = True
            ok = True
        except Exception as e:
            log(f"ramo {i + 1} su {b.name}: {type(e).__name__}")
        finally:
            st.release(b, ok)
        if ok or got:
            break
        skip.append(b)
    if not got:
        q.put("_(parte non disponibile)_")
    q.put(None)


def serve_parallel(st, h, req, body_json):
    t0 = time.time()
    msgs = body_json.get("messages") or []
    why = []
    if not msgs or not isinstance(msgs[-1].get("content"), str) or body_json.get("tools"):
        why.append("richiesta non adatta")
    elif len(st.healthy()) < 2:
        why.append("meno di 2 repliche")
    plan = None if why else make_plan(st, msgs, why)
    if plan is None:
        st.count_fallback(why[0])
        return False
    log(f"scaletta in {time.time() - t0:.1f}s: {len(plan)} punti")
    params = {k: v for k, v in body_json.items() if k in ("temperature", "top_p", "top_k", "seed", "model")}
    if body_json.get("max_tokens"):
        st.a.branch_tokens = max(120, int(body_json["max_tokens"]) // len(plan))
    stream = bool(body_json.get("stream"))
    cancel, conns = threading.Event(), []
    qs = [queue.Queue() for _ in plan]
    for i, t in enumerate(plan):
        threading.Thread(target=run_branch, args=(st, i, t, plan, msgs, params, qs[i], cancel, conns), daemon=True).start()
    cid = f"chatcmpl-sot{int(t0 * 1000)}"
    out, ntok = [], 0

    def chunk(text, final=None):
        d = dict(id=cid, object="chat.completion.chunk", created=int(t0), model=params.get("model", "distai"),
                 choices=[dict(index=0, delta=({"content": text} if text is not None else {}), finish_reason=final)])
        return d

    def emit(text):
        out.append(text)
        if stream:
            h.wfile.write(b"data: " + json.dumps(chunk(text)).encode() + b"\n\n")
            h.wfile.flush()

    try:
        if stream:
            h.send_response(200)
            for k, v in {**CORS, "Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "close"}.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(b"data: " + json.dumps(dict(chunk(""), choices=[dict(index=0, delta={"role": "assistant", "content": ""}, finish_reason=None)])).encode() + b"\n\n")
        for i, t in enumerate(plan):
            emit(("\n\n" if i else "") + f"**{i + 1}. {t}**\n")
            while True:
                x = qs[i].get()
                if x is None:
                    break
                ntok += 1
                emit(x)
        wall = time.time() - t0
        timings = dict(predicted_n=ntok, predicted_per_second=ntok / wall if wall else 0, wall_s=wall)
        extra = dict(timings=timings, distai=dict(mode="parallel", branches=len(plan), replicas=[b.name for b in st.backends if b.served]))
        if stream:
            h.wfile.write(b"data: " + json.dumps(dict(chunk(None, "stop"), **extra)).encode() + b"\n\ndata: [DONE]\n\n")
            h.wfile.flush()
        else:
            h.send_response(200)
            data = json.dumps(dict(id=cid, object="chat.completion", created=int(t0), model=params.get("model", "distai"),
                                   choices=[dict(index=0, message=dict(role="assistant", content="".join(out)), finish_reason="stop")],
                                   usage=dict(completion_tokens=ntok), **extra)).encode()
            for k, v in {**CORS, "Content-Type": "application/json", "Content-Length": str(len(data))}.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(data)
        st.stats["parallel_ok"] += 1
        st.stats["branches"] += len(plan)
        log(f"risposta in {len(plan)} rami: {wall:.1f}s, ~{ntok} token, ~{ntok / wall:.1f} tok/s percepiti")
    except (BrokenPipeError, ConnectionResetError):
        cancel.set()
        for c in conns:
            try:
                c.close()
            except Exception:
                pass
        log("client chiuso: rami annullati")
    return True


def passthrough(st, h, method, path, body):
    skip = []
    while True:
        b = st.acquire(skip, need_slot=False)
        if b is None:
            h.send_response(503)
            for k, v in {**CORS, "Content-Type": "application/json"}.items():
                h.send_header(k, v)
            h.end_headers()
            h.wfile.write(b'{"error":"nessuna replica pronta"}')
            return
        sent = False
        try:
            c = http.client.HTTPConnection(b.host, b.port, timeout=180)
            c.request(method, path, body, {"Content-Type": "application/json"} if body else {})
            r = c.getresponse()
            h.send_response(r.status)
            for k, v in r.getheaders():
                if k.lower() not in HOP and not k.lower().startswith("access-control"):
                    h.send_header(k, v)
            for k, v in CORS.items():
                h.send_header(k, v)
            h.send_header("Connection", "close")
            h.end_headers()
            sent = True
            while True:
                d = r.read1(8192)
                if not d:
                    break
                h.wfile.write(d)
                h.wfile.flush()
            st.release(b)
            st.stats["passthrough"] += 1
            return
        except (BrokenPipeError, ConnectionResetError):
            st.release(b, False)
            return
        except Exception as e:
            st.release(b, False)
            if sent:
                return
            skip.append(b)
            log(f"{b.name}: {type(e).__name__}, riprovo altrove")


def make_handler(st):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            for k, v in {**CORS, "Content-Type": "application/json", "Content-Length": str(len(data))}.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self):
            self.send_response(204)
            for k, v in CORS.items():
                self.send_header(k, v)
            self.end_headers()

        def do_GET(self):
            if self.path == "/health":
                n = len(st.healthy())
                return self._json(200 if n else 503, dict(status="ok" if n else "no replicas", replicas=n))
            if self.path == "/sot/status":
                return self._json(200, st.status())
            passthrough(st, self, "GET", self.path, None)

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(n) if n else b""
            if self.path == "/sot/mode":
                try:
                    st.parallel = bool(json.loads(body)["parallel"])
                except Exception:
                    return self._json(400, dict(error='atteso {"parallel": true|false}'))
                log("modalità parallela", "ACCESA" if st.parallel else "SPENTA")
                return self._json(200, dict(parallel=st.parallel))
            if self.path == "/v1/chat/completions":
                hdr = self.headers.get("X-DistAI-Parallel")
                on = st.parallel if hdr is None else hdr.strip() in ("1", "true", "on")
                if on:
                    try:
                        j = json.loads(body)
                    except Exception:
                        j = None
                    if j is not None and serve_parallel(st, self, self.headers, j):
                        return
            passthrough(st, self, "POST", self.path, body)
    return H


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replicas", nargs="+", required=True, help="URL delle repliche llama-server")
    ap.add_argument("--listen", type=int, default=8085)
    ap.add_argument("--slots", type=int, default=2, help="richieste contemporanee per replica (-np)")
    ap.add_argument("--mode", choices=("on", "off"), default="off", help="scelta all'avvio: on = con rami paralleli (skeleton), off = normale (default)")
    ap.add_argument("--branch-tokens", type=int, default=220)
    ap.add_argument("--max-points", type=int, default=6)
    ap.add_argument("--points", type=int, default=0, help="se > 0 chiede esattamente questo numero di punti (4 e 8 sono lotti efficienti sui telefoni con dotprod)")
    ap.add_argument("--policy", choices=("spread", "pack"), default="spread", help="rami sulle repliche meno cariche (spread) o riempiendo la prima (pack)")
    ap.add_argument("--plan-timeout", type=float, default=25)
    ap.add_argument("--plan-tokens", type=int, default=70, help="token massimi della scaletta (la prima replica, la più veloce, la scrive)")
    a = ap.parse_args()
    st = State([Backend(u, a.slots) for u in a.replicas], a)
    threading.Thread(target=health_loop, args=(st,), daemon=True).start()
    log(f"in ascolto su :{a.listen}, rami paralleli {'ACCESI' if st.parallel else 'SPENTI'}, repliche: {[b.name for b in st.backends]}")
    ThreadingHTTPServer(("0.0.0.0", a.listen), make_handler(st)).serve_forever()


if __name__ == "__main__":
    main()
