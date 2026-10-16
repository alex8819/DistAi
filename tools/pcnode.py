# uso: pcnode.py [--threads 4] [--mask f] [--gpu]
# Il PC come nodo DistAI, visto dall'app come un telefono qualsiasi:
# - worker x86 (native/build-pc/bin/ggml-rpc-server) su 0.0.0.0:50052 con threadpool fissato sui core fisici;
# - /status su :50053 con lo stesso JSON di StatusServer.kt (RAM, CPU, ruoli);
# - annuncio mDNS "_distai._tcp" (avahi-publish-service) con gli stessi TXT dei telefoni;
# - per ogni telefono collegato via USB: `adb reverse tcp:50072 tcp:50052`, così nell'app si aggiunge il nodo
#   manuale 127.0.0.1:50072 e il PC lavora via cavo senza toccare il firewall.
# - con --gpu il PC diventa DUE nodi: oltre a quello CPU, un worker sulla GPU su :50055 con /status su :50056 e un
#   proprio annuncio mDNS. L'app li vede come due dispositivi e li pesa col suo benchmark (il più veloce prende più layer).
#   Il nodo GPU non fa da coordinatore: il coordinatore gira sempre sulla CPU e usa la GPU come nodo remoto.
#   Con una GPU integrata la memoria è la stessa RAM: la memoria libera viene divisa tra i due nodi, non contata due volte.
# - coordinatore su richiesta dell'app (POST /coord/start, /coord/stop): llama-server x86 su :8090 (la 8080 è occupata),
#   con i modelli in --models (scaricati dal telefono se mancano). Per ogni telefono collegato via USB sceglie tra
#   cavo e Wi-Fi con una sonda HELLO v8 vera; se il coordinatore smette di rispondere ripete la scelta e riparte
#   (i worker con -c hanno i pesi in cache: ricevono solo gli hash). Via USB l'API è su 127.0.0.1:50074 del telefono.
# Multipiattaforma (Linux, Windows, macOS): binari da native/build-pc.py; annuncio mDNS con `pip install zeroconf`
# (senza: avahi-publish-service su Linux, dns-sd su macOS); adb dal PATH o dall'Android SDK.
# Dal Wi-Fi il PC è raggiungibile solo se il firewall lo permette:
#   Linux:   sudo ufw allow from 192.168.1.0/24 to any port 50052:50053 proto tcp comment distai
#            sudo ufw allow from 192.168.1.0/24 to any port 8090 proto tcp comment distai
#   Windows (PowerShell da amministratore):
#            New-NetFirewallRule -DisplayName DistAI -Direction Inbound -Protocol TCP -LocalPort 50052,50053,8090 -RemoteAddress LocalSubnet -Action Allow
import argparse, ctypes, json, os, platform, re, shutil, socket, struct, subprocess, sys, tempfile, threading, time
import urllib.parse, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OS = platform.system()  # 'Linux', 'Windows', 'Darwin'
# 'x86' come prima (l'app preferisce i PC x86 come coordinatore); 'arm64' per Raspberry Pi e Mac Apple Silicon
ARCH = 'x86' if platform.machine().lower() in ('x86_64', 'amd64', 'i686', 'x86') else 'arm64'
LOG = os.path.join(tempfile.gettempdir(), 'distai-pc-srv.log')
ap = argparse.ArgumentParser()
ap.add_argument('--threads', type=int, default=4)
ap.add_argument('--mask', help='core su cui fissare i thread (esadecimale); default: un thread per core fisico')
ap.add_argument('--models', default=os.path.expanduser('~/distai-models'), help='cartella dei modelli GGUF')
ap.add_argument('--api-port', type=int, default=8090)
ap.add_argument('--gpu', action='store_true',
                help='aggiunge un secondo nodo sulla GPU (:50055, binari di native/build-pc.py --gpu); solo GPU dedicate')
ap.add_argument('--gpu-integrated', action='store_true',
                help='crea il nodo GPU anche con una GPU integrata (di solito peggiora: stessa RAM, più lenta della CPU)')
a = ap.parse_args()
os.makedirs(a.models, exist_ok=True)
log = lambda *m: print(time.strftime('%H:%M:%S'), *m, flush=True)


def meminfo():
    """(RAM totale, RAM disponibile) in MB."""
    if OS == 'Windows':
        class MS(ctypes.Structure):
            _fields_ = [('len', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong),
                        ('avail', ctypes.c_ulonglong)] + [(f'x{i}', ctypes.c_ulonglong) for i in range(5)]
        m = MS(); m.len = ctypes.sizeof(MS)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
        return m.total >> 20, m.avail >> 20
    if OS == 'Darwin':
        total = int(subprocess.run(['sysctl', '-n', 'hw.memsize'], capture_output=True, text=True).stdout)
        vm = subprocess.run(['vm_stat'], capture_output=True, text=True).stdout
        page = int(re.search(r'page size of (\d+)', vm).group(1))
        pages = sum(int(n) for _, n in re.findall(r'Pages (free|inactive|speculative|purgeable):\s+(\d+)', vm))
        return total >> 20, pages * page >> 20
    m = {l.split(':')[0]: int(l.split()[1]) for l in open('/proc/meminfo')}
    return m['MemTotal'] // 1024, m['MemAvailable'] // 1024


