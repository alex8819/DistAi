#!/usr/bin/env bash
# uso: srv.sh SERIAL VARIANT [args llama-server...]  -> (ri)avvia llama-server sul telefono in background
D=$1; V=$2; shift 2
LIB=$(adb -s $D shell pm path com.distai.app | sed 's/package://; s/base.apk//')lib/arm64
adb -s $D shell "pkill -f libllamaserver" ; sleep 1
timeout 5 adb -s $D shell "cd /data/local/tmp && setsid $LIB/libllamaserver_$V.so $* --host 0.0.0.0 --port 8080 </dev/null >/data/local/tmp/srv.log 2>&1 &" ; true
