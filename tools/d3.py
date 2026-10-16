#!/usr/bin/env python3
"""D3: harness di misura controllata. Mette in pausa (SIGSTOP) Firefox e gnome-software durante le prove e li riprende alla fine
(nessun dato perso), registra per ogni ripetizione: temperatura del package, frequenza media dei core, carico, RAM disponibile.

uso come modulo:  from d3 import quiet, env_snapshot, run_reps
"""
import atexit, contextlib, json, os, re, signal, statistics as st, subprocess, sys, time

HEAVY_PATTERNS = ["/snap/firefox/", "gnome-software", "thunderbird", "/snap/chromium/"]
_stopped = []


def _pids():
    out = subprocess.run(["pgrep", "-f", "|".join(HEAVY_PATTERNS)], capture_output=True, text=True).stdout.split()
    me = {os.getpid(), os.getppid()}
    return [int(p) for p in out if int(p) not in me]


def quiet_on():
    for p in _pids():
        try:
            os.kill(p, signal.SIGSTOP); _stopped.append(p)
        except ProcessLookupError:
            pass


def quiet_off():
    for p in _stopped:
        try:
            os.kill(p, signal.SIGCONT)
        except ProcessLookupError:
            pass
    _stopped.clear()


atexit.register(quiet_off)
for _s in (signal.SIGINT, signal.SIGTERM):
    signal.signal(_s, lambda *a: (quiet_off(), sys.exit(1)))


@contextlib.contextmanager
def quiet():
    quiet_on()
    time.sleep(1.0)
    try:
        yield
    finally:
        quiet_off()


def pkg_temp():
    t = subprocess.run(["sensors"], capture_output=True, text=True).stdout
    m = re.search(r"Package id 0:\s*\+?([\d.]+)", t)
    return float(m.group(1)) if m else None


def freqs():
    f = [float(x.split(":")[1]) for x in open("/proc/cpuinfo") if x.startswith("cpu MHz")]
    return round(st.mean(f)), round(max(f))


def env_snapshot():
    mem = {l.split(":")[0]: int(l.split()[1]) for l in open("/proc/meminfo") if l.split(":")[0] in ("MemAvailable", "MemFree", "SwapFree")}
    avg_f, max_f = freqs()
    return {"temp_c": pkg_temp(), "mhz_avg": avg_f, "mhz_max": max_f, "load1": os.getloadavg()[0], "mem_avail_mb": mem["MemAvailable"] // 1024}


def run_reps(cmd, reps=3, parse=None, env=None, timeout=240, label=""):
    """Esegue cmd `reps` volte con la CPU libera. Ritorna una lista di dict {wall_s, before, after, parsed}. """
    rows = []
    with quiet():
        for r in range(reps):
            before = env_snapshot()
            t0 = time.perf_counter()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, **(env or {})})
                out = p.stdout + p.stderr
            except subprocess.TimeoutExpired as e:
                out = (e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or "")
            wall = time.perf_counter() - t0
            after = env_snapshot()
            rows.append({"label": label, "rep": r + 1, "wall_s": round(wall, 2), "before": before, "after": after,
                         "parsed": parse(out) if parse else None, "tail": out[-300:]})
    return rows


if __name__ == "__main__":
    print(json.dumps(env_snapshot()))
