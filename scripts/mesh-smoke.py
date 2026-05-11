#!/usr/bin/env python3
"""mesh-smoke — Phase G mesh week-1 acceptance harness.

Drives the full mesh enrollment path end-to-end against a live
coordinator:

  1. POST /mesh/households          (idempotent on dev-stub)
  2. POST /mesh/pairing-tokens
  3. POST /mesh/enroll              (with the token + a fake WG pubkey)
  4. GET  /mesh/peers               (using the WG pubkey)
  5. POST /mesh/heartbeat           (update last_endpoint)

Exits 0 on full success. Stdlib-only — runs from any Python 3.11+
without pip.

Run from the repo root with the coordinator + Postgres up:
    make coordinator   # in another terminal
    python3 scripts/mesh-smoke.py
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import urllib.error
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
    print(f"  ok — household_id={household_id} mesh_cidr={mesh_cidr}")

    # ---- 2. Issue a pairing token ---- #
    print(f"→ POST {base}/mesh/pairing-tokens", flush=True)
    status, body = _http("POST", f"{base}/mesh/pairing-tokens")
    if status != 201:
        print(f"✗ token issue failed: {status} {body}")
        return 1
    token = body["token"]
    print(f"  ok — token={token[:8]}... expires_at={body['expires_at']}")

    # ---- 3. Enroll a node ---- #
    print(f"→ POST {base}/mesh/enroll", flush=True)
    wg_key = _fake_wg_key()
    status, body = _http(
        "POST",
        f"{base}/mesh/enroll",
        {
            "pairing_token": token,
            "wg_public_key": wg_key,
            "node_name": "smoke-laptop",
            "platform": "smoke",
        },
    )
    if status != 201:
        print(f"✗ enroll failed: {status} {body}")
        return 1
    node_id = body["node_id"]
    mesh_ip = body["mesh_ip"]
    roster = body["peers"]
    print(f"  ok — node_id={node_id} mesh_ip={mesh_ip} roster_size={len(roster)}")

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
    print(f"  ok — 409 as expected: {body.get('detail')}")

    # ---- 4. Fetch peer roster via wg_public_key ---- #
    print(f"→ GET  {base}/mesh/peers?wg_public_key=...", flush=True)
    import urllib.parse

    qs = urllib.parse.urlencode({"wg_public_key": wg_key})
    status, body = _http("GET", f"{base}/mesh/peers?{qs}")
    if status != 200:
        print(f"✗ peer roster fetch failed: {status} {body}")
        return 1
    print(f"  ok — roster has {len(body)} other peer(s)")

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
    print(f"  ok — server_time={body['server_time']}")

    print()
    print("✓ mesh week-1 acceptance: PASS")
    print(f"  household: {household_id}")
    print(f"  node:      {node_id} @ {mesh_ip}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
