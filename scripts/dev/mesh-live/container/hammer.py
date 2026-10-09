#!/usr/bin/env python3
"""Tries to open TCP connections to a port that must stay closed to this host.

usage: hammer.py <source-ip> <dest-ip> <port> <seconds> <resultfile> [threads]
Every attempt binds the source address and connects with a short timeout.
Writes "attempts=N connected=M" to the result file when done. connected must be 0.
"""
import socket, sys, threading, time

src, dst, port, secs, out = sys.argv[1], sys.argv[2], int(sys.argv[3]), float(sys.argv[4]), sys.argv[5]
threads = int(sys.argv[6]) if len(sys.argv) > 6 else 12
end = time.time() + secs
lock = threading.Lock()
n = ok = 0

def work():
    global n, ok
    while time.time() < end:
        s = socket.socket()
        s.settimeout(0.25)
        try:
            s.bind((src, 0))
            r = s.connect_ex((dst, port))
        except OSError:
            r = -1
            time.sleep(0.02)  # the mesh interface is down (sidecar restarting): do not spin
        s.close()
        with lock:
            n += 1
            if r == 0:
                ok += 1

ts = [threading.Thread(target=work) for _ in range(threads)]
[t.start() for t in ts]
[t.join() for t in ts]
open(out, "w").write(f"attempts={n} connected={ok}\n")
