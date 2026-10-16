# uso: plan.py MODELLO.gguf NOME=host:porta[@host:porta_status] ... [--ctx 2048] [--eff 1.0] [--start]
# Pianificatore del cluster dal PC: quale sottoinsieme di nodi usare e quanti layer a ciascuno per avere più tok/s.
# Il primo nodo è il coordinatore (tiene anche embedding e output). Per ogni nodo misura velocità e round-trip
# (rpcbench) e legge la RAM libera da /status (di default sulla porta RPC + 1, come Node.statusPort nell'app).
# Modello dei tempi (lo stesso di SplitPlan.kt): un token attraversa i nodi uno dopo l'altro, quindi
#   tempo/token = somma(layer_i * byte_layer / banda_i) + byte_extra / banda_coord + somma(round-trip dei nodi remoti usati)
# Prova TUTTI i sottoinsiemi che entrano in RAM (layer ai più veloci per primi) e per ogni nodo stima quanto vale:
# "serve per la RAM" (senza non entra), "conviene" o "rallenta" (con la perdita stimata se lo si collega lo stesso).
# --eff corregge la banda di rpcbench verso quella vera dell'inferenza (rapporto tok/s misurati / stimati).
# --start avvia il piano migliore sul coordinatore di tools/pcnode.py (POST /coord/start sulla porta status del primo nodo).
import argparse, itertools, json, os, sys, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', 'native', 'llama.cpp', 'gguf-py'))
import rpcbench  # noqa: E402
from gguf import GGUFReader  # noqa: E402

sys.stdout.reconfigure(encoding='utf-8')  # la console di Windows è cp1252
RESERVE_MB, COMPUTE_MB = 400, 200  # come SplitPlan.kt
HOP_MS = 10.0  # costo per nodo remoto oltre al round-trip misurato (SplitPlan.HOP_MS, tarato sul 0.5B il 12/10/2026)
# Android conta in MemAvailable anche la memoria che recupera comprimendo in zram: i pesi del worker (RAM anonima)
# finiscono in swap e ogni token li rilegge. Misurato il 12/10/2026 sul Note 10: 4,1 GB "liberi", con 3,4 GB di pesi
# 2,1 GB in zram e 2,19 tok/s invece di 3,98 stimati. Sui telefoni si usa solo questa frazione della RAM libera.
ANDROID_RAM_FRAC = 0.5
MB = 1 << 20

ap = argparse.ArgumentParser()
ap.add_argument('model')
ap.add_argument('nodes', nargs='+', help='NOME=host:porta[@host:porta_status], il primo è il coordinatore')
ap.add_argument('--ctx', type=int, default=2048)
ap.add_argument('--eff', type=float, default=1.0, help='fattore di correzione della banda misurata')
ap.add_argument('--start', action='store_true', help='avvia il piano migliore sul coordinatore (pcnode.py)')
a = ap.parse_args()


def gguf_info(path):
    """(numero di layer, byte medi per layer, byte fuori dai layer, byte di KV per token per layer)."""
    r = GGUFReader(path)
    field = lambda suffix: next((f.contents() for k, f in r.fields.items() if k.endswith(suffix)), None)
    L = field('.block_count')
    layer = sum(int(t.n_bytes) for t in r.tensors if t.name.startswith('blk.'))
    other = sum(int(t.n_bytes) for t in r.tensors if not t.name.startswith('blk.'))
    heads, kv_heads = field('.attention.head_count'), field('.attention.head_count_kv') or field('.attention.head_count')
    kv_heads = kv_heads[0] if isinstance(kv_heads, list) else kv_heads
    k_len = field('.attention.key_length') or field('.embedding_length') // (heads[0] if isinstance(heads, list) else heads)
    v_len = field('.attention.value_length') or k_len
    return L, layer // L, other, kv_heads * (k_len + v_len)


def status(ep):
    host, port = ep.rsplit(':', 1)
    try:
        return json.load(urllib.request.urlopen(f'http://{host}:{port}/status', timeout=5))
    except Exception:
        return {}


L, layer_b, other_b, kv_dim = gguf_info(a.model)
per_layer_mb = (layer_b + a.ctx * kv_dim * 2) // MB + 1
print(f'{os.path.basename(a.model)}: {L} layer da {layer_b / MB:.0f} MB (+{a.ctx * kv_dim * 2 / MB:.0f} MB di KV a ctx {a.ctx}),'
      f' extra sul coordinatore {other_b / MB:.0f} MB\n')

