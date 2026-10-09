#!/usr/bin/env python3
"""Make one circle link as the requester's engine would and time it. WITH_LINKS phases only.

usage: linkprobe.py <coordinator-url> <circle-id> <requester-node-id> <model> <my-mesh-ip> [budget-seconds] [mode]
Starts the clock, POSTs /me/circles/<id>/links, then connects to the lender's
mesh address and circle port from <my-mesh-ip> with the link's token until the
lender's gate answers, and prints one JSON object:
  {"status": 201, "link_id":..., "token":..., "lender": ip, "port": n,
   "create_ms": time from the POST to its answer,
   "ready_ms": (engine mode) time from the POST until the sidecar's /circle-peers said reachable,
   "first_byte_ms": time from the POST to the first byte of the gate's answer,
   "attempts": connect attempts, "reply": the first line of the answer}
mode "engine" (the default) waits for /circle-peers to show the lender reachable before it connects, polling the
sidecar every 50 ms, as the requester engine does; mode "naive" connects at once and lets TCP retry.
"first_byte_ms" is the first byte of a 200 answer, and null if none came within the budget (default 20 s).
"""
import http.client, json, socket, sys, time, urllib.error, urllib.request

base, circle, node, model, me = sys.argv[1:6]
budget = float(sys.argv[6]) if len(sys.argv) > 6 else 20.0
mode = sys.argv[7] if len(sys.argv) > 7 else "engine"
SIDECAR = "/run/potluck-mesh.sock"


class Unix(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(3)
        self.sock.connect(SIDECAR)


def reachable(ip):
    try:
        c = Unix("localhost")
        c.request("GET", "/circle-peers")
        return any(p["mesh_ip"] == ip and p["reachable"] for p in json.loads(c.getresponse().read())["peers"])
    except Exception:
        return False


t0 = time.monotonic()
req = urllib.request.Request(
    f"{base}/me/circles/{circle}/links", method="POST",
    data=json.dumps({"requester_node_id": node, "model_slug": model}).encode(),
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as r:
        status, body = r.status, json.loads(r.read())
except urllib.error.HTTPError as e:
    print(json.dumps({"status": e.code, "body": e.read().decode()[:300]}))
    sys.exit(0)
t_create = time.monotonic()
out = {"status": status, "link_id": body["link_id"], "token": body["token"], "lender": body["lender"]["mesh_ip"],
       "port": body["lender"]["port"], "create_ms": round((t_create - t0) * 1000), "first_byte_ms": None, "attempts": 0}
if mode == "engine":
    while time.monotonic() - t0 < budget and not reachable(out["lender"]):
        time.sleep(0.05)
    out["ready_ms"] = round((time.monotonic() - t0) * 1000)
while time.monotonic() - t0 < budget:
    out["attempts"] += 1
    s = socket.socket()
    s.settimeout(3)
    try:
        s.bind((me, 0))
        s.connect((out["lender"], out["port"]))
        s.sendall(f"GET / HTTP/1.0\r\nAuthorization: Bearer {out['token']}\r\n\r\n".encode())
        first = s.recv(1)
        if first:
            t_byte = round((time.monotonic() - t0) * 1000)
            head = (first + s.recv(300)).decode(errors="replace").split("\r\n")[0]
            out["reply"] = head
            if " 200 " in head:  # a 401 or 403 is the gate answering, not the link working
                out["first_byte_ms"] = t_byte
                break
            time.sleep(0.05)
    except OSError:
        pass
    finally:
        s.close()
print(json.dumps(out))