def cpu_name():
    name = ''
    try:
        if OS == 'Windows':
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'HARDWARE\DESCRIPTION\System\CentralProcessor\0') as k:
                name = winreg.QueryValueEx(k, 'ProcessorNameString')[0]
        elif OS == 'Darwin':
            name = subprocess.run(['sysctl', '-n', 'machdep.cpu.brand_string'], capture_output=True, text=True).stdout
        else:
            name = next((l.split(':', 1)[1] for l in open('/proc/cpuinfo') if l.startswith('model name')), '')
            if not name.strip() and os.path.exists('/proc/device-tree/model'):  # ARM (Raspberry Pi): nome della scheda
                name = re.sub(r' Model \w+.*| Rev .*', '', open('/proc/device-tree/model').read().strip('\0\n '))
    except OSError:
        pass
    name = (name.strip() or platform.processor()).replace('Intel(R) Core(TM) ', '').replace(' CPU', '')
    return re.sub(r'\s+@.*', '', name)


def temp_c():
    try:  # solo Linux: su Windows e macOS servono driver o privilegi di amministratore
        return int(open('/sys/class/thermal/thermal_zone0/temp').read()) / 1000
    except OSError:
        return -1


def core_mask(n):
    """Maschera esadecimale di n CPU logiche su core fisici diversi (un thread per core).
    Con l'hyper-threading Windows numera i due thread di un core uno accanto all'altro (0-1 = core 0): la vecchia
    maschera 'f' prendeva 2 core con 2 thread ciascuno, 114 GFLOPS invece di 208 su i7-7700 (devbench, 12/10/2026)."""
    cpus = []
    try:
        if OS == 'Windows':
            out = subprocess.run(['powershell', '-NoProfile', '-Command',
                                  '(Get-CimInstance Win32_Processor | Measure-Object NumberOfCores -Sum).Sum'],
                                 capture_output=True, text=True, timeout=20).stdout
            cores, logical = int(out.strip()), os.cpu_count()
            step = logical // cores if cores and logical % cores == 0 else 1
            cpus = list(range(0, logical, step))
        elif OS == 'Linux':
            seen = set()
            for c in sorted(int(d[3:]) for d in os.listdir('/sys/devices/system/cpu') if re.fullmatch(r'cpu\d+', d)):
                sib = open(f'/sys/devices/system/cpu/cpu{c}/topology/thread_siblings_list').read().strip()
                if sib not in seen:
                    seen.add(sib); cpus.append(c)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    cpus = (cpus or list(range(os.cpu_count() or n)))[:n]
    return format(sum(1 << c for c in cpus), 'x')


def binary(name, gpu=False):
    """Eseguibile llama.cpp compilato da native/build-pc.py [--gpu] (o dai vecchi build-x86*.sh su Linux)."""
    exe = name + ('.exe' if OS == 'Windows' else '')
    for d in ('build-pc-gpu',) if gpu else ('build-pc', 'build-x86c', 'build-x86'):
        p = os.path.join(ROOT, 'native', d, 'bin', exe)
        if os.path.exists(p):
            return p
    sys.exit(f'{exe} non trovato: compilalo con  python native/build-pc.py' + (' --gpu' if gpu else ''))


def find_adb():
    sdks = [os.environ.get('ANDROID_HOME'), os.environ.get('ANDROID_SDK_ROOT'),
            os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Android', 'Sdk'),
            os.path.expanduser('~/Library/Android/sdk'), os.path.expanduser('~/Android/Sdk')]
    for d in filter(None, sdks):
        p = os.path.join(d, 'platform-tools', 'adb' + ('.exe' if OS == 'Windows' else ''))
        if os.path.exists(p):
            return p
    return shutil.which('adb')


ADB = find_adb()


def adb(*args, **kw):
    """adb con l'output catturato; senza adb installato restituisce un fallimento invece di un'eccezione."""
    if not ADB:
        return subprocess.CompletedProcess(args, 1, '', '')
    return subprocess.run([ADB, *args], capture_output=True, **kw)


GPU_RPC_PORT, GPU_STATUS_PORT = 50055, 50056
# GPU che condividono la RAM di sistema (integrate): Intel HD/UHD/Iris/Graphics, Radeon integrate, Apple Silicon
SHARED_GPU = r'Intel.*(HD|UHD|Iris)|Intel\(R\) Graphics|Radeon\(TM\) Graphics|Radeon Graphics|Vega \d+ Graphics|Apple'


def gpu_info():
    """(nome, MB totali, MB liberi, memoria condivisa) del primo dispositivo GPU visto da llama.cpp, None se manca."""
    out = subprocess.run([binary('llama-server', True), '--list-devices'], capture_output=True, text=True, timeout=120)
    m = re.search(r'^\s*\w+\d+: (.+?) \((\d+) MiB, (\d+) MiB free\)', out.stdout + out.stderr, re.M)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3)), bool(re.search(SHARED_GPU, m.group(1)))


DEV = f'PC {socket.gethostname()} ({cpu_name()})'
RPC_SERVER, LLAMA_SERVER = binary('ggml-rpc-server'), binary('llama-server')
GPU = None
if a.gpu:
    GPU = gpu_info() or sys.exit('nessuna GPU vista da llama.cpp (driver Vulkan installato?)')
    if GPU[3] and not a.gpu_integrated:
        # misurato il 12/10/2026 (devbench): HD 630 9,7 GB/s contro 17,6 della CPU, e la RAM è la stessa: un nodo GPU
        # separato toglie metà RAM al nodo CPU e i suoi layer costano il doppio. Tutta la RAM resta al nodo CPU.
        print(f'{GPU[0]}: GPU integrata, niente nodo GPU separato (stessa RAM della CPU e più lenta);'
              f' forzalo con --gpu-integrated', flush=True)
        GPU = None
