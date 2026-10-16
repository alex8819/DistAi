# uso: dashboard.py [--port 8099] [--llama http://127.0.0.1:8090] SERIAL:NOME [SERIAL:NOME ...]
# Dashboard sul PC: per ogni telefono mostra CPU e RAM reali del processo librpcserver (letti con adb top)
# e il testo che il cluster sta scrivendo (stream dal llama-server). Niente dati finti: se un worker non gira, risulta spento.
import argparse, json, re, subprocess, threading, time, urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ap = argparse.ArgumentParser()
ap.add_argument('--port', type=int, default=8099)
ap.add_argument('--llama', default='http://127.0.0.1:8090')
ap.add_argument('nodes', nargs='+')
A = ap.parse_args()
NODES = [n.split(':', 1) for n in A.nodes]
STATE = {s: {'name': n, 'up': False, 'cpu': 0.0, 'res_mb': 0.0} for s, n in NODES}


def mb(v):
    m = re.match(r'([\d.]+)([KMG]?)', v)
    return float(m.group(1)) * {'K': 1 / 1024, 'M': 1, 'G': 1024, '': 1 / 1048576}[m.group(2)] if m else 0.0


def poll(serial):
    while True:
        try:
            out = subprocess.run(['adb', '-s', serial, 'shell', 'top -b -n 1 -H 2>/dev/null | grep -m1 -E "librpcserver" ; top -b -n 1 | grep -m1 librpcserver'],
                                 capture_output=True, text=True, timeout=8).stdout.strip().splitlines()
            line = next((l for l in out if 'librpcserver' in l), '')
            f = line.split()
            if f:
                # PID USER PR NI VIRT RES SHR S %CPU %MEM TIME+ ARGS
                STATE[serial].update(up=True, res_mb=mb(f[5]), cpu=float(f[8]))
            else:
                STATE[serial].update(up=False, cpu=0.0, res_mb=0.0)
        except Exception:
            STATE[serial].update(up=False)
        time.sleep(0.8)


for s, _ in NODES:
    threading.Thread(target=poll, args=(s,), daemon=True).start()

PAGE = open(__file__.replace('dashboard.py', 'dashboard.html'), encoding='utf-8').read()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def do_GET(self):
        if self.path == '/stats':
            b = json.dumps(list(STATE.values())).encode()
            self.send_response(200); self.send_header('Content-Type', 'application/json'); self.end_headers(); self.wfile.write(b)
        else:
            self.send_response(200); self.send_header('Content-Type', 'text/html; charset=utf-8'); self.end_headers(); self.wfile.write(PAGE.encode())

    def do_POST(self):  # /chat: inoltra lo stream SSE del llama-server
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        req = urllib.request.Request(A.llama + '/v1/chat/completions', body, {'Content-Type': 'application/json'})
        self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
        try:
            with urllib.request.urlopen(req) as r:
                for l in r:
                    self.wfile.write(l); self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            self.wfile.write(f'data: {{"error":"{e}"}}\n\n'.encode())


print(f'dashboard su http://127.0.0.1:{A.port}')
ThreadingHTTPServer(('127.0.0.1', A.port), H).serve_forever()
