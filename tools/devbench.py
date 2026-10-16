# uso: devbench.py [NOME=host:porta[@status] ...] [--adb] [--groups] [--model M.gguf] [--json out.json]
# Banco di prova dei dispositivi del cluster, con lo stesso protocollo RPC dell'inferenza (i numeri sono quelli che contano):
#   - latenza: ping ICMP (se l'IP è diretto) e round-trip RPC min/mediana/p90 sul canale indicato (USB, Wi-Fi, ponte del PC);
#   - trasmissione: 64 MB con SET_TENSOR, come al caricamento del modello (MB/s);
#   - banda RAM effettiva: prodotti matrice-vettore Q4_0 su 38 MB (limita la generazione dei token, GB/s);
#   - calcolo: prodotti matrice-matrice Q4_0 x 64 token (limita la lettura del prompt, GFLOPS).
# Senza argomenti trova i nodi in mDNS (_distai._tcp); con --adb aggiunge i telefoni sul cavo (adb forward).
# --groups: ripete banda e calcolo per gruppo di core (big/medi/little/tutti sui telefoni via adb; sul PC core fisici
#   contro thread), avviando un worker di prova sulla porta 50059. I worker del cluster devono essere liberi.
# --model: per ogni dispositivo ms per layer, costo di rete per token, tempo di caricamento e fattore limitante.
import argparse, json, os, platform, re, shutil, statistics, struct, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import rpcbench as rb  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')  # la console di Windows è cp1252
OS = platform.system()
SET_TENSOR, DEVICE_COUNT = 6, 15
UPLOAD_MB = 64
TEST_PORT = 50059     # worker di prova per i gruppi di core
HOP_MS = 10.0         # come SplitPlan.HOP_MS

ap = argparse.ArgumentParser()
ap.add_argument('nodes', nargs='*', help='NOME=host:porta[@host:porta_status]; senza: mDNS')
ap.add_argument('--adb', action='store_true', help='aggiunge i telefoni collegati via cavo')
ap.add_argument('--groups', action='store_true', help='banda e calcolo per gruppo di core')
ap.add_argument('--model', help="GGUF per l'analisi del fattore limitante")
ap.add_argument('--json', help='salva i risultati')
a = ap.parse_args()


def find_adb():
    for d in filter(None, [os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT'),
                           os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk'),
                           os.path.expanduser('~/Library/Android/sdk'), os.path.expanduser('~/Android/Sdk')]):
        p = os.path.join(d, 'platform-tools', 'adb' + ('.exe' if OS == 'Windows' else ''))
        if os.path.exists(p):
            return p
    return shutil.which('adb')


ADB = find_adb()


def adb(serial, *args, timeout=30):
    try:
        return subprocess.run([ADB, '-s', serial, *args], capture_output=True, text=True, encoding='utf-8',
                              errors='replace', timeout=timeout).stdout
    except subprocess.TimeoutExpired:
        return ''  # un comando lanciato in background tiene aperta la shell: va bene così


def status(ep):
    try:
        h, p = ep.rsplit(':', 1)
        return json.load(urllib.request.urlopen(f'http://{h}:{p}/status', timeout=4))
    except Exception:
        return {}


# ------------------------------------------------------------------ misure via RPC

def rtt_stats(ep, n=30):
    """Round-trip RPC puro (DEVICE_COUNT): minimo, mediana, 90° percentile in ms."""
    r = rb.Rpc(ep)
    t = []
    for _ in range(n):
        s = time.perf_counter(); r.call(DEVICE_COUNT, b''); t.append((time.perf_counter() - s) * 1000)
    r.s.close()
    t.sort()
    return t[0], statistics.median(t), t[int(len(t) * 0.9) - 1]


