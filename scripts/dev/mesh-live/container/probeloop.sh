#!/bin/sh
# probeloop.sh FROMIP TOIP OUTFILE : used by phase RH.
# Every 200 ms, one HTTP GET from FROMIP to TOIP:8080 with a 1 s limit. One line per try in OUTFILE:
# "<epoch milliseconds> ok" or "<epoch milliseconds> fail". Runs until /tmp/probe.stop exists.
from=$1; to=$2; out=$3
: > "$out"
while [ ! -f /tmp/probe.stop ]; do
  ms=$(date +%s%3N)
  if curl -s -m 1 -f --interface "$from" -o /dev/null "http://$to:8080/"; then
    echo "$ms ok" >> "$out"
  else
    echo "$ms fail" >> "$out"
  fi
  sleep 0.2
done
