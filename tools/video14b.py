# uso: video14b.py [modello.gguf]
# Qwen2.5-Coder-14B su Xiaomi + Huawei + Samsung, tutti i layer sui telefoni (split uguale), cavo USB (adb forward).
# Il PC tiene solo embedding e uscita e fa da coordinatore (llama-server :8090); la dashboard sta su http://127.0.0.1:8099.
import os, subprocess, sys, time, urllib.request

import distrun, lbench  # i seriali dei telefoni arrivano da SERIAL_XIAOMI / SERIAL_HUAWEI / SERIAL_SAMSUNG

HERE = os.path.dirname(os.path.abspath(__file__))
MODEL = sys.argv[1] if len(sys.argv) > 1 else "qwen2.5-coder-14b-instruct-q3_k_m.gguf"
NODES = [("xiaomi", 50151, "c0", 2), ("huawei", 50152, "f0", 4), ("samsung", 50153, "f0", 4)]  # (telefono, porta sul PC, core, thread)
LOGDIR = os.environ.get("VIDEO_LOGDIR", "/tmp")


def main():
    subprocess.run("pkill -x llama-server; pkill -f tools/dashboard.py", shell=True)
    for dev, port, mask, th in NODES:
        distrun.start_rpc(dev, port, mask, th)
    time.sleep(2)
    cmd = [distrun.PCBIN, "-m", os.path.expanduser("~/distai-models/") + MODEL, "--rpc", ",".join(f"127.0.0.1:{p}" for _, p, _, _ in NODES),
           "-ngl", "49", "-ot", "^(output|output_norm|token_embd)[.]weight$=CPU", "-ts", "1,1,1", "-c", "4096", "-np", "1", "-t", "4",
           "--poll", "100", "--host", "127.0.0.1", "--port", "8090"]
    subprocess.Popen(cmd, stdout=open(f"{LOGDIR}/video-llama.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    dash = ["python3", os.path.join(HERE, "dashboard.py")] + [f"{lbench.DEV[d][0]}:{d.capitalize()}" for d, *_ in NODES]
    subprocess.Popen(dash, stdout=open(f"{LOGDIR}/video-dash.log", "w"), stderr=subprocess.STDOUT, start_new_session=True)
    print("avviato: llama-server :8090 (carica i pesi sui telefoni), dashboard http://127.0.0.1:8099", flush=True)
    t = lbench.ready("http://127.0.0.1:8090", 1500)
    print("PRONTO" if t else "NON PRONTO", f"{t:.0f} s" if t else "", flush=True)


if __name__ == "__main__":
    main()