if GPU:
    gpu_short = ' '.join(re.sub(r'Intel|\(R\)|\(TM\)|Graphics', '', GPU[0]).split())
    GPU_DEV = f'PC {socket.gethostname()} ({gpu_short})'
    RPC_SERVER_GPU = binary('ggml-rpc-server', True)
procs = {}  # 'cpu' / 'gpu' -> processo del worker
clients = {'cpu': 0, 'gpu': 0}  # coordinatori collegati a ciascun worker (dal suo output)
_cpu_last = {}  # kind -> (secondi di CPU, istante) dell'ultimo campione


def running(kind):
    return procs.get(kind) is not None and procs[kind].poll() is None


def proc_usage(p):
    """(MB residenti, secondi di CPU) di un processo; (0, 0) se non leggibile."""
    try:
        if OS == 'Windows':
            from ctypes import wintypes
            class PMC(ctypes.Structure):
                _fields_ = [('cb', wintypes.DWORD), ('faults', wintypes.DWORD)] + \
                           [(f, ctypes.c_size_t) for f in ('peak_ws', 'ws', 'qpp', 'qp', 'qnpp', 'qnp', 'pf', 'peak_pf')]
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, p.pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            try:
                m = PMC(); m.cb = ctypes.sizeof(PMC)
                ctypes.windll.psapi.GetProcessMemoryInfo(h, ctypes.byref(m), m.cb)
                t = [wintypes.FILETIME() for _ in range(4)]
                k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t])
                secs = sum((x.dwHighDateTime << 32 | x.dwLowDateTime) for x in t[2:]) / 1e7
                return m.ws >> 20, secs
            finally:
                k32.CloseHandle(h)
        if OS == 'Linux':
            rss = int(open(f'/proc/{p.pid}/statm').read().split()[1]) * os.sysconf('SC_PAGE_SIZE')
            f = open(f'/proc/{p.pid}/stat').read().rsplit(')', 1)[1].split()
            return rss >> 20, (int(f[11]) + int(f[12])) / os.sysconf('SC_CLK_TCK')
    except (OSError, ValueError, IndexError, AttributeError):
        pass
    return 0, 0.0


def worker_activity(kind):
    """Campi /status dell'attività del worker: come WorkerStats.kt nell'app."""
    p = procs.get(kind)
    if not running(kind):
        return {'workerClients': 0, 'workerMb': 0, 'workerCpu': 0}
    mb, secs = proc_usage(p)
    now, (last, at) = time.time(), _cpu_last.get(kind, (secs, time.time()))
    _cpu_last[kind] = (secs, now)
    cpu = int((secs - last) / max(0.5, now - at) / max(1, a.threads) * 100) if now > at else 0
    return {'workerClients': clients[kind], 'workerMb': mb, 'workerCpu': max(0, min(100, cpu))}


BENCH = {'cpu': {}, 'gpu': {}}  # bench dei worker di questo PC all'avvio (come SelfBench.kt nell'app)


def self_bench():
    """Misura i worker di questo PC appena partiti: banda (generazione) e GFLOPS (prompt), pubblicati in /status."""
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import rpcbench
    time.sleep(4)
    for kind, port in (('cpu', 50052),) + ((('gpu', GPU_RPC_PORT),) if GPU else ()):
        if clients[kind]:
            continue  # un coordinatore lo sta già usando
        try:
            BENCH[kind] = {'gbps': round(rpcbench.bench(f'127.0.0.1:{port}')[2], 1),
                           'gflops': round(rpcbench.compute_gflops(f'127.0.0.1:{port}'), 1), 'at': int(time.time() * 1000)}
            log(f"bench {kind}: {BENCH[kind]['gbps']} GB/s · {BENCH[kind]['gflops']} GFLOPS")
        except Exception as e:
            log(f'bench {kind}: {e}')


def tok_s():
    """tok/s medi del coordinatore dalle metriche di llama-server (--metrics); 0 se non disponibili."""
    try:
        for line in urllib.request.urlopen(f'http://127.0.0.1:{a.api_port}/metrics', timeout=1).read().decode().splitlines():
            if line.startswith('llamacpp:predicted_tokens_seconds '):
                return round(float(line.split()[1]), 2)
    except Exception:
        pass
    return 0.0


