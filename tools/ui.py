# uso: ui.py SERIAL [testo_da_toccare]  -> elenca gli elementi dell'app (o tocca quello che contiene il testo)
import re, subprocess, sys
s = sys.argv[1]
subprocess.run(['adb', '-s', s, 'shell', 'uiautomator', 'dump', '/sdcard/ui.xml'], capture_output=True)
x = subprocess.run(['adb', '-s', s, 'shell', 'cat', '/sdcard/ui.xml'], capture_output=True, text=True, encoding='utf-8', errors='replace').stdout
nodes = [(m.group(1), m.group(2), m.group(3)) for m in re.finditer(r'text="([^"]*)"[^>]*?clickable="(true|false)"[^>]*?bounds="\[(\d+,\d+)\]\[(?:\d+,\d+)\]"', x)]
b = {m.group(1): (m.group(2), m.group(3), m.group(4), m.group(5)) for m in re.finditer(r'text="([^"]*)"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', x)}
if len(sys.argv) > 2:
    for t, (x1, y1, x2, y2) in b.items():
        if sys.argv[2].lower() in t.lower():
            cx, cy = (int(x1) + int(x2)) // 2, (int(y1) + int(y2)) // 2
            subprocess.run(['adb', '-s', s, 'shell', 'input', 'tap', str(cx), str(cy)]); print('toccato:', t[:60]); break
    else: print('non trovato:', sys.argv[2])
else:
    for t, c, _ in nodes:
        if t: print(('[btn] ' if c == 'true' else '      ') + t.replace('&#10;', ' | ')[:90])
