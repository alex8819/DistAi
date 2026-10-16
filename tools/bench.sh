#!/usr/bin/env bash
# uso: bench.sh HOST [n_predict]  -> misura prompt e generazione via /completion
H=${1:-192.168.1.133}; N=${2:-128}
until curl -s -m 2 http://$H:8080/health | grep -q ok; do sleep 2; done
P="Spiega in modo dettagliato come funziona un motore a combustione interna, descrivendo i quattro tempi del ciclo Otto, i componenti principali e i vantaggi e gli svantaggi rispetto a un motore elettrico."
for i in 1 2; do
  curl -s http://$H:8080/completion -d "{\"prompt\":\"$P\",\"n_predict\":$N,\"temperature\":0,\"cache_prompt\":false}" \
   | python3 -c "import json,sys; t=json.load(sys.stdin)['timings']; print(f\"run $i: prompt {t['prompt_n']} tok @ {t['prompt_per_second']:.1f} tok/s | gen {t['predicted_n']} tok @ {t['predicted_per_second']:.2f} tok/s\")"
done
