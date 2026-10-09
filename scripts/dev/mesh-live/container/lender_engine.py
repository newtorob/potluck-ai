#!/usr/bin/env python3
"""Stand-in for a lender's engine, used only by the WITH_LINKS phases.

It is not the engine. It does the one thing the coordinator needs from an engine
before it will offer a machine as a lender: it attests on /peers/connect as a
member of the circle, names the mesh node its sidecar is (mesh_node_id, from the
sidecar's /status) and the circle port it listens on, offers one model for the
circle audience, and keeps heartbeating. It reconnects when the coordinator goes
away, so a coordinator restart does not end the lender.

Runs from the coordinator image (it has websockets and cryptography), as its own
compose service, so a restart of the coordinator container does not stop it.
The attestation sequence is the one tests/circle_link_helpers.py and
tests/test_circle_link_wire.py in the coordinator use.

Environment: LENDER_MEMBER_ID, LENDER_NODE_ID, LINK_MODEL, CIRCLE_PORT,
COORDINATOR_WS (default ws://coordinator:8400/peers/connect).
"""
import asyncio, base64, hashlib, json, os, sys, time
from datetime import datetime, timezone

import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

URL = os.environ.get("COORDINATOR_WS", "ws://coordinator:8400/peers/connect")
MEMBER = os.environ["LENDER_MEMBER_ID"]
NODE = os.environ["LENDER_NODE_ID"]
MODEL = os.environ["LINK_MODEL"]
PORT = int(os.environ.get("CIRCLE_PORT", "8347"))
# A fixed key, so a reconnect is the same peer and the member binding holds.
PRIV = Ed25519PrivateKey.from_private_bytes(hashlib.sha256(b"meshlive lender engine " + MEMBER.encode()).digest())
PUB = base64.urlsafe_b64encode(
    PRIV.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).rstrip(b"=").decode()


def b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


async def session() -> None:
    async with websockets.connect(URL, max_size=2**20) as ws:
        challenge = json.loads(await ws.recv())
        assert challenge["type"] == "challenge", challenge
        nonce = base64.urlsafe_b64decode(challenge["nonce"] + "==")
        await ws.send(json.dumps({
            "type": "attest", "member_id": MEMBER, "peer_public_key": PUB,
            "binary_hash": "meshlive-" + "0" * 55, "client_version": "0.0.0-meshlive",
            "platform": "linux-x86_64", "hostname": "lender-engine-standin", "hardware_summary": {},
            "nonce_signature": b64url(PRIV.sign(nonce)),
            "capabilities": [{"model_slug": MODEL, "quantization": "q4"}],
            "audiences": ["circle"],
            "mesh_node_id": NODE, "circle_port": PORT,
        }))
        accept = json.loads(await ws.recv())
        if accept.get("type") != "accept":
            raise RuntimeError(f"not accepted: {accept}")
        log(f"accepted peer_id={accept['peer_id']} circle_links={accept.get('circle_links')} "
            f"bound={'mesh_node_id' in accept and accept['mesh_node_id'] == NODE}")
        peer_id, session_id = accept["peer_id"], accept["session_id"]

        async def beat() -> None:
            while True:
                await ws.send(json.dumps({
                    "type": "heartbeat", "peer_id": peer_id, "session_id": session_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(), "current_jobs": 0,
                    "max_concurrent_jobs": 2,
                }))
                await asyncio.sleep(10)

        task = asyncio.create_task(beat())
        try:
            async for _ in ws:  # the coordinator sends nothing we need to answer
                pass
        finally:
            task.cancel()


async def main() -> None:
    while True:
        try:
            await session()
            log("session ended")
        except Exception as e:  # coordinator down or restarting: try again
            log(f"session failed: {type(e).__name__}: {str(e)[:120]}")
        await asyncio.sleep(2)


asyncio.run(main())
