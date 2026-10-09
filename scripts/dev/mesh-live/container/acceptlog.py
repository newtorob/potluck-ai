#!/usr/bin/env python3
"""Listener that records every connection it accepts, to prove one never happens.

usage: acceptlog.py <bind-ip> <port> <logfile>
Each accepted connection appends one line "<peer-ip>:<peer-port>" to the log
file and is closed at once. An empty log means nobody got through.
"""
import socket, sys

ip, port, log = sys.argv[1], int(sys.argv[2]), sys.argv[3]
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind((ip, port))
s.listen(128)
open(log, "a").close()
while True:
    c, a = s.accept()
    with open(log, "a") as f:
        f.write(f"{a[0]}:{a[1]}\n")
    c.close()
