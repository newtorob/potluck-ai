#!/usr/bin/env python3
"""Stand-in for the lender engine's circle listener, used only by the WITH_LINKS phases.

usage: circlegate.py <mesh-ip> <port> [sidecar-socket]
It is bound to the lender's own mesh address and applies the gate the real
listener applies, using only what the sidecar's GET /circle-peers says:
  1. the connection's source address must be a circle peer with an inbound grant;
  2. the Authorization: Bearer token must hash (sha256) to that grant's token_sha256.
Answers: 403 not_a_circle_peer, 401 bad_token, else 200.
  GET /          "gate-ok client=<source address> link=<link id>"
  GET /ticks/N   one line a second for N seconds; the gate is checked again each
                 second and the stream ends when the grant is gone, as the real
                 listener's stream does when a link is revoked
"""
import hashlib, hmac, http.client, json, socket, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOCK = sys.argv[3] if len(sys.argv) > 3 else "/run/potluck-mesh.sock"


class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(5)
        self.sock.connect(SOCK)


def circle_peers():
    c = Unix("localhost")
    c.request("GET", "/circle-peers")
    return json.loads(c.getresponse().read())["peers"]


class H(BaseHTTPRequestHandler):
    def gate(self):
        src = self.client_address[0]
        entry = next((p for p in circle_peers() if p["mesh_ip"] == src and p.get("inbound")), None)
        if entry is None:
            return 403, "not_a_circle_peer", None
        auth = self.headers.get("Authorization", "")
        token = auth[7:] if auth.startswith("Bearer ") else ""
        want = entry["inbound"]["token_sha256"]
        if not token or not hmac.compare_digest(hashlib.sha256(token.encode("ascii", "replace")).hexdigest(), want):
            return 401, "bad_token", None
        return 200, "ok", entry["inbound"]["link_id"]

    def reply(self, code, body):
        b = body.encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        code, why, link = self.gate()
        if code != 200:
            return self.reply(code, why + "\n")
        if self.path.startswith("/ticks/") and self.path[7:].isdigit():
            self.send_response(200)
            self.end_headers()
            for _ in range(int(self.path[7:])):
                self.wfile.write(b"tick\n")
                self.wfile.flush()
                time.sleep(1)
                if self.gate()[0] != 200:
                    return
            return
        self.reply(200, f"gate-ok client={self.client_address[0]} link={link}\n")

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer((sys.argv[1], int(sys.argv[2])), H).serve_forever()
