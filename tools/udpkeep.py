# uso: udpkeep.py HOST INTERVALLO_S DURATA_S  -> battito UDP verso la porta 50054 (test power-save Wi-Fi)
import socket, sys, time
host, interval, dur = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
end = time.time() + dur
while time.time() < end:
    s.sendto(b'k', (host, 50054)); time.sleep(interval)
