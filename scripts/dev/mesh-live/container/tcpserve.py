#!/usr/bin/env python3
"""Tiny HTTP server bound to ONE address, used to prove the data path.

usage: tcpserve.py <bind-ip> <port>
GET /           replies with the server name, the address it is bound to, and
                the address the request came from. A reply with client=<a's mesh
                ip> proves the packet came through the tunnel with the right
                source address.
GET /bytes/N    replies with N zero bytes, for a bulk transfer.
GET /rand/N?seed=S   replies with N bytes of a seeded pseudo-random stream.
GET /sha/N?seed=S    replies with the sha256 of that same stream, so a client can
                     check a transfer arrived whole and in order.
GET /ticks/N    replies with one line a second for N seconds, for a stream that
                must be cut at a deadline.
"""
import hashlib, random, socket, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def rand_chunks(self, total, seed):
        rng = random.Random(seed)
        while total > 0:
            n = min(total, 65536)
            yield rng.randbytes(n)
            total -= n

    def do_GET(self):
        path, _, query = self.path.partition("?")
        seed = int(query[5:]) if query.startswith("seed=") and query[5:].isdigit() else 1
        if path.startswith("/rand/") and path[6:].isdigit():
            total = int(path[6:])
            self.send_response(200)
            self.send_header("Content-Length", str(total))
            self.end_headers()
            for c in self.rand_chunks(total, seed):
                self.wfile.write(c)
            return
        if path.startswith("/sha/") and path[5:].isdigit():
            h = hashlib.sha256()
            for c in self.rand_chunks(int(path[5:]), seed):
                h.update(c)
            body = (h.hexdigest() + "\n").encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/ticks/") and path[7:].isdigit():
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            for i in range(int(path[7:])):
                self.wfile.write(b"tick\n")
                self.wfile.flush()
                time.sleep(1)
            return
        if self.path.startswith("/bytes/") and self.path[7:].isdigit():
            left = int(self.path[7:])
            self.send_response(200)
            self.send_header("Content-Length", str(left))
            self.end_headers()
            chunk = b"\0" * 65536
            while left > 0:
                self.wfile.write(chunk[:min(left, len(chunk))])
                left -= min(left, len(chunk))
            return
        body = f"served-by={socket.gethostname()} bound={self.server.server_address[0]} client={self.client_address[0]}\n".encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

if __name__ == "__main__":
    ip, port = sys.argv[1], int(sys.argv[2])
    ThreadingHTTPServer((ip, port), H).serve_forever()
