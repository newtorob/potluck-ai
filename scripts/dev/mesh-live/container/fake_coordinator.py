#!/usr/bin/env python3
"""Stand-in coordinator for phase D only. It is not the real coordinator.

The real coordinator's Postgres has UNIQUE(mesh_ip), so it cannot produce a
roster where one entry claims the receiving node's own address, or where two
entries claim the same address. This speaks just enough of /mesh/signal
(hello -> welcome, then whatever the harness pushes) to hand a real sidecar a
roster the real coordinator cannot send. Stdlib only.

Control: drop a JSON file into /tmp/push/ and it is sent to the connected
client as one message, then deleted (files are taken in name order).
Keys it hands out are random; they are written to /tmp/fake_keys.json so the
harness can check that none of them appears in the sidecar log.
"""
import asyncio, base64, hashlib, json, os, struct, secrets, glob

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
SELF_IP = "100.64.9.9"
HOUSEHOLD = "00000000-0000-4000-8000-0000000000aa"
SELF_ID = "00000000-0000-4000-8000-0000000000d0"

def key(n=32):
    return base64.b64encode(secrets.token_bytes(n)).decode()

def peer(i, name, ip, k):
    return {"node_id": f"00000000-0000-4000-8000-{i:012x}", "node_name": name,
            "mesh_ip": ip, "wg_public_key": k, "last_endpoint": None,
            "local_endpoints": [], "platform": "linux"}

KEYS = {n: key() for n in ["good1", "own", "dupA", "dupB", "newcomer", "good2", "good3"]}
KEYS["badkey"] = "not-a-key!!"          # not base64
KEYS["shortkey"] = key(31)               # base64 of 31 bytes
ROSTER = [
    peer(1, "good-1",    "100.64.9.10", KEYS["good1"]),
    peer(2, "claims-own","100.64.9.9",  KEYS["own"]),       # this node's own address
    peer(3, "dup-A",     "100.64.9.20", KEYS["dupA"]),      # two nodes, one address
    peer(4, "dup-B",     "100.64.9.20", KEYS["dupB"]),
    peer(5, "bad-key",   "100.64.9.30", KEYS["badkey"]),
    peer(6, "short-key", "100.64.9.31", KEYS["shortkey"]),
    peer(7, "good-2",    "100.64.9.11", KEYS["good2"]),
]
NEW = {  # pushed later by the harness as files; kept here so the keys are known
    "newcomer": peer(8, "newcomer", "100.64.9.11", KEYS["newcomer"]),  # takes good-2's address
    "good3": peer(9, "good-3", "100.64.9.12", KEYS["good3"]),
}
json.dump({"keys": KEYS, "peers": ROSTER, "new": NEW}, open("/tmp/fake_keys.json", "w"))
os.makedirs("/tmp/push", exist_ok=True)

def frame(data: bytes, opcode=0x1) -> bytes:
    n = len(data)
    if n < 126:
        h = struct.pack("!BB", 0x80 | opcode, n)
    elif n < 65536:
        h = struct.pack("!BBH", 0x80 | opcode, 126, n)
    else:
        h = struct.pack("!BBQ", 0x80 | opcode, 127, n)
    return h + data

async def read_frame(r):
    b1, b2 = await r.readexactly(2)
    op, masked, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
    if n == 126:
        n = struct.unpack("!H", await r.readexactly(2))[0]
    elif n == 127:
        n = struct.unpack("!Q", await r.readexactly(8))[0]
    mask = await r.readexactly(4) if masked else b""
    data = await r.readexactly(n)
    if masked:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    return op, data

async def handle(r, w):
    try:
        head = await r.readuntil(b"\r\n\r\n")
        lines = head.decode().split("\r\n")
        hdr = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        if "sec-websocket-key" not in hdr:
            w.write(b"HTTP/1.1 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"); await w.drain(); return
        acc = base64.b64encode(hashlib.sha1((hdr["sec-websocket-key"] + GUID).encode()).digest()).decode()
        w.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                 f"Sec-WebSocket-Accept: {acc}\r\n\r\n").encode()); await w.drain()
        op, data = await read_frame(r)   # hello
        print("hello received", flush=True)
        welcome = {"type": "welcome", "node_id": SELF_ID, "household_id": HOUSEHOLD,
                   "mesh_ip": SELF_IP, "roster": ROSTER}
        w.write(frame(json.dumps(welcome).encode())); await w.drain()
        print(f"welcome sent, roster_size={len(ROSTER)}", flush=True)

        async def pusher():
            while True:
                for f in sorted(glob.glob("/tmp/push/*.json")):
                    msg = open(f).read(); os.remove(f)
                    w.write(frame(msg.encode())); await w.drain()
                    print(f"pushed {os.path.basename(f)}", flush=True)
                await asyncio.sleep(0.3)
        t = asyncio.create_task(pusher())
        try:
            while True:
                op, data = await read_frame(r)
                if op == 0x8:
                    w.write(frame(b"", 0x8)); await w.drain(); break
                if op == 0x9:
                    w.write(frame(data, 0xA)); await w.drain()
                elif op == 0x1:
                    m = json.loads(data)
                    if m.get("type") == "ping":
                        w.write(frame(json.dumps({"type": "pong", "server_time": "2026-10-08T00:00:00Z"}).encode())); await w.drain()
        finally:
            t.cancel()
    except (asyncio.IncompleteReadError, ConnectionError):
        pass
    finally:
        w.close()

async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", 8400)
    print("fake coordinator on :8400", flush=True)
    async with srv:
        await srv.serve_forever()

asyncio.run(main())
