#!/usr/bin/env python3
"""mesh-smoke - smoke test for the mesh control-plane.

Drives the full mesh control-plane path end-to-end against a live
coordinator:

  1. POST /mesh/households          (idempotent on dev-stub)
  2. POST /mesh/pairing-tokens
  3. POST /mesh/enroll              (with the token + a fake WG pubkey)
  4. GET  /mesh/peers               (using the WG pubkey)
  5. POST /mesh/heartbeat           (update last_endpoint)
  6. WS   /mesh/signal              hello → welcome → endpoint_update → ping/pong

Exits 0 on full success.

Run from the repo root with the coordinator + Postgres up:
    make coordinator   # in another terminal
    python3 scripts/dev/mesh-smoke.py

The WS step requires the `websockets` package; the HTTP-only portion
runs stdlib-only.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request


def _http(
    method: str,
    url: str,
    body: dict | None = None,
    *,
    timeout: float = 10.0,
) -> tuple[int, dict]:
    """Single-shot HTTP. Returns (status, parsed_json_or_empty_dict)."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            raw = resp.read().decode("utf-8")
            return status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8") if e.fp else ""
        try:
            return e.code, json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            return e.code, {"detail": raw}


def _fake_wg_key() -> str:
    """32 random bytes, base64-encoded, padded to 44 chars. Matches the
    shape of a real WireGuard public key without actually being one."""
    return base64.standard_b64encode(secrets.token_bytes(32)).decode("ascii")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coordinator-url",
        default=os.environ.get("COORDINATOR_URL", "http://127.0.0.1:8400"),
        help="Coordinator base URL (default: %(default)s).",
    )
    args = parser.parse_args()
    base = args.coordinator_url.rstrip("/")

    # ---- 1. Create or fetch the household ---- #
    print(f"→ POST {base}/mesh/households", flush=True)
    status, body = _http("POST", f"{base}/mesh/households", {"name": "smoke-test"})
    if status not in (200, 201):
        print(f"✗ household create failed: {status} {body}")
        return 1
    household_id = body["id"]
    mesh_cidr = body["mesh_cidr"]
    print(f"  ok - household_id={household_id} mesh_cidr={mesh_cidr}")

    # ---- 2. Issue a pairing token ---- #
    print(f"→ POST {base}/mesh/pairing-tokens", flush=True)
    status, body = _http("POST", f"{base}/mesh/pairing-tokens")
    if status != 201:
        print(f"✗ token issue failed: {status} {body}")
        return 1
    token = body["token"]
    print(f"  ok - token={token[:8]}... expires_at={body['expires_at']}")

    # ---- 3. Enroll a node ---- #
    print(f"→ POST {base}/mesh/enroll", flush=True)
    wg_key = _fake_wg_key()
    import time as _time

    unique_suffix = secrets.token_hex(3)
    status, body = _http(
        "POST",
        f"{base}/mesh/enroll",
        {
            "pairing_token": token,
            "wg_public_key": wg_key,
            "node_name": f"smoke-laptop-{unique_suffix}",
            "platform": "smoke",
        },
    )
    if status != 201:
        print(f"✗ enroll failed: {status} {body}")
        return 1
    node_id = body["node_id"]
    mesh_ip = body["mesh_ip"]
    roster = body["peers"]
    print(f"  ok - node_id={node_id} mesh_ip={mesh_ip} roster_size={len(roster)}")

    # ---- 3a. Re-enrolling with the same token must fail (409) ---- #
    print(f"→ POST {base}/mesh/enroll (duplicate token, expect 409)", flush=True)
    status, body = _http(
        "POST",
        f"{base}/mesh/enroll",
        {
            "pairing_token": token,
            "wg_public_key": _fake_wg_key(),
            "node_name": "smoke-laptop-2",
        },
    )
    if status != 409:
        print(f"✗ expected 409 on consumed token, got {status} {body}")
        return 1
    print(f"  ok - 409 as expected: {body.get('detail')}")

    # ---- 4. Fetch peer roster via wg_public_key ---- #
    print(f"→ GET  {base}/mesh/peers?wg_public_key=...", flush=True)
    import urllib.parse

    qs = urllib.parse.urlencode({"wg_public_key": wg_key})
    status, body = _http("GET", f"{base}/mesh/peers?{qs}")
    if status != 200:
        print(f"✗ peer roster fetch failed: {status} {body}")
        return 1
    print(f"  ok - roster has {len(body)} other peer(s)")

    # ---- 5. Heartbeat ---- #
    print(f"→ POST {base}/mesh/heartbeat", flush=True)
    status, body = _http(
        "POST",
        f"{base}/mesh/heartbeat",
        {
            "node_id": node_id,
            "wg_public_key": wg_key,
            "external_endpoint": "203.0.113.7:51820",
        },
    )
    if status != 200:
        print(f"✗ heartbeat failed: {status} {body}")
        return 1
    print(f"  ok - server_time={body['server_time']}")

    # ---- 6. Signaling WebSocket ---- #
    try:
        import websockets  # noqa: F401 - lazy import keeps the HTTP portion stdlib-only
    except ImportError:
        print()
        print("(skipping WS step - pip install websockets to exercise /mesh/signal)")
        print()
        print("✓ mesh acceptance (HTTP-only): PASS")
        return 0

    ws_base = base.replace("http://", "ws://").replace("https://", "wss://")
    ws_ok = asyncio.run(_ws_smoke(ws_base, wg_key))
    if not ws_ok:
        return 1

    print()
    print("✓ mesh acceptance (HTTP + WS): PASS")
    print(f"  household: {household_id}")
    print(f"  node:      {node_id} @ {mesh_ip}")
    return 0


async def _ws_smoke(ws_base: str, wg_key: str) -> bool:
    """Exercise the /mesh/signal protocol: hello → welcome →
    endpoint_update → ping/pong → clean close."""
    import websockets

    url = f"{ws_base}/mesh/signal"
    print(f"→ WS   {url}", flush=True)

    try:
        async with websockets.connect(url, open_timeout=5, close_timeout=2) as ws:
            # hello → welcome
            await ws.send(json.dumps({"type": "hello", "wg_public_key": wg_key}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") != "welcome":
                print(f"✗ expected welcome, got: {msg}")
                return False
            print(
                f"  welcome ok - node_id={msg['node_id']} "
                f"mesh_ip={msg['mesh_ip']} roster_size={len(msg['roster'])}"
            )

            # endpoint_update (no expected reply since we have no other peers)
            await ws.send(
                json.dumps(
                    {
                        "type": "endpoint_update",
                        "external_endpoint": "198.51.100.42:51820",
                    }
                )
            )
            print("  endpoint_update sent")

            # ping → pong
            await ws.send(json.dumps({"type": "ping"}))
            raw = await asyncio.wait_for(ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") != "pong":
                print(f"✗ expected pong, got: {msg}")
                return False
            print(f"  pong ok - server_time={msg['server_time']}")
        return True
    except Exception as e:
        print(f"✗ WS smoke failed: {type(e).__name__}: {e}")
        return False


if __name__ == "__main__":
    sys.exit(main())
