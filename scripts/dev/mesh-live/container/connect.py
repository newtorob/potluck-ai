#!/usr/bin/env python3
"""One HTTP GET from a chosen source address and source port, with SO_REUSEADDR.

usage: connect.py <src-ip> <src-port> <dst-ip> <dst-port> [timeout-seconds]
Prints the last line of the reply (tcpserve's "served-by=... client=..." line),
or FAILED:bind:<error> / FAILED:connect:<error> / FAILED:read:<error>.
curl cannot do this reliably: without SO_REUSEADDR a source port with a recent
connection in TIME_WAIT cannot be bound, which would look like a block.
"""
import socket, sys

src, sport, dst, dport = sys.argv[1], int(sys.argv[2]), sys.argv[3], int(sys.argv[4])
timeout = float(sys.argv[5]) if len(sys.argv) > 5 else 5.0
s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.settimeout(timeout)
try:
    s.bind((src, sport))
except OSError as e:
    print(f"FAILED:bind:{e}")
    sys.exit(0)
try:
    s.connect((dst, dport))
except OSError as e:
    print(f"FAILED:connect:{e}")
    sys.exit(0)
try:
    s.sendall(b"GET / HTTP/1.0\r\n\r\n")
    buf = b""
    while True:
        c = s.recv(4096)
        if not c:
            break
        buf += c
    print(buf.decode(errors="replace").strip().splitlines()[-1])
except OSError as e:
    print(f"FAILED:read:{e}")
