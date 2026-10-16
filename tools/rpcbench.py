# uso: rpcbench.py host:porta [host:porta ...]
# Mini-benchmark di un worker ggml-rpc (protocollo v8) in pochi secondi, senza modello:
# alloca sul worker 4 matrici Q4_0 4096x4096 (~38 MB), le riempie con MEMSET (niente dati in rete)
# e calcola una catena di 16 prodotti matrice-vettore, il lavoro di un token (~150 MB letti).
# Misura: round-trip (ms) e banda effettiva di calcolo (GB/s). Stessa logica di RpcBench.kt nell'app.
# Il worker serve un client alla volta: va lanciato quando nessun coordinatore è collegato.
import socket, struct, sys, time

HELLO, ALLOC, GET_BASE, FREE, GET_TENSOR, GRAPH_COMPUTE, MEMSET = 14, 0, 3, 4, 8, 10, 17
Q4_0, F32, OP_MUL_MAT = 2, 0, 29
K = 4096          # lato delle matrici
MATS = 4          # matrici distinte (più grandi della cache L3)
CHAIN = 16        # prodotti per grafo
QROW = K // 32 * 18
WBYTES = QROW * K
VBYTES = K * 4


def tensor(tid, typ, buf, ne, nb, op=0, src=(), data=0):
    flags = 16 if op else 0  # GGML_TENSOR_FLAG_COMPUTE: senza, la CPU salta il nodo
    ne = list(ne) + [1] * (4 - len(ne))
    nb = list(nb) + [nb[-1] * ne[len(nb) - 1]] * (4 - len(nb))
    s = list(src) + [0] * (10 - len(src))
    return struct.pack('<QIQ4I4II16ii10QQQQ64si', tid, typ, buf, *ne, *nb, op, *([0] * 16), flags, *s, 0, 0, data,
                       b'', 1)


class Rpc:
    def __init__(self, ep, timeout=10):
        h, p = ep.rsplit(':', 1)
        self.s = socket.create_connection((h, int(p)), timeout=timeout)
        self.s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        r = self.call(HELLO, bytes(24))
        assert r[0] == 8, f'protocollo {r[0]}.{r[1]}'

    def send(self, cmd, payload):
        self.s.sendall(struct.pack('<BQ', cmd, len(payload)) + payload)

    def recv(self, n):
        b = b''
        while len(b) < n:
            k = self.s.recv(n - len(b))
            if not k:
                raise EOFError
            b += k
        return b

    def call(self, cmd, payload):
        self.send(cmd, payload)
        size, = struct.unpack('<Q', self.recv(8))
        return self.recv(size)


def bench(ep, runs=8):
    r = Rpc(ep)
    rtts = []
    for _ in range(5):
        t = time.perf_counter()
        r.call(HELLO + 1, b'')  # DEVICE_COUNT: un round-trip puro
        rtts.append((time.perf_counter() - t) * 1000)
    rtt = min(rtts)  # il Wi-Fi ha picchi: conta il caso tipico
    size = MATS * WBYTES + (CHAIN + 1) * VBYTES + 4096
    buf, got = struct.unpack('<QQ', r.call(ALLOC, struct.pack('<IQ', 0, size)))
    if buf == 0:
        raise MemoryError('il worker non ha RAM per il benchmark')
    base, = struct.unpack('<Q', r.call(GET_BASE, struct.pack('<Q', buf)))
    base = (base + 63) // 64 * 64
    ws = [tensor(1 + i, Q4_0, buf, (K, K), (18, QROW), data=base + i * WBYTES) for i in range(MATS)]
    vbase = base + MATS * WBYTES
    vec = lambda i, op=0, src=(): tensor(100 + i, F32, buf, (K, 1), (4, VBYTES), op, src, vbase + i * VBYTES)
    # riempie le matrici: pagine vere (non la pagina zero condivisa) e scale fp16 0x1111 valide
    for w in ws:
        r.send(MEMSET, w + struct.pack('<QQB', 0, WBYTES, 0x11))
    r.send(MEMSET, vec(0) + struct.pack('<QQB', 0, VBYTES, 0x11))
    nodes = [vec(i + 1, OP_MUL_MAT, (1 + i % MATS, 100 + i)) for i in range(CHAIN)]
    tensors = ws + [vec(0)] + nodes
    graph = struct.pack('<IQI', 0, 0, CHAIN) + b''.join(struct.pack('<Q', 101 + i) for i in range(CHAIN)) + \
        struct.pack('<I', len(tensors)) + b''.join(tensors)
    get = nodes[-1] + struct.pack('<QQ', 0, 4)
    times = []
    # almeno `runs` grafi e 0,4 s di calcolo: la CPU di un portatile sale di frequenza solo dopo un po'
    while len(times) < runs or (sum(times) < 400 and len(times) < 60):
        t = time.perf_counter()
        r.send(GRAPH_COMPUTE, graph)
        r.call(GET_TENSOR, get)  # sincronizza: risponde a calcolo finito
        times.append((time.perf_counter() - t) * 1000)
    r.send(FREE, struct.pack('<Q', buf))
    r.s.close()
    best = min(times[1:])
    gbs = CHAIN * WBYTES / max(0.1, best - rtt) / 1e6
    return rtt, best, gbs


