#!/usr/bin/env python3
"""Minimal STUN binding responder (RFC 5389) for the harness.

The sidecar discovers its endpoint with STUN and the default servers are on the
public internet, which this harness must not touch. This answers binding
requests with the address the request came from, which is what a real server
does. On a flat Docker network that address is the container's own IP, so no
NAT is involved. Listens on two ports so the sidecar's NAT classifier (which
wants two servers) has two.
"""
import os, socket, struct, select, sys

COOKIE = 0x2112A442

# STUN_REPORT_PORT (the WITH_LINKS stack sets it to the WireGuard port): answer with that port, not the one
# the request came from. In this stack a sidecar's probe leaves from an ephemeral port (its endpoint comes out
# as, say, 198.51.100.6:59364), not from 51820 where WireGuard listens. A home NAT that keeps the source port
# (most do) would show 51820, which is what this answers. Unset (the default run), nothing changes.
REPORT_PORT = int(os.environ.get("STUN_REPORT_PORT", "0"))

def serve(ports):
    socks = []
    for p in ports:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind(("0.0.0.0", p))
        socks.append(s)
    print(f"stun: listening on udp {ports}", flush=True)
    while True:
        r, _, _ = select.select(socks, [], [])
        for s in r:
            data, (ip, port) = s.recvfrom(2048)
            if len(data) < 20:
                continue
            mtype, mlen, cookie = struct.unpack("!HHI", data[:8])
            if mtype != 0x0001:
                continue
            txid = data[8:20]
            xport = (REPORT_PORT or port) ^ (COOKIE >> 16)
            xaddr = struct.unpack("!I", socket.inet_aton(ip))[0] ^ COOKIE
            attr = struct.pack("!HHBBHI", 0x0020, 8, 0, 1, xport, xaddr)
            resp = struct.pack("!HHI", 0x0101, len(attr), COOKIE) + txid + attr
            s.sendto(resp, (ip, port))

if __name__ == "__main__":
    serve([3478, 3479])
