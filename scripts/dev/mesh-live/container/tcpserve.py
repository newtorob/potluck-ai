#!/usr/bin/env python3
"""Tiny HTTP server bound to ONE address, used to prove the data path.

usage: tcpserve.py <bind-ip> <port>
GET /           replies with the server name, the address it is bound to, and
                the address the request came from. A reply with client=<a's mesh
                ip> proves the packet came through the tunnel with the right
                source address.
GET /bytes/N    replies with N zero bytes, for a bulk transfer.
"""
import socket, sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

class H(BaseHTTPRequestHandler):
    def do_GET(self):
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
