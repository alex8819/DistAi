# uso: rpcrelay.py PORTA_LOCALE HOST_DEST PORTA_DEST RITARDO_MS
# Relay TCP che aggiunge RITARDO_MS alle risposte (dest -> client): ogni attesa sincrona costa RITARDO_MS in più.
# Conta anche i messaggi per direzione, così si stimano i round-trip RPC per token.
import asyncio, sys, time
LP, DH, DP, D = int(sys.argv[1]), sys.argv[2], int(sys.argv[3]), float(sys.argv[4]) / 1000
stats = {'up': 0, 'down': 0, 'upB': 0, 'downB': 0}
last = {'up': 0.0, 'down': 0.0}; worker_ms, coord_ms = [], []  # attesa risposta / tempo tra risposta e richiesta successiva
async def pipe(r, w, key, delay):
    q = asyncio.Queue()
    async def sender():
        while True:
            t, data = await q.get()
            if data is None: w.close(); return
            dt = t + delay - time.monotonic()
            if dt > 0: await asyncio.sleep(dt)
            w.write(data); await w.drain()
    s = asyncio.create_task(sender())
    while True:
        data = await r.read(65536)
        if not data: await q.put((0, None)); break
        stats[key] += 1; stats[key + 'B'] += len(data); now = time.monotonic()
        if key == 'down' and last['up'] > last['down']: worker_ms.append((now - last['up']) * 1000)
        if key == 'up' and last['down'] > last['up']: coord_ms.append((now - last['down']) * 1000)
        last[key] = now
        await q.put((time.monotonic(), data))
    await s
async def handle(cr, cw):
    sk = cw.get_extra_info('socket'); import socket as so; sk.setsockopt(so.IPPROTO_TCP, so.TCP_NODELAY, 1)
    dr, dw = await asyncio.open_connection(DH, DP)
    dw.get_extra_info('socket').setsockopt(so.IPPROTO_TCP, so.TCP_NODELAY, 1)
    await asyncio.gather(pipe(cr, dw, 'up', 0), pipe(dr, cw, 'down', D), return_exceptions=True)
async def report():
    while True:
        await asyncio.sleep(5)
        w = sorted(x for x in worker_ms if x < 2000); c = sorted(x for x in coord_ms if x < 2000)
        med = lambda v: v[len(v) // 2] if v else 0
        print(dict(stats), f"worker+rete mediana {med(w):.1f} ms (n={len(w)}) | coordinatore mediana {med(c):.1f} ms (n={len(c)})", flush=True)
        worker_ms.clear(); coord_ms.clear()
async def main():
    srv = await asyncio.start_server(handle, '0.0.0.0', LP)
    asyncio.create_task(report())
    async with srv: await srv.serve_forever()
asyncio.run(main())
