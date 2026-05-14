#!/usr/bin/env python3
"""End-to-end smoke test for the coordinator's /peers/connect WS handshake.

Generates a fresh per-peer Ed25519 keypair, opens a WebSocket to the
coordinator, completes the challenge → attest → accept handshake, then
disconnects. Run with --invalid-* flags to exercise the failure paths.

Requires `websockets` and `cryptography` on the python that runs it.
The system python on a dev machine usually has both; otherwise:
    pip install websockets cryptography
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import sys
import uuid
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

import websockets


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def b64url_decode(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def make_peer_key() -> tuple[Ed25519PrivateKey, str]:
    """Fresh per-peer keypair. Returns (priv, base64url-pubkey-32-raw-bytes)."""
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, b64url(raw)


async def run_handshake(args: argparse.Namespace) -> int:
    priv, pub_b64 = make_peer_key()

    async with websockets.connect(args.url) as ws:
        # ---- challenge ----
        msg = json.loads(await ws.recv())
        if msg.get("type") != "challenge":
            print(f"FAIL  expected challenge, got: {msg}", file=sys.stderr)
            return 1
        nonce_b64 = msg["nonce"]
        nonce = b64url_decode(nonce_b64)
        print(f"  ← challenge nonce: {nonce_b64}")

        # ---- attest ----
        if args.invalid_signature:
            sig_bytes = priv.sign(b"wrong message")  # signs the wrong bytes
        else:
            sig_bytes = priv.sign(nonce)

        if args.invalid_pubkey:
            attest_pub = b64url(b"\x00" * 32)
        else:
            attest_pub = pub_b64

        attest = {
            "type": "attest",
            "member_id": str(uuid.uuid4()),
            "peer_public_key": attest_pub,
            "binary_hash": args.binary_hash,
            "client_version": args.client_version,
            "platform": args.platform,
            "hostname": args.hostname,
            "hardware_summary": {"smoke_test": True},
            "nonce_signature": b64url(sig_bytes),
        }
        print(f"  → attest (binary_hash={args.binary_hash[:16]}…, platform={args.platform})")
        await ws.send(json.dumps(attest))

        # ---- accept or reject ----
        try:
            response = json.loads(await asyncio.wait_for(ws.recv(), timeout=5.0))
        except asyncio.TimeoutError:
            print("FAIL  no response from coordinator within 5s", file=sys.stderr)
            return 1

        if response.get("type") == "accept":
            print(f"  ← accept peer_id={response['peer_id']}")
            print(f"           session_id={response['session_id']}")
            print(f"           heartbeat_interval_s={response['heartbeat_interval_s']}")
            print(f"           server_time={response['server_time']}")
            if args.expect == "reject":
                print("FAIL  expected reject, got accept", file=sys.stderr)
                return 1
            print("OK    handshake completed")
            return 0
        elif response.get("type") == "reject":
            print(f"  ← reject reason={response.get('reason')}")
            if args.expect == "accept":
                print("FAIL  expected accept, got reject", file=sys.stderr)
                return 1
            print("OK    handshake correctly rejected")
            return 0
        else:
            print(f"FAIL  unexpected response: {response}", file=sys.stderr)
            return 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--url", default="ws://localhost:8400/peers/connect")
    p.add_argument(
        "--binary-hash",
        required=True,
        help="SHA256 hex of a binary that exists in contributor_releases",
    )
    p.add_argument("--client-version", default="0.0.1-dev")
    p.add_argument("--platform", default="darwin-arm64")
    p.add_argument("--hostname", default="smoke-test")
    p.add_argument(
        "--expect",
        choices=["accept", "reject"],
        default="accept",
        help="expected outcome",
    )
    p.add_argument(
        "--invalid-signature",
        action="store_true",
        help="sign the wrong bytes — handshake should reject",
    )
    p.add_argument(
        "--invalid-pubkey",
        action="store_true",
        help="send a zero-bytes pubkey — coordinator can't verify, should reject",
    )
    args = p.parse_args()

    rc = asyncio.run(run_handshake(args))
    sys.exit(rc)


if __name__ == "__main__":
    main()