nodes = []
for i, spec in enumerate(a.nodes):
    name, eps = spec.split('=', 1)
    rpc, _, st = eps.partition('@')
    if not st:
        h, p = rpc.rsplit(':', 1); st = f'{h}:{int(p) + 1}'
    j = status(st)
    free = j.get('free', 0)
    if i and j.get('variant', '').startswith('v8'):  # telefono worker (il coordinatore legge in mmap: non va in zram)
        free = int(free * ANDROID_RAM_FRAC)
    try:
        rtt, _, gbps = rpcbench.bench(rpc)
    except Exception as e:
        print(f'{name}: {rpc} non risponde ({e}), escluso'); continue
    budget = free - RESERVE_MB - COMPUTE_MB - (other_b // MB if i == 0 else 0)
    n = dict(name=name, rpc=rpc, status=st, free=free, gbps=gbps * a.eff, rtt=rtt if i else 0.0,
             cap=max(0, budget // per_layer_mb))
    nodes.append(n)
    print(f'{name:<22} {rpc:<22} libera {free:>5} MB · max {n["cap"]:>3} layer · {n["gbps"]:5.1f} GB/s · rtt {n["rtt"]:.1f} ms')
coord, remote = nodes[0], nodes[1:]


def best_alloc(subset, force=None):
    """Layer ai più veloci per primi fino alla loro RAM (ottimo per un costo lineare con capacità); None se non entra.
    [force]: nodo che riceve comunque la sua quota di RAM (come se lo si collegasse per la memoria)."""
    group = [coord] + list(subset)
    if sum(n['cap'] for n in group) < L:
        return None
    alloc, left = {}, L
    if force:
        alloc[force['name']] = min(force['cap'], max(1, round(L * force['cap'] / sum(n['cap'] for n in group))))
        left -= alloc[force['name']]
    for n in sorted((n for n in group if n is not force), key=lambda n: -n['gbps']):
        alloc[n['name']] = min(n['cap'], left); left -= alloc[n['name']]
    ms = sum(alloc[n['name']] * layer_b / 1e9 / n['gbps'] * 1000 for n in group) + other_b / 1e9 / coord['gbps'] * 1000 \
        + sum(n['rtt'] + HOP_MS for n in subset if alloc[n['name']] > 0)
    used = [n for n in subset if alloc[n['name']] > 0]
    return 1000 / ms, alloc, used


plans = [(p, s) for k in range(len(remote) + 1) for s in itertools.combinations(remote, k) if (p := best_alloc(s))]
if not plans:
    sys.exit(f'\nil modello non entra nemmeno usando tutti i nodi: servono altri '
             f'{(L - sum(n["cap"] for n in nodes)) * per_layer_mb} MB circa')
(tok, alloc, used), _ = max(plans, key=lambda x: x[0][0])
print(f'\nPIANO MIGLIORE: {tok:.2f} tok/s stimati · ' +
      ' · '.join(f'{n["name"]} {alloc[n["name"]]}' for n in [coord] + used))

print('\nValore di ogni nodo (miglior piano senza di lui → con lui):')
all_tok = best_alloc(remote)
for n in remote:
    without = [p for p, s in plans if n not in s]
    w = max(without, key=lambda p: p[0]) if without else None
    if w is None:
        verdict = 'NECESSARIO: senza non entra in RAM'
    elif n in used:
        verdict = f'conviene: {w[0]:.2f} → {tok:.2f} tok/s (+{(tok / w[0] - 1) * 100:.0f}%)'
    elif n['cap'] == 0:
        verdict = 'escluso: non ha RAM libera per nemmeno un layer'
    else:
        forced = best_alloc(used + [n], force=n)
        verdict = 'escluso, non serve' + (f'; usarlo per la sua RAM ({forced[1][n["name"]]} layer) costerebbe'
                                          f' {(1 - forced[0] / tok) * 100:.0f}% ({forced[0]:.2f} tok/s)' if forced else '')
    print(f'  {n["name"]:<22} {verdict}')
if all_tok and remote:
    # confronto: tutti i nodi con i layer divisi per RAM, senza pesare la velocità (il modo "ingenuo" di unirli)
    caps = sum(n['cap'] for n in nodes)
    share = [n['cap'] * L // caps for n in nodes]
    share[max(range(len(nodes)), key=lambda i: nodes[i]['cap'] - share[i])] += L - sum(share)
    ms = sum(s * layer_b / 1e9 / n['gbps'] * 1000 for s, n in zip(share, nodes)) + other_b / 1e9 / coord['gbps'] * 1000 \
        + sum(n['rtt'] + HOP_MS for s, n in zip(share[1:], remote) if s)
    split = ' · '.join(f"{n['name']} {s}" for n, s in zip(nodes, share))
    print(f'\nTutti i nodi con i layer divisi per RAM: {1000 / ms:.2f} tok/s stimati ({split})')

layers_remote = [alloc[n['name']] for n in used]
body = {'model': os.path.basename(a.model), 'rpc': ','.join(n['rpc'] for n in used),
        'ngl': sum(layers_remote), 'ts': ','.join(map(str, layers_remote)), 'ctx': a.ctx}
print('\n/coord/start:', json.dumps(body))
if a.start:
    req = urllib.request.Request(f'http://{coord["status"]}/coord/start', json.dumps(body).encode(), method='POST')
    print('avviato:', urllib.request.urlopen(req, timeout=10).status)