TOKENS, MM_CHAIN = 64, 4  # prodotto matrice-matrice: un prompt corto, 4 prodotti per grafo


def compute_gflops(ep):
    """GFLOPS di prodotti matrice-matrice Q4_0 4096x4096 per F32 4096x64 (come la lettura di un prompt)."""
    r = Rpc(ep, timeout=60)
    act = K * TOKENS * 4
    size = WBYTES + (MM_CHAIN + 1) * act + 4096
    buf, got = struct.unpack('<QQ', r.call(ALLOC, struct.pack('<IQ', 0, size)))
    if buf == 0:
        raise MemoryError('il worker non ha RAM per la prova di calcolo')
    base, = struct.unpack('<Q', r.call(GET_BASE, struct.pack('<Q', buf)))
    base = (base + 63) // 64 * 64
    w = tensor(1, Q4_0, buf, (K, K), (18, QROW), data=base)
    x = lambda i, op=0, src=(): tensor(100 + i, F32, buf, (K, TOKENS), (4, K * 4), op, src, base + WBYTES + i * act)
    r.send(MEMSET, w + struct.pack('<QQB', 0, WBYTES, 0x11))
    r.send(MEMSET, x(0) + struct.pack('<QQB', 0, act, 0x11))
    nodes = [x(i + 1, OP_MUL_MAT, (1, 100 + i)) for i in range(MM_CHAIN)]
    tensors = [w, x(0)] + nodes
    graph = struct.pack('<IQI', 0, 0, MM_CHAIN) + b''.join(struct.pack('<Q', 101 + i) for i in range(MM_CHAIN)) +         struct.pack('<I', len(tensors)) + b''.join(tensors)
    get = nodes[-1] + struct.pack('<QQ', 0, 4)
    times = []
    while len(times) < 4 or (sum(times) < 1500 and len(times) < 30):
        t = time.perf_counter()
        r.send(GRAPH_COMPUTE, graph); r.call(GET_TENSOR, get)
        times.append((time.perf_counter() - t) * 1000)
    r.send(FREE, struct.pack('<Q', buf)); r.s.close()
    return MM_CHAIN * 2 * K * K * TOKENS / (min(times[1:]) / 1000) / 1e9


if __name__ == '__main__':
    for ep in sys.argv[1:]:
        try:
            t0 = time.time()
            rtt, ms, gbs = bench(ep)
            print(f'{ep}: rtt {rtt:.1f} ms · grafo {ms:.1f} ms · {gbs:.1f} GB/s · benchmark {time.time() - t0:.1f} s')
        except Exception as e:
            print(f'{ep}: errore {e!r}')
