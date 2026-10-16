# uso: cluster.py nodi.json
# Supervisore del coordinatore sul PC con più canali per nodo (USB + Wi-Fi).
# - per ogni nodo prova i canali (ricrea i forward adb se servono) e sceglie quello con la connessione più rapida;
# - avvia llama-server (tools/pc.sh) con quei canali;
# - se il coordinatore cade o /health non risponde, ripete la scelta e lo riavvia sull'altro canale.
#   Il riavvio è rapido perché i worker con -c hanno già i pesi in cache: ricevono solo gli hash.
# nodi.json: {"args": [...argomenti llama-server senza --rpc...],
#             "nodes": [{"name": "huawei", "paths": [{"kind": "usb", "serial": "GAB7...", "port": 50061},
#                                                    {"kind": "wifi", "host": "192.168.1.59", "port": 50052}]}]}
import json, os, socket, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(sys.argv[1]))
log = lambda *a: print(time.strftime('%H:%M:%S'), *a, flush=True)


def endpoint(p):
    if p['kind'] == 'usb':
        # il forward sparisce quando il collegamento adb si resetta: lo ricreo ogni volta
        r = subprocess.run(['adb', '-s', p['serial'], 'forward', f"tcp:{p['port']}", f"tcp:{p.get('remote', 50052)}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            return None
        return '127.0.0.1', p['port']
    return p['host'], p['port']


def connect_ms(host, port):
    """Tempo di apertura della connessione TCP (un round-trip); None se il canale non funziona.
    Solo connect e chiusura: il worker scarta il client senza HELLO e torna libero."""
    best = None
    for _ in range(3):
        try:
            t = time.perf_counter()
            socket.create_connection((host, port), timeout=2).close()
            ms = (time.perf_counter() - t) * 1000
            best = ms if best is None else min(best, ms)
        except OSError:
            return None
    return best


def choose():
    chosen = []
    for n in cfg['nodes']:
        best = None
        for p in n['paths']:
            ep = endpoint(p)
            ms = connect_ms(*ep) if ep else None
            log(f"  {n['name']} via {p['kind']}: " + (f"{ms:.1f} ms" if ms is not None else 'non raggiungibile'))
            if ms is not None and (best is None or ms < best[0]):
                best = (ms, p['kind'], ep)
        if best is None:
            log(f"  {n['name']}: nessun canale, nodo escluso")
            continue
        chosen.append((n['name'], best[1], f'{best[2][0]}:{best[2][1]}'))
    return chosen


def healthy():
    try:
        return b'"ok"' in urllib.request.urlopen('http://127.0.0.1:8090/health', timeout=3).read()
    except Exception:
        return False


while True:
    log('scelta dei canali')
    nodes = choose()
    if not nodes:
        log('nessun nodo raggiungibile, riprovo tra 10 s'); time.sleep(10); continue
    log('avvio: ' + ', '.join(f'{n} ({k})' for n, k, _ in nodes))
    rpc = ','.join(ep for _, _, ep in nodes)
    t0 = time.time()
    subprocess.run([os.path.join(HERE, 'pc.sh'), 'srv', '--rpc', rpc] + cfg['args'])
    while not healthy():
        time.sleep(1)
        if subprocess.run(['pgrep', '-f', 'build-x86c/bin/llama-server'], capture_output=True).returncode != 0:
            log('il coordinatore si è chiuso durante il caricamento'); break
    else:
        log(f'pronto in {time.time() - t0:.0f} s')
        fails = 0
        while fails < 3:  # 3 controlli falliti di fila = canale perso
            time.sleep(2)
            fails = 0 if healthy() else fails + 1
        log('coordinatore non risponde: cambio canale')
    subprocess.run([os.path.join(HERE, 'pc.sh'), 'stop'])
