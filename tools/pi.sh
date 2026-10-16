#!/usr/bin/env bash
# uso: pi.sh HOST stop | srv [args] | rpc [args]  -> (ri)avvia llama-server o rpc-server sul nodo Linux (Raspberry) in ~/distai
H=$1; W=$2; shift 2
ssh $H 'pkill -f "[l]lama-server-pi|[r]pc-server-pi"; sleep 1'
case $W in
  srv) ssh -n -f $H "cd distai && setsid nohup ./llama-server-pi $* --host 0.0.0.0 --port 8080 </dev/null >srv.log 2>&1 &" ;;
  rpc) ssh -n -f $H "cd distai && $ENVV setsid nohup ./rpc-server-pi -H 0.0.0.0 -p 50052 $* </dev/null >rpc.log 2>&1 &" ;;
esac