def mem_split():
    """MB liberi per il nodo CPU e per il nodo GPU. Una GPU integrata usa la RAM di sistema: le do al massimo metà
    della RAM libera e la tolgo al nodo CPU, così il piano dell'app non conta due volte la stessa memoria."""
    total, free = meminfo()
    if not GPU:
        return free, 0
    _, _, gfree, shared = GPU
    if shared:
        gfree = min(gfree, free // 2)
        return free - gfree, gfree
    return free, gfree


def status():
    total, _ = meminfo()
    return {'dev': DEV, 'ram': total, 'free': mem_split()[0], 'cores': os.cpu_count(), 'bigCores': a.threads,
            'variant': ARCH, 'battery': -1, 'charging': True, 'tempC': temp_c(),
            'worker': running('cpu'),
            'coordinator': coord.proc is not None and coord.proc.poll() is None,
            'apiReady': coord.ready, 'coordState': coord.state,
            'canCoord': True, 'apiPort': a.api_port,
            'replica': False, 'replicaReady': False, 'replicaState': '',
            'models': sorted(f for f in os.listdir(a.models) if f.endswith('.gguf')),
            # l'app li offre senza copiarli sul telefono: legge l'intestazione da /gguf/<nome> e delega a questo PC
            'modelSizes': {f: os.path.getsize(os.path.join(a.models, f)) for f in os.listdir(a.models) if f.endswith('.gguf')},
            'lanIps': lan_ips(),
            'tokS': tok_s() if coord.ready else 0.0, **worker_activity('cpu'), 'bench': BENCH['cpu'],
            'usbPeers': [pr for pr in peers.values() if not pr['dev'].endswith('(USB del PC)')]}


def gpu_status():
    return {'dev': GPU_DEV, 'ram': GPU[1], 'free': mem_split()[1], 'cores': os.cpu_count(), 'bigCores': 1,
            'variant': 'metal' if OS == 'Darwin' else 'vulkan', 'gpu': GPU[0], 'shared': GPU[3],
            'battery': -1, 'charging': True, 'tempC': temp_c(),
            'worker': running('gpu'), 'coordinator': False, 'apiReady': False, 'coordState': '',
            'canCoord': False, 'replica': False, 'replicaReady': False, 'replicaState': '', 'models': [],
            **worker_activity('gpu'), 'bench': BENCH['gpu']}


# ------------------------------------------------------------------ canali verso i telefoni (USB e Wi-Fi)

def adb_devices():
    out = adb('devices', text=True).stdout.splitlines()[1:]
    return [l.split()[0] for l in out if l.endswith('device')]


def usb_ips():
    """IP Wi-Fi (e IP del tethering USB, se il telefono si annuncia con quello) -> seriale adb dei telefoni collegati via cavo."""
    m = {}
    for s in adb_devices():
        out = adb('-s', s, 'shell', 'ip -4 -o addr', text=True).stdout
        for ip in re.findall(r'^\d+:\s+(?:wlan|rndis|ncm|usb)\w*\s+inet (\d+\.\d+\.\d+\.\d+)', out, re.M):
            m[ip] = s
    return m


def tether_ip(serial):
    """IP del telefono sulla rete del cavo (tethering USB: rndis/ncm/usb), None se il tethering è spento.
    Misure sul Samsung (13/10/2026, adb 1.0.41): adb forward 237 MB/s PC → telefono e 35 indietro, tethering 30 e 40, latenza ~2 e ~1,6 ms;
    l'adb 1.0.32 di C:db faceva 3,9 MB/s (era lui il collo di bottiglia, non Windows)."""
    out = adb('-s', serial, 'shell', 'ip -4 -o addr', text=True).stdout
    ip = re.search(r'^\d+:\s+(?:rndis|ncm|usb)\w*\s+inet (\d+\.\d+\.\d+\.\d+)', out, re.M)
    return ip.group(1) if ip else None


def hello_ms(host, port, timeout=3):
    """Sonda vera del worker: HELLO v8 + un round-trip DEVICE_COUNT. None se il canale non funziona."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as c:
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            c.settimeout(timeout)
            def call(cmd, payload):
                c.sendall(struct.pack('<BQ', cmd, len(payload)) + payload)
                n, = struct.unpack('<Q', recvn(c, 8))
                return recvn(c, n)
            if call(14, bytes(24))[0] != 8:
                return None
            best = None
            for _ in range(3):
                t = time.perf_counter(); call(15, b'')
                ms = (time.perf_counter() - t) * 1000
                best = ms if best is None else min(best, ms)
            return best
    except (OSError, EOFError, struct.error):
        return None


def recvn(c, n):
    b = b''
    while len(b) < n:
        k = c.recv(n - len(b))
        if not k:
            raise EOFError
        b += k
    return b


def choose(endpoints):
    """Per ogni worker sceglie il canale più rapido tra Wi-Fi e (se il telefono è sul cavo) tethering USB o adb.
    Vince il round-trip minore: con un adb aggiornato la banda è simile (adb anzi è più veloce verso il telefono)."""
    usb = usb_ips()
    out = []
    for i, ep in enumerate(endpoints):
        host, port = ep.rsplit(':', 1)
        teth = tether_ip(usb[host]) if host in usb else None
        paths = [('tethering' if host == teth else 'wifi', host, int(port))]
        if host in usb:
            if teth and teth != host:
                paths.append(('tethering', teth, int(port)))
            local = 50061 + i
            if adb('-s', usb[host], 'forward', f'tcp:{local}', f'tcp:{port}').returncode == 0:
                paths.append(('usb', '127.0.0.1', local))
        best = None
        for kind, h, p in paths:
            ms = hello_ms(h, p)
            log(f'  {ep} via {kind}: ' + (f'{ms:.1f} ms' if ms is not None else 'non raggiungibile'))
            if ms is not None and (best is None or ms < best[0]):
                best = (ms, f'{h}:{p}')
        if best is None:
            raise RuntimeError(f'worker {ep} non raggiungibile')
        out.append(best[1])
    return out


def via_usb(url):
    """Se la sorgente è un telefono sul cavo, scarico via cavo: tethering USB se acceso (40 MB/s), altrimenti adb forward (35 con adb aggiornato)."""
    u = urllib.parse.urlparse(url)
    s = usb_ips().get(u.hostname or '')
    if not s:
        return url
    teth = tether_ip(s)
    if teth:
        return u._replace(netloc=f'{teth}:{u.port or 50053}').geturl()
    adb('-s', s, 'forward', 'tcp:50083', f'tcp:{u.port or 50053}')
    return u._replace(netloc='127.0.0.1:50083').geturl()


# ------------------------------------------------------------------ coordinatore

def gguf_arch(path):
    """Architettura del modello (general.architecture) letta dall'intestazione GGUF, in Python puro; None se non la trova."""
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    try:
        with open(path, 'rb') as f:
            if f.read(4) != b'GGUF':
                return None
            f.read(4)  # versione
            _tensors, kvs = struct.unpack('<QQ', f.read(16))
            rd = lambda fmt: struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]

            def string():
                return f.read(rd('<Q')).decode('utf-8', 'replace')

            def skip(t):
                if t == 8:
                    f.seek(rd('<Q'), 1)
                elif t == 9:
                    et, n = rd('<I'), rd('<Q')
                    for _ in range(n):
                        skip(et)
                else:
                    f.seek(sizes[t], 1)
            for _ in range(min(kvs, 64)):
                key, t = string(), rd('<I')
                if key == 'general.architecture' and t == 8:
                    return string()
                skip(t)
    except Exception as e:
        log(f'architettura non letta: {e}')
    return None


