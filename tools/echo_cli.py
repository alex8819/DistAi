import socket, sys, time, statistics as st
host, port, n, it = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), 300
s = socket.create_connection((host, port)); s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
msg = b"x" * n; t = []
for i in range(it + 20):
    a = time.perf_counter(); s.sendall(msg); got = 0
    while got < n: got += len(s.recv(n - got))
    if i >= 20: t.append((time.perf_counter() - a) * 1000)
t.sort(); print(f"{n:6d} B  mediana {st.median(t):.2f} ms  p90 {t[int(len(t)*.9)]:.2f}  min {t[0]:.2f}")
