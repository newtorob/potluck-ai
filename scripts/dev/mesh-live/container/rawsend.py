#!/usr/bin/env python3
"""Sends one hand-made IPv4 packet (raw socket, IP_HDRINCL). Needs NET_RAW.

usage: rawsend.py <kind> <src-ip> <dst-ip> [sport] [dport]
kinds:
  ack        a bare TCP ACK, as a stray reply
  synack     a TCP SYN+ACK, as a reply to a connection nobody opened
  syn        a TCP SYN
  rst        a TCP RST
  fragsyn    a TCP SYN to dport split into two IP fragments (the first has MF set)
  laterfrag  only a later fragment (offset 1, no TCP header at all)
  udp        a UDP datagram
  icmp       an ICMP echo request
With RAWSEND_DRY=1 it prints each packet as hex instead of sending it.
The packet leaves by the normal routing table, so a mesh destination goes
through the tunnel with the given source address.
"""
import os, socket, struct, sys

def csum(b):
    if len(b) % 2:
        b += b"\0"
    s = sum(struct.unpack("!%dH" % (len(b) // 2), b))
    s = (s >> 16) + (s & 0xFFFF)
    s += s >> 16
    return ~s & 0xFFFF

IDENT = os.getpid() & 0xFFFF  # a fresh IP id per run, so fragments of two runs never share a reassembly queue

def ip(src, dst, proto, payload, ident=None, frag=0):
    ident = IDENT if ident is None else ident
    h = struct.pack("!BBHHHBBH4s4s", 0x45, 0, 20 + len(payload), ident, frag, 64, proto, 0, socket.inet_aton(src), socket.inet_aton(dst))
    h = h[:10] + struct.pack("!H", csum(h)) + h[12:]
    return h + payload

def tcp(src, dst, sport, dport, flags, payload=b""):
    h = struct.pack("!HHIIBBHHH", sport, dport, 1000, 0, 5 << 4, flags, 8192, 0, 0)
    pseudo = socket.inet_aton(src) + socket.inet_aton(dst) + struct.pack("!BBH", 0, 6, len(h) + len(payload))
    c = csum(pseudo + h + payload)
    return h[:16] + struct.pack("!H", c) + h[18:] + payload

kind, src, dst = sys.argv[1], sys.argv[2], sys.argv[3]
sport = int(sys.argv[4]) if len(sys.argv) > 4 else 40000
dport = int(sys.argv[5]) if len(sys.argv) > 5 else 8080
FIN, SYN, RST, ACK = 1, 2, 4, 16
pkts = []
if kind == "ack":
    pkts = [ip(src, dst, 6, tcp(src, dst, sport, dport, ACK))]
elif kind == "synack":
    pkts = [ip(src, dst, 6, tcp(src, dst, sport, dport, SYN | ACK))]
elif kind == "syn":
    pkts = [ip(src, dst, 6, tcp(src, dst, sport, dport, SYN))]
elif kind == "rst":
    pkts = [ip(src, dst, 6, tcp(src, dst, sport, dport, RST))]
elif kind == "fragsyn":
    seg = tcp(src, dst, sport, dport, SYN, b"x" * 20)  # 40 bytes
    pkts = [ip(src, dst, 6, seg[:24], frag=0x2000), ip(src, dst, 6, seg[24:], frag=24 // 8)]
elif kind == "laterfrag":
    pkts = [ip(src, dst, 6, b"y" * 16, frag=1)]
elif kind == "udp":
    body = b"meshlive"
    pkts = [ip(src, dst, 17, struct.pack("!HHHH", sport, dport, 8 + len(body), 0) + body)]
elif kind == "icmp":
    e = struct.pack("!BBHHH", 8, 0, 0, 0x77, 1) + b"meshlive"
    e = e[:2] + struct.pack("!H", csum(e)) + e[4:]
    pkts = [ip(src, dst, 1, e)]
else:
    sys.exit("unknown kind " + kind)
if os.environ.get("RAWSEND_DRY"):
    for p in pkts:
        print(p.hex())
    sys.exit(0)
s = socket.socket(socket.AF_INET, socket.SOCK_RAW, socket.IPPROTO_RAW)
s.setsockopt(socket.IPPROTO_IP, socket.IP_HDRINCL, 1)
for p in pkts:
    s.sendto(p, (dst, 0))