class Coord:
    def __init__(self):
        self.proc = None
        self.ready = False
        self.state = 'fermo'
        self.req = None
        self.gen = 0
        self.lock = threading.Lock()

    def model(self, name, source):
        name = os.path.basename(name)
        assert name.endswith('.gguf'), 'modello non valido'
        path = os.path.join(a.models, name)
        if os.path.exists(path):
            return path
        assert source, f'{name} assente e nessuna sorgente'
        url = via_usb(source)
        self.state = f'download {name}'
        log(f'scarico {name} da {url}')
        with urllib.request.urlopen(url, timeout=30) as r, open(path + '.part', 'wb') as f:
            total, done = int(r.headers.get('Content-Length') or 0), 0
            while chunk := r.read(1 << 20):
                f.write(chunk); done += len(chunk)
                self.state = f'download {name} {done * 100 // total if total else done >> 20}{"%" if total else " MB"}'
        os.replace(path + '.part', path)
        return path

    def start(self, req):
        with self.lock:
            self.stop_locked()
            self.req = req
            self.gen += 1
            threading.Thread(target=self.run, args=(self.gen,), daemon=True).start()

    def stop(self):
        with self.lock:
            self.stop_locked()

    def stop_locked(self):
        self.gen += 1
        self.req = None
        self.kill()
        self.state = 'fermo'

    def kill(self):
        p, self.proc, self.ready = self.proc, None, False
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                p.kill()

    def health(self):
        try:
            return urllib.request.urlopen(f'http://127.0.0.1:{a.api_port}/health', timeout=3).status == 200
        except Exception:
            return False

    def run(self, gen):
        r = self.req
        try:
            model = self.model(r['model'], r.get('source', ''))
            draft = self.model(r['draft'], r.get('draftSource', '')) if r.get('draft') else None
        except Exception as e:
            self.state = f'errore: {e}'; log(self.state); return
        workers = [e for e in r.get('rpc', '').split(',') if e]
        while gen == self.gen:
            try:
                self.state = 'scelta dei canali'
                rpc = choose(workers)
            except Exception as e:
                self.state = f'errore: {e}'; log(self.state); time.sleep(10); continue
            # sempre il binario CPU: la GPU di questo PC, se c'è, arriva come nodo remoto (:50055) nel piano dell'app
            cmd = [LLAMA_SERVER, '-m', model,
                   '-c', str(r.get('ctx', 4096)), '-np', '1', '-t', str(a.threads),
                   '--host', '0.0.0.0', '--port', str(a.api_port), '--cors-origins', '*', '--metrics']
            if rpc:
                # come sul telefono: output e embedding restano qui, in rete viaggia solo lo stato nascosto
                cmd += ['--rpc', ','.join(rpc), '-ngl', str(int(r.get('ngl', 0)) + 1),
                        '-ot', '^(output|output_norm|token_embd)[.]weight$=CPU', '--poll', '100']
                if r.get('ts'):
                    cmd += ['-ts', r['ts']]
            if r.get('experts'):
                # MoE: meno esperti attivi per token = più tok/s. Qwen3-Coder-30B-A3B: 6 su 8 stessa qualità sui nostri 22 compiti, +12-25%;
                # 5 su 8 perde 2 compiti, 4 su 8 rompe il codice (ROADMAP, 13/10/2026). Opzione scelta dall'utente all'avvio, mai nascosta.
                arch = gguf_arch(model)
                if arch:
                    cmd += ['--override-kv', f"{arch}.expert_used_count=int:{int(r['experts'])}"]
                    log(f"esperti attivi per token: {int(r['experts'])} ({arch})")
            if draft:
                cmd += ['--spec-type', 'draft-simple', '-md', draft, '-devd', 'none', '-ngld', '0', '--spec-draft-n-max', str(r.get('draftMax', 4)), '--spec-draft-n-min', '1']
            log('avvio coordinatore: ' + ' '.join(cmd[1:]))
            self.state = 'caricamento'
            with self.lock:
                if gen != self.gen:
                    return
                self.proc = subprocess.Popen(cmd, stdout=open(LOG, 'w'), stderr=subprocess.STDOUT)
            t0 = time.time()
            while gen == self.gen and self.proc and self.proc.poll() is None and not self.health():
                time.sleep(1)
            if gen != self.gen:
                return
            if self.proc and self.proc.poll() is None:
                self.ready = True
                self.state = f'pronto ({os.path.basename(model)})'
                log(f'coordinatore pronto in {time.time() - t0:.0f} s')
                fails = 0
                while gen == self.gen and fails < 3:  # 3 controlli falliti di fila = canale perso
                    time.sleep(2)
                    fails = 0 if self.health() else fails + 1
                if gen != self.gen:
                    return
                log('il coordinatore non risponde: rifaccio la scelta dei canali')
            else:
                log(f'il coordinatore si è chiuso durante il caricamento (log in {LOG})')
            self.kill()
            time.sleep(2)


