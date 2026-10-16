#!/usr/bin/env python3
"""Genera il sottoinsieme di vocabolario per il calcolo ridotto dei logits (patch native/patches/vocab-subset.patch).

uso: vocabsub.py MODELLO.gguf OUT.txt [--corpus FILE ...] [--server URL] [--min-count N] [--keep-short N]

OUT.txt contiene un id di token per riga; si usa con  LLAMA_VOCAB_SUBSET=OUT.txt llama-server ...
Filtro di caratteri (obbligatorio): si tengono i token il cui testo, decodificato dai byte, è UTF-8 valido e fatto solo di: ASCII, lettere latine
accentate (Latin-1, Latin Extended-A), punteggiatura tipografica, € e simboli comuni nel codice e nelle documentazioni (frecce, operatori matematici,
lettere greche, righe per alberi di directory). Sempre tenuti: tutti i token speciali/di controllo e tutti i token ASCII di al massimo --keep-short
caratteri (default 3), così qualunque testo ASCII si può sempre scrivere. Con --corpus e --server (un llama-server già avviato col modello) si
tengono, tra quelli che passano il filtro, solo i token che compaiono almeno --min-count volte nel corpus tokenizzato.
Esclusi: cinese, giapponese, coreano, cirillico, arabo, emoji e simili.
"""
import argparse, json, struct, sys, urllib.request


def gpt2_inverse():
    bs = list(range(33, 127)) + list(range(161, 173)) + list(range(174, 256))
    cs, n = bs[:], 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return {chr(c): b for b, c in zip(bs, cs)}


def read_vocab(path):
    f = open(path, 'rb')
    assert f.read(4) == b'GGUF'
    f.read(4)
    _nt, kvs = struct.unpack('<QQ', f.read(16))
    rd = lambda fmt: struct.unpack(fmt, f.read(struct.calcsize(fmt)))[0]
    sizes = {0: 1, 1: 1, 2: 2, 3: 2, 4: 4, 5: 4, 6: 4, 7: 1, 10: 8, 11: 8, 12: 8}
    string = lambda: f.read(rd('<Q')).decode('utf-8', 'replace')

    def skip(t):
        if t == 8:
            f.seek(rd('<Q'), 1)
        elif t == 9:
            et, n = rd('<I'), rd('<Q')
            for _ in range(n):
                skip(et)
        else:
            f.seek(sizes[t], 1)
    model, tokens, types = None, None, None
    for _ in range(kvs):
        key, t = string(), rd('<I')
        if key == 'tokenizer.ggml.model':
            model = string()
        elif key == 'tokenizer.ggml.tokens' and t == 9:
            et, n = rd('<I'), rd('<Q')
            tokens = [string() for _ in range(n)]
        elif key == 'tokenizer.ggml.token_type' and t == 9:
            et, n = rd('<I'), rd('<Q')
            types = list(struct.unpack(f'<{n}i', f.read(4 * n)))
        else:
            skip(t)
    return model, tokens, types


def allowed_char(c):
    o = ord(c)
    return (o < 128 or 0xA0 <= o <= 0x17F or 0x2010 <= o <= 0x2027 or 0x2030 <= o <= 0x205E or o == 0x20AC
            or 0x2190 <= o <= 0x21FF or 0x2200 <= o <= 0x22FF or 0x2500 <= o <= 0x257F or 0x370 <= o <= 0x3FF
            or o in (0x2713, 0x2714, 0x2717, 0x2718, 0x2022, 0x2122, 0x00B7))


def decode(tok, model, inv):
    if model == 'gpt2':
        try:
            return bytes(inv[c] for c in tok).decode('utf-8')
        except (KeyError, UnicodeDecodeError):
            return None
    if tok.startswith('<0x') and tok.endswith('>') and len(tok) == 6:
        b = int(tok[3:5], 16)
        return chr(b) if b < 128 else None
    return tok.replace('▁', ' ')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('model'); ap.add_argument('out')
    ap.add_argument('--group', action='append', default=[], metavar='MIN=FILE[,FILE...]',
                    help='gruppo del corpus con la sua soglia: un token passa se ha almeno MIN occorrenze in QUALUNQUE gruppo (italiano e inglese con soglia bassa, codice più alta)')
    ap.add_argument('--server', help='llama-server avviato con lo stesso tokenizer (usa /tokenize)')
    ap.add_argument('--min-count', type=int, default=2); ap.add_argument('--keep-short', type=int, default=3)
    a = ap.parse_args()
    model, tokens, types = read_vocab(a.model)
    inv = gpt2_inverse()
    n = len(tokens)
    keep, special, passed, short = set(), 0, set(), 0
    for i, tok in enumerate(tokens):
        ty = types[i] if types else 1
        if ty != 1 and ty != 6:  # controllo, utente, sconosciuto, non usato: tutti tenuti
            keep.add(i); special += 1
            continue
        txt = decode(tok, model, inv)
        if txt is None or not txt or not all(allowed_char(c) for c in txt):
            continue
        passed.add(i)
        if len(txt) <= a.keep_short and all(ord(c) < 128 for c in txt):
            keep.add(i); short += 1
    if a.group and a.server:
        for spec in a.group:
            mn, _, files = spec.partition('=')
            cnt = {}
            for path in files.split(','):
                txt = open(path, 'r', errors='ignore').read()
                for off in range(0, len(txt), 20000):
                    req = urllib.request.Request(a.server.rstrip('/') + '/tokenize', json.dumps({'content': txt[off:off + 20000]}).encode(),
                                                 {'Content-Type': 'application/json'})
                    for t in json.load(urllib.request.urlopen(req, timeout=120))['tokens']:
                        cnt[t] = cnt.get(t, 0) + 1
            hit = {t for t, c in cnt.items() if c >= int(mn)}
            keep |= passed & hit
            print(f'gruppo {files.split(",")[0].split("/")[-1]}...: {sum(cnt.values())} token, {len(cnt)} distinti, soglia {mn}: {len(passed & hit)} tenuti')
    else:
        keep |= passed
    ids = sorted(keep)
    open(a.out, 'w').write('\n'.join(map(str, ids)) + '\n')
    print(f'{a.model.split("/")[-1]}: vocabolario {n}, passano il filtro di caratteri {len(passed)}, speciali {special}, corti ASCII {short} -> sottoinsieme {len(ids)} ({100 * len(ids) / n:.0f}%)')


if __name__ == '__main__':
    main()