def upload_mbs(ep):
    """MB/s di SET_TENSOR (come il caricamento dei pesi), chiuso da una lettura sincrona."""
    r = rb.Rpc(ep, timeout=60)
    size = UPLOAD_MB << 20
    buf, _ = struct.unpack('<QQ', r.call(rb.ALLOC, struct.pack('<IQ', 0, size + 4096)))
    if buf == 0:
        raise MemoryError('il worker non ha RAM per la prova di trasmissione')
    base, = struct.unpack('<Q', r.call(rb.GET_BASE, struct.pack('<Q', buf)))
    base = (base + 63) // 64 * 64
    chunk = 8 << 20
    t_ = rb.tensor(1, rb.F32, buf, (size // 4,), (4,), data=base)
    payload = bytes(range(256)) * (chunk // 256)
    s = time.perf_counter()
    for off in range(0, size, chunk):
        r.send(SET_TENSOR, t_ + b'\x00' + struct.pack('<Q', off) + payload)
    r.call(rb.GET_TENSOR, t_ + struct.pack('<QQ', 0, 4))  # risponde solo dopo aver ricevuto tutto
    dt = time.perf_counter() - s
    r.send(rb.FREE, struct.pack('<Q', buf)); r.s.close()
    return size / dt / 1e6


compute_gflops = rb.compute_gflops  # in rpcbench.py, condivisa con pcnode.py


def membw(ep):
    """Banda effettiva di prodotti matrice-vettore Q4_0 (rpcbench): quanto veloce legge i pesi a ogni token."""
    return rb.bench(ep)[2]


def ping_ms(host):
    if host in ('127.0.0.1', 'localhost'):
        return None
    cmd = ['ping', '-n', '5', host] if OS == 'Windows' else ['ping', '-c', '5', '-i', '0.3', host]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, errors='replace', timeout=15).stdout
    except subprocess.TimeoutExpired:
        return None
    t = [float(x.replace(',', '.')) for x in re.findall(r'(?:time|durata|tempo)[=<]\s*([\d.,]+)', out)]
    return statistics.mean(t) if t else None


def bench_ep(ep):
    res = {}
    for key, fn in (('rtt', rtt_stats), ('upload', upload_mbs), ('membw', membw), ('gflops', compute_gflops)):
        try:
            res[key] = fn(ep)
        except Exception as e:
            res[key + '_err'] = str(e)[:80]
    return res


# ------------------------------------------------------------------ gruppi di core

def phone_clusters(serial):
    """Gruppi di core per frequenza massima, dal più veloce: [(nome, maschera esadecimale, n)] più "tutti"."""
    out = adb(serial, 'shell', 'for c in /sys/devices/system/cpu/cpu[0-9]*; do echo ${c##*cpu} $(cat $c/cpufreq/cpuinfo_max_freq); done')
    f = {}
    for line in out.split('\n'):
        p = line.split()
        if len(p) == 2 and p[1].isdigit():
            f.setdefault(int(p[1]), []).append(int(p[0]))
    groups, names = [], ['big', 'medi', 'little', 'altri']
    for i, (freq, cpus) in enumerate(sorted(f.items(), reverse=True)):
        groups.append((f'{names[min(i, 3)]} {len(cpus)}x{freq / 1e6:.2f} GHz', format(sum(1 << c for c in cpus), 'x'), len(cpus)))
    if len(groups) > 1:
        n = sum(g[2] for g in groups)
        groups.append((f'tutti {n}', format(sum(int(g[1], 16) for g in groups), 'x'), n))
    return groups


def phone_groups(serial):
    """Worker di prova sul telefono (binario dell'app) per ogni gruppo di core, raggiunto con adb forward."""
    apk = adb(serial, 'shell', 'pm path com.distai.app').strip().replace('package:', '').replace('base.apk', '')
    adb(serial, 'forward', 'tcp:50058', 'tcp:50053')
    variant = status('127.0.0.1:50058').get('variant', 'v8')
    lib = f'{apk}lib/arm64/librpcserver_{variant}.so'
    out = []
    for name, mask, n in phone_clusters(serial):
        adb(serial, 'shell', 'pkill -f "[r]pcserver_v.*-p 50059"')
        adb(serial, 'shell', f'cd /data/local/tmp && GGML_RPC_CPUMASK={mask} setsid {lib} -H 127.0.0.1 -p {TEST_PORT} '
                             f'-t {n} </dev/null >/dev/null 2>&1 &', timeout=5)
        adb(serial, 'forward', f'tcp:{TEST_PORT}', f'tcp:{TEST_PORT}')
        time.sleep(1.5)
        r = {'gruppo': f'{name} (maschera {mask})'}
        try:
            r['membw'], r['gflops'] = membw(f'127.0.0.1:{TEST_PORT}'), compute_gflops(f'127.0.0.1:{TEST_PORT}')
        except Exception as e:
            r['err'] = str(e)[:60]
        out.append(r)
    adb(serial, 'shell', 'pkill -f "[r]pcserver_v.*-p 50059"')
    adb(serial, 'forward', '--remove', f'tcp:{TEST_PORT}')
    return out


def pc_groups():
    """PC: core fisici (un thread per core) contro i primi thread logici e contro tutti i thread."""
    exe = os.path.join(HERE, '..', 'native', 'build-pc', 'bin', 'ggml-rpc-server' + ('.exe' if OS == 'Windows' else ''))
    if not os.path.exists(exe):
        return []
    logical = os.cpu_count() or 4
    phys = logical // 2 if logical >= 4 else logical
    # Windows numera i due thread di un core uno accanto all'altro (0-1 = core 0); Linux di solito prima tutti i core
    phys_mask = sum(1 << (2 * i) for i in range(phys)) if OS == 'Windows' else (1 << phys) - 1
    configs = [(f'{phys} core fisici', format(phys_mask, 'x'), phys),
               (f'primi {phys} thread logici', format((1 << phys) - 1, 'x'), phys),
               (f'tutti {logical} thread', format((1 << logical) - 1, 'x'), logical)]
    out = []
    for name, mask, n in configs:
        p = subprocess.Popen([exe, '-H', '127.0.0.1', '-p', str(TEST_PORT), '-t', str(n)],
                             env=dict(os.environ, GGML_RPC_CPUMASK=mask), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.5)
        r = {'gruppo': f'{name} (maschera {mask})'}
        try:
            r['membw'], r['gflops'] = membw(f'127.0.0.1:{TEST_PORT}'), compute_gflops(f'127.0.0.1:{TEST_PORT}')
        except Exception as e:
            r['err'] = str(e)[:60]
        p.terminate(); p.wait(5)
        out.append(r)
    return out


# ------------------------------------------------------------------ elenco dei nodi

def mdns_nodes():
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return []
    found = {}

    class L:
        def add_service(self, zc, t, n):
            i = zc.get_service_info(t, n, 3000)
            if i and i.parsed_addresses():
                found[n.split('._')[0].replace('DistAI ', '')] = f'{i.parsed_addresses()[0]}:{i.port}'

        def update_service(self, *x):
            pass

        def remove_service(self, *x):
            pass
    zc = Zeroconf(); ServiceBrowser(zc, '_distai._tcp.local.', L()); time.sleep(5); zc.close()
    return [(n, ep, None) for n, ep in sorted(found.items())]


nodes = []
for spec in a.nodes:
    name, eps = spec.split('=', 1)
    rpc, _, st = eps.partition('@')
    nodes.append((name, rpc, st or None))
if not nodes:
    nodes = mdns_nodes()
phones = []
if a.adb and ADB:
    devs = [l.split()[0] for l in subprocess.run([ADB, 'devices'], capture_output=True, text=True).stdout.split('\n')[1:]
            if l.strip().endswith('device')]
    for i, s in enumerate(devs):
        port = 50140 + 2 * i
        adb(s, 'forward', f'tcp:{port}', 'tcp:50052'); adb(s, 'forward', f'tcp:{port + 1}', 'tcp:50053')
        model = adb(s, 'shell', 'getprop ro.product.model').strip()
        nodes.append((f'{model} (USB)', f'127.0.0.1:{port}', f'127.0.0.1:{port + 1}'))
        phones.append((s, model))

results = []
for name, ep, st_ep in nodes:
    host, port = ep.rsplit(':', 1)
    st = status(st_ep or f'{host}:{int(port) + 1}')
    print(f'… {name} ({ep})', flush=True)
    results.append({'nome': name, 'endpoint': ep, 'dev': st.get('dev', ''), 'ram': st.get('ram'), 'libera': st.get('free'),
                    'core': st.get('cores'), 'variante': st.get('variant'), 'tempC': st.get('tempC'),
                    'batteria': st.get('battery'), 'ping': ping_ms(host), **bench_ep(ep)})

groups = {}
if a.groups:
    for s, model in phones:
        print(f'… gruppi di core: {model}', flush=True)
        groups[model] = phone_groups(s)
    print('… gruppi di core: PC', flush=True)
    groups['PC'] = pc_groups()

# ------------------------------------------------------------------ stampa

f1 = lambda v, fmt='{:.1f}': fmt.format(v) if isinstance(v, (int, float)) else '–'
print('\nDISPOSITIVI')
print(f'{"nodo":<34}{"RAM/libera MB":>15}{"ping ms":>9}{"rtt min/med/p90 ms":>21}{"trasm. MB/s":>12}{"banda GB/s":>11}{"GFLOPS":>8}')
for r in results:
    rt = r.get('rtt')
    rts = f'{rt[0]:.1f}/{rt[1]:.1f}/{rt[2]:.1f}' if rt else r.get('rtt_err', '–')[:18]
    print(f'{r["nome"][:33]:<34}{f1(r["ram"], "{}") + "/" + f1(r["libera"], "{}"):>15}{f1(r["ping"]):>9}{rts:>21}'
          f'{f1(r.get("upload")):>12}{f1(r.get("membw")):>11}{f1(r.get("gflops")):>8}')
for r in results:
    errs = {k: v for k, v in r.items() if k.endswith('_err')}
    if errs:
        print(f'  {r["nome"]}: {errs}')
for dev, gl in groups.items():
    print(f'\nGRUPPI DI CORE · {dev}')
    for g in gl:
        print(f'  {g["gruppo"]:<44} banda {f1(g.get("membw")):>6} GB/s · calcolo {f1(g.get("gflops")):>6} GFLOPS {g.get("err", "")}')

if a.model:
    sys.path.insert(0, os.path.join(HERE, '..', 'native', 'llama.cpp', 'gguf-py'))
    from gguf import GGUFReader
    rd = GGUFReader(a.model)
    L = next(f.contents() for k, f in rd.fields.items() if k.endswith('.block_count'))
    layer_b = sum(int(t.n_bytes) for t in rd.tensors if t.name.startswith('blk.')) / L
    print(f'\nFATTORE LIMITANTE · {os.path.basename(a.model)} ({L} layer da {layer_b / 2 ** 20:.0f} MB)')
    print(f'{"nodo":<34}{"ms/layer":>9}{"rete ms/token":>14}{"carico s/layer":>15}{"layer max":>10}  limite')
    for r in results:
        if not r.get('membw'):
            continue
        ms_layer = layer_b / 1e9 / r['membw'] * 1000
        net = (r['rtt'][1] if r.get('rtt') else 0) + HOP_MS
        load = layer_b / 1e6 / r['upload'] if r.get('upload') else None
        frac = 0.5 if str(r.get('variante', '')).startswith('v8') else 1.0  # telefoni worker: zram (SplitPlan)
        cap = int(max(0, (r['libera'] or 0) * frac - 600) / (layer_b / 2 ** 20))
        share = max(1, min(cap, L // 2))  # la sua parte: i layer che può tenere, al massimo metà modello
        limit = 'RAM (pochi layer)' if cap < max(1, L // 8) else ('rete' if net > share * ms_layer else 'calcolo / banda RAM')
        print(f'{r["nome"][:33]:<34}{ms_layer:>9.1f}{net:>14.1f}{f1(load, "{:.2f}"):>15}{cap:>10}  {limit}')
    print('rete = round-trip mediano + costo per salto; vince "rete" se costa più dei layer che il nodo può calcolare.')

if a.json:
    json.dump({'nodi': results, 'gruppi': groups}, open(a.json, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('salvato', a.json)