coord = Coord()


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith('/gguf/'):
            return self.gguf_header()
        if self.path != '/status':
            self.send_error(404); return
        self.reply(self.status())

    def gguf_header(self):
        """Primi `bytes` byte di un modello (default 32 MB: intestazione e descrittori dei tensori) per il piano dell'app."""
        u = urllib.parse.urlparse(self.path)
        name = os.path.basename(urllib.parse.unquote(u.path[len('/gguf/'):]))
        path = os.path.join(a.models, name)
        if not name.endswith('.gguf') or not os.path.isfile(path):
            self.send_error(404); return
        size = os.path.getsize(path)
        n = min(int((urllib.parse.parse_qs(u.query).get('bytes') or ['33554432'])[0]), size, 256 << 20)
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(n)); self.send_header('X-File-Size', str(size))
        self.end_headers()
        with open(path, 'rb') as f:
            while n > 0:
                chunk = f.read(min(n, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk); n -= len(chunk)

    def status(self):
        return status()

    def do_POST(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = json.loads(self.rfile.read(n) or b'{}')
        if self.path == '/coord/start':
            log(f'coordinatore richiesto da {self.client_address[0]}: {body.get("model")}')
            coord.start(body); self.reply({'ok': True}, 202)
        elif self.path == '/coord/stop':
            coord.stop(); self.reply({'ok': True})
        else:
            self.send_error(404)

    def reply(self, j, code=200):
        body = json.dumps(j).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json'); self.send_header('Content-Length', str(len(body)))
        self.end_headers(); self.wfile.write(body)

    def log_message(self, *args):
        pass


class HG(H):
    """/status del nodo GPU: solo stato, niente coordinatore."""
    def status(self):
        return gpu_status()

    def do_POST(self):
        self.send_error(404)


def worker(kind, exe, port, env=None):
    while True:  # se il worker cade (es. coordinatore chiuso male) lo riavvio
        p = procs[kind] = subprocess.Popen([exe, '-H', '0.0.0.0', '-p', str(port), '-t', str(a.threads), '-c'],
                                           env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, errors='replace')
        for line in p.stdout:  # "Accepted client connection" / "Client connection closed" (ggml-rpc.cpp)
            if line.startswith('Accepted client'):
                clients[kind] += 1
            elif line.startswith('Client connection closed'):
                clients[kind] = max(0, clients[kind] - 1)
        p.wait(); clients[kind] = 0; time.sleep(2)


def lan_ip():
    """IP della scheda verso la LAN (connect UDP sceglie solo la route, non invia pacchetti)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as u:
        u.connect(('192.168.255.255', 1))
        return u.getsockname()[0]


def lan_ips():
    """Tutti gli IPv4 del PC (rete di casa, tethering USB dei telefoni, adattatori): l'app prova quelli sulla rete del suo cavo."""
    ips = set()
    try:
        ips.update(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass
    if OS != 'Windows':
        try:
            out = subprocess.run(['ip', '-4', '-o', 'addr'], capture_output=True, text=True, timeout=3).stdout
            ips.update(re.findall(r'inet (\d+\.\d+\.\d+\.\d+)', out))
        except (OSError, subprocess.SubprocessError):
            pass
    return sorted(ip for ip in ips if not ip.startswith('127.'))


ZC = None  # Zeroconf attivo: serve anche al ponte USB per annunciare i telefoni sul cavo


def register(dev, port, txt):
    """Annuncia un nodo DistAI in mDNS (TXT come Discovery.kt). False se non c'è modo di farlo."""
    global ZC
    name = f'DistAI {dev}'[:60]
    txt = {'dev': dev[:60], **{k: str(v) for k, v in txt.items()}}
    try:
        from zeroconf import ServiceInfo, Zeroconf
        ip = lan_ip()
        ZC = ZC or Zeroconf(interfaces=[ip])
        ZC.register_service(ServiceInfo('_distai._tcp.local.', f'{name}._distai._tcp.local.', port=port,
                                        properties=txt, addresses=[socket.inet_aton(ip)],
                                        server=f'{socket.gethostname()}.local.'))
        return True
    except ImportError:
        pass
    tool = {'Linux': ['avahi-publish-service', name, '_distai._tcp', str(port)],
            'Darwin': ['dns-sd', '-R', name, '_distai._tcp', 'local', str(port)]}.get(OS)
    if not (tool and shutil.which(tool[0])):
        return False
    subprocess.Popen(tool + [f'{k}={v}' for k, v in txt.items()], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return True


def announce():
    """Annuncio mDNS dei nodi del PC (CPU e, con --gpu, GPU)."""
    total, _ = meminfo()
    cpu_free, gpu_free = mem_split()
    ok = register(DEV, 50052, {'ram': total, 'free': cpu_free, 'cpu': f'{os.cpu_count()}/{ARCH}'})
    if GPU and ok:
        register(GPU_DEV, GPU_RPC_PORT, {'ram': GPU[1], 'free': gpu_free, 'cpu': f'{os.cpu_count()}/gpu'})
    return ok


# ------------------------------------------------------------------ ponte USB
# Un telefono collegato via cavo ma su un'altra rete Wi-Fi (o senza Wi-Fi) lo vede solo il PC, con adb forward,
# che ascolta solo su 127.0.0.1. Il ponte lo espone sulla rete del PC: relay TCP 0.0.0.0:porta -> adb forward,
# più un annuncio mDNS "<telefono> (USB del PC)". Così lo vedono e lo usano anche gli altri telefoni.
BRIDGE_PORT = 50080  # RPC su 50080 + 2*i, /status sulla successiva (Node.statusPort nell'app)
FWD_PORT = 50090     # adb forward locali verso il telefono i-esimo (RPC e status)
bridge_slots = {}    # seriale -> indice
bridged = set()      # seriali già esposti


def relay(listen_port, target_port, target_host='127.0.0.1', bind='0.0.0.0'):
    """Relay TCP bind:listen_port -> target_host:target_port, con TCP_NODELAY (il protocollo RPC è a botta e risposta)."""
    srv = socket.create_server((bind, listen_port))

    def pipe(src, dst):
        try:
            while b := src.recv(1 << 16):
                dst.sendall(b)
        except OSError:
            pass
        for x in (src, dst):
            try:
                x.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    def serve():
        while True:
            c, _ = srv.accept()
            try:
                d = socket.create_connection((target_host, target_port), timeout=5)
            except OSError:
                c.close(); continue
            d.settimeout(None)
            for x in (c, d):
                x.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            threading.Thread(target=pipe, args=(c, d), daemon=True).start()
            threading.Thread(target=pipe, args=(d, c), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()


# Ponte inverso: un telefono isolato (sul cavo ma su un'altra Wi-Fi) non raggiunge gli altri nodi della rete.
# Per ognuno dei nodi trovati in mDNS il PC apre un relay locale (127.0.0.1:50120+2k -> nodo) e con adb reverse
# lo porta nel telefono su 127.0.0.1:50100+2k; l'elenco va in /status come usbPeers e l'app lo aggiunge da sola.
PEER_PHONE_PORT, PEER_RELAY_PORT = 50100, 50120
peers = {}       # 'ip:porta' del nodo -> {'dev', 'port' (sul telefono), 'lan'}
peer_slots = {}  # 'ip:porta' -> indice


def browse_peers():
    """Segue in mDNS i nodi DistAI della rete e prepara un relay per ciascuno (escluso il nodo CPU di questo PC)."""
    try:
        from zeroconf import ServiceBrowser, Zeroconf
    except ImportError:
        return

    class L:
        def add_service(self, zc, t, n):
            i = zc.get_service_info(t, n, 3000)
            if not i or not i.parsed_addresses():
                return
            ip, port = i.parsed_addresses()[0], i.port
            if (ip == lan_ip() and port == 50052) or ':' in ip:
                return
            dev = (i.properties.get(b'dev') or n.split('._')[0].encode()).decode(errors='replace')
            key = f'{ip}:{port}'
            if key in peers:
                return
            k = peer_slots.setdefault(key, len(peer_slots))
            relay(PEER_RELAY_PORT + 2 * k, port, ip, bind='127.0.0.1')
            relay(PEER_RELAY_PORT + 2 * k + 1, port + 1 if port != 50052 else 50053, ip, bind='127.0.0.1')
            peers[key] = {'dev': dev, 'port': PEER_PHONE_PORT + 2 * k, 'lan': key}

        def update_service(self, *a):
            pass

        def remove_service(self, zc, t, n):
            pass
    ServiceBrowser(ZC or Zeroconf(interfaces=[lan_ip()]), '_distai._tcp.local.', L())


def reverse_peers(serial):
    """Porta nel telefono isolato tutti i nodi della rete (adb reverse verso i relay locali)."""
    for key, pr in list(peers.items()):
        k = peer_slots[key]
        adb('-s', serial, 'reverse', f"tcp:{pr['port']}", f'tcp:{PEER_RELAY_PORT + 2 * k}')
        adb('-s', serial, 'reverse', f"tcp:{pr['port'] + 1}", f'tcp:{PEER_RELAY_PORT + 2 * k + 1}')


def lan_status_ok(ip):
    try:
        return urllib.request.urlopen(f"http://{ip}:50053/status", timeout=3).status == 200
    except Exception:
        return False


def usb_bridge():
    """Cerca via adb i telefoni con DistAI attivo; quelli che la rete del PC non raggiunge li espone col ponte."""
    tips = set()
    while True:
        phones = []
        for s in adb_devices():
            i = bridge_slots.setdefault(s, len(bridge_slots))
            fwd = FWD_PORT + 2 * i
            if adb('-s', s, 'forward', f'tcp:{fwd}', 'tcp:50052').returncode or \
                    adb('-s', s, 'forward', f'tcp:{fwd + 1}', 'tcp:50053').returncode:
                continue
            try:
                st = json.load(urllib.request.urlopen(f'http://127.0.0.1:{fwd + 1}/status', timeout=3))
            except Exception:
                continue  # sul telefono l'app DistAI non è aperta
            out = adb('-s', s, 'shell', 'ip -4 -o addr show wlan0', text=True).stdout
            ip = (re.search(r'inet (\d+\.\d+\.\d+\.\d+)', out) or [None, ''])[1]
            # raggiungibile dalla rete del PC? Si prova /status: la porta RPC serve un client alla volta e, se un
            # coordinatore è collegato, una sonda HELLO scade anche con il telefono sulla stessa Wi-Fi
            on_lan = bool(ip) and ip.rsplit('.', 1)[0] == lan_ip().rsplit('.', 1)[0] and lan_status_ok(ip)
            phones.append((st.get('dev', s), on_lan))
            if not on_lan:  # ogni giro: i nodi della rete possono cambiare
                reverse_peers(s)
            if on_lan or s in bridged:
                continue
            pub = BRIDGE_PORT + 2 * i
            teth = tether_ip(s)  # tethering USB acceso: il ponte passa da lì (latenza un po' minore di adb forward)
            if teth:
                relay(pub, 50052, teth); relay(pub + 1, 50053, teth)
            else:
                relay(pub, fwd); relay(pub + 1, fwd + 1)
            bridged.add(s)
            dev = f"{st.get('dev', s)} (USB del PC)"
            register(dev, pub, {'ram': st.get('ram', 0), 'free': st.get('free', 0),
                                'cpu': f"{st.get('cores', 0)}/{st.get('variant', '')}"})
            log(f"{st.get('dev', s)}: Wi-Fi {ip or 'spento'}, non raggiungibile dalla rete del PC ({lan_ip()}):"
                f" ponte USB ({'tethering ' + teth if teth else 'adb forward'}) attivo su {lan_ip()}:{pub},"
                f" ora lo vedono anche gli altri telefoni")
        # suggerimenti, una volta per situazione
        for dev, on_lan in phones:
            tip = (f'{dev}: collegato solo via USB tramite il PC. Per meno latenza mettilo sulla stessa Wi-Fi del PC'
                   f' ({lan_ip().rsplit(".", 1)[0]}.x); se è l\'unico telefono del cluster il cavo va benissimo.'
                   if not on_lan else '')
            if len(phones) == 1:
                tip = f'{dev}: unico telefono collegato via USB: il cavo va benissimo così.'
            if tip and tip not in tips:
                tips.add(tip); log('suggerimento: ' + tip)
        time.sleep(10)


def usb_reverse():
    # il nodo GPU non passa dal cavo: un coordinatore delegato al PC escluderebbe i nodi visti via USB dal telefono
    # (Cluster.plan), e la GPU uscirebbe dal piano. Via Wi-Fi resta raggiungibile da tutti.
    seen = set()
    while True:
        for s in adb_devices():
            r = adb('-s', s, 'reverse', 'tcp:50072', 'tcp:50052')
            # /status sulla porta successiva (convenzione di Node.statusPort nell'app)
            adb('-s', s, 'reverse', 'tcp:50073', 'tcp:50053')
            # API del coordinatore sulla porta RPC + 2 (Node.apiPort nell'app)
            adb('-s', s, 'reverse', 'tcp:50074', f'tcp:{a.api_port}')
            if r.returncode == 0 and s not in seen:
                print(f'{s}: PC raggiungibile via USB come 127.0.0.1:50072', flush=True); seen.add(s)
        time.sleep(10)


threading.Thread(target=worker, args=('cpu', RPC_SERVER, 50052, dict(os.environ, GGML_RPC_CPUMASK=a.mask or core_mask(a.threads))),
                 daemon=True).start()
if GPU:
    threading.Thread(target=worker, args=('gpu', RPC_SERVER_GPU, GPU_RPC_PORT), daemon=True).start()
    threading.Thread(target=ThreadingHTTPServer(('0.0.0.0', GPU_STATUS_PORT), HG).serve_forever, daemon=True).start()
    print(f'{GPU_DEV}: worker GPU :{GPU_RPC_PORT}, stato :{GPU_STATUS_PORT}'
          f' ({GPU[1]} MB{", memoria condivisa con la CPU" if GPU[3] else ""})', flush=True)
if ADB:
    # l'adb 1.0.32 (2015) fa 3,9 MB/s sul cavo, il 1.0.41 dell'SDK 237 (Samsung, 13/10/2026)
    v = re.search(r'version (\d+)\.(\d+)\.(\d+)', adb('version', text=True).stdout or '')
    if v and tuple(map(int, v.groups())) < (1, 0, 41):
        print(f'attenzione: {ADB} è adb {".".join(v.groups())}, lento sul cavo (3,9 MB/s): installa le platform-tools aggiornate', flush=True)
    threading.Thread(target=usb_reverse, daemon=True).start()
else:
    print('adb non trovato: niente collegamento via USB, solo Wi-Fi', flush=True)
if announce():
    print(f'{DEV}: worker :50052, stato :50053, annuncio mDNS attivo', flush=True)
else:
    print(f'{DEV}: worker :50052, stato :50053; annuncio mDNS non disponibile (pip install zeroconf):'
          f' nell\'app aggiungi il PC a mano con l\'IP {lan_ip()}', flush=True)
threading.Thread(target=self_bench, daemon=True).start()
if ADB:  # dopo l'annuncio del PC, così usa lo stesso Zeroconf
    browse_peers()
    threading.Thread(target=usb_bridge, daemon=True).start()
ThreadingHTTPServer(('0.0.0.0', 50053), H).serve_forever()
