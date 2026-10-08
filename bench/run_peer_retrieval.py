#!/usr/bin/env python3
"""Cross-machine peer retrieval bench.

Verifies that /peer/memory/search produces the same results across the mesh
as the underlying retriever does locally. Two probes:

  1. Correctness: populate the peer's memory store with known memories,
     query from this machine via /peer/memory/search, verify the returned
     memories match expectations and recall@10 is preserved.

  2. Latency at scale: measure cross-machine retrieval latency at multiple
     memory store sizes (10, 100, 500 memories on the peer) to find the
     scaling envelope.

Population uses SSH port-forwarding so the peer's loopback-only /memory/save
remains loopback-only — we tunnel Mac:<local_port> → Peer:8321 and POST
through the tunnel. Cleanup deletes the test memories from the peer.

Usage:
    python bench/run_peer_retrieval.py \\
        --peer-ssh-user rob \\
        --peer-mesh-ip 100.64.0.1 \\
        --peer-name linux
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[1]

# Use a unique project tag per run so we never collide with the user's
# real memories and cleanup is trivial.
_RUN_PROJECT = f"bench_peer_retrieval_{int(time.time())}"


# ---------------------------------------------------------------------------
# Test memory content — synthetic, semantically distinct, allows verifying
# that the right memory comes back for each query.
# ---------------------------------------------------------------------------
TEST_MEMORIES: list[dict[str, str]] = [
    {"q": "how to configure nginx reverse proxy", "memory": "Nginx reverse proxy configuration uses proxy_pass directive inside a location block. Set proxy_set_header Host and X-Forwarded-For for backend visibility."},
    {"q": "TCP keepalive tuning on Linux", "memory": "Linux TCP keepalive: tcp_keepalive_time sets idle seconds before first probe (default 7200), tcp_keepalive_intvl spacing between probes (default 75), tcp_keepalive_probes count (default 9)."},
    {"q": "Python asyncio event loop best practices", "memory": "Always use asyncio.run() to launch the top-level coroutine. Avoid mixing sync blocking calls inside an async coroutine; use asyncio.to_thread() for unavoidable blocking I/O."},
    {"q": "Postgres replication slot management", "memory": "Postgres replication slots prevent WAL recycling for active subscribers. Inactive slots can fill the disk; monitor pg_replication_slots.confirmed_flush_lsn and drop abandoned slots."},
    {"q": "Redis cluster failover behavior", "memory": "Redis cluster uses gossip-based failure detection. A node is marked PFAIL after node-timeout ms, then FAIL once majority agrees. Replicas auto-promote via election if the failed master had a replica."},
    {"q": "Kubernetes pod evicted reasons", "memory": "Pods get evicted for: node resource pressure (memory, disk, PID), node not ready (NodeLost), node tainted with NoExecute, or PriorityClass preemption."},
    {"q": "Docker layer caching invalidation", "memory": "Docker layer cache invalidates on any COPY/ADD source change, RUN command text change, or earlier-layer invalidation. Order layers from least- to most-frequently-changed to maximize cache hits."},
    {"q": "Go context cancellation propagation", "memory": "context.Context propagates cancellation down the call tree. Pass ctx as the first arg; check ctx.Done() in loops; never store contexts in structs (pass them through function calls)."},
    {"q": "TLS handshake debugging openssl", "memory": "openssl s_client -connect host:443 -showcerts dumps the cert chain. Add -servername host for SNI. -msg shows handshake records. Time-to-first-byte for the handshake is in -trace output."},
    {"q": "systemd unit auto-restart configuration", "memory": "systemd unit Restart= options: no (default), always, on-failure, on-abnormal, on-watchdog. Set RestartSec= to throttle; pair with StartLimitInterval and StartLimitBurst to prevent restart storms."},
]


# ---------------------------------------------------------------------------
# SSH port forwarding for population (peer loopback access)
# ---------------------------------------------------------------------------

class SSHTunnel:
    """Context manager that opens an SSH local-forward tunnel.

    Tunnels Mac:<local_port> → Peer:8321 so the peer's loopback-only
    /memory/save endpoint is reachable from Mac (the request reaches the
    peer's sidecar via 127.0.0.1, satisfying the peer_access_middleware
    loopback check).
    """

    def __init__(self, ssh_user: str, peer_ip: str, local_port: int = 18321):
        self.ssh_user = ssh_user
        self.peer_ip = peer_ip
        self.local_port = local_port
        self.proc: subprocess.Popen | None = None

    def __enter__(self):
        # -N: no remote command; -L: local forward; -o ServerAliveInterval keeps the tunnel alive
        cmd = [
            "ssh", "-N",
            "-L", f"{self.local_port}:127.0.0.1:8321",
            "-o", "ServerAliveInterval=10",
            "-o", "ExitOnForwardFailure=yes",
            f"{self.ssh_user}@{self.peer_ip}",
        ]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # Wait for the tunnel to be ready
        for _ in range(50):
            time.sleep(0.1)
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{self.local_port}/health", timeout=1).read()
                return self
            except Exception:
                continue
        raise RuntimeError(f"SSH tunnel to {self.peer_ip}:8321 via local {self.local_port} did not come up")

    def __exit__(self, *args):
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _post(url: str, body: dict, timeout: float = 30.0) -> tuple[int, dict | None]:
    """POST JSON, return (status, parsed_body_or_none)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, None
    except Exception as e:
        return 0, {"error": str(e)}


def _delete(url: str, timeout: float = 10.0) -> int:
    """DELETE, return status."""
    req = urllib.request.Request(url, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Populate / cleanup the peer's memory store
# ---------------------------------------------------------------------------

def populate_peer(tunnel_port: int, memories: list[dict[str, str]]) -> list[str]:
    """POST each memory to the peer's POST /memory endpoint (via SSH tunnel).
    Returns the list of created memory ids.
    """
    url = f"http://127.0.0.1:{tunnel_port}/memory"
    created_ids: list[str] = []
    for m in memories:
        status, body = _post(url, {
            "content": m["memory"],
            "category": "fact",
            "confidence": 1.0,
            "project": _RUN_PROJECT,
        })
        if status != 201 or not body:
            print(f"  warn: populate failed for memory (status={status} body={body})", file=sys.stderr)
            continue
        created_ids.append(body["id"])
    return created_ids


def cleanup_peer(tunnel_port: int, memory_ids: list[str]) -> int:
    """Delete the memories we created. Returns count deleted."""
    deleted = 0
    for mid in memory_ids:
        status = _delete(f"http://127.0.0.1:{tunnel_port}/memory/{mid}")
        if status in (200, 204):
            deleted += 1
    return deleted


# ---------------------------------------------------------------------------
# Cross-machine query via /peer/memory/search
# ---------------------------------------------------------------------------

def query_peer(peer_ip: str, query: str, limit: int = 10) -> tuple[int, list[dict] | None, float]:
    """POST /peer/memory/search to the peer's mesh IP. Returns (status, memories, latency_ms)."""
    t0 = time.perf_counter()
    status, body = _post(
        f"http://{peer_ip}:8321/peer/memory/search",
        {"query": query, "project": _RUN_PROJECT, "limit": limit},
        timeout=10.0,
    )
    elapsed_ms = (time.perf_counter() - t0) * 1000
    if status == 200 and body:
        return status, body.get("memories", []), elapsed_ms
    return status, None, elapsed_ms


# ---------------------------------------------------------------------------
# Bench probes
# ---------------------------------------------------------------------------

def probe_correctness(peer_ip: str, ssh_user: str) -> dict[str, Any]:
    """Populate the peer with 10 known memories, issue 10 queries (one per
    expected memory), measure recall@1 (top hit matches expected) and
    recall@3 (expected memory is in top 3).
    """
    print(f"[probe 1/2] correctness — populate {len(TEST_MEMORIES)} memories on peer, query, verify recall")

    with SSHTunnel(ssh_user, peer_ip) as tunnel:
        created = populate_peer(tunnel.local_port, TEST_MEMORIES)
        print(f"  populated {len(created)} memories (project={_RUN_PROJECT})")

        # Map memory_content -> memory_id for verification
        content_to_id: dict[str, str] = {}
        for m, mid in zip(TEST_MEMORIES, created):
            content_to_id[m["memory"]] = mid

        # Warm the embedder on the peer
        try:
            query_peer(peer_ip, "warm")
        except Exception:
            pass
        time.sleep(0.5)

        # Issue queries
        recall_at_1 = 0
        recall_at_3 = 0
        recall_at_10 = 0
        per_query: list[dict] = []

        for i, m in enumerate(TEST_MEMORIES):
            expected_id = created[i]
            status, returned, ms = query_peer(peer_ip, m["q"], limit=10)
            if status != 200 or returned is None:
                print(f"  query {i+1}: ERR status={status}")
                continue

            returned_ids = [r.get("id") for r in returned]
            hit_at_1 = expected_id == returned_ids[0] if returned_ids else False
            hit_at_3 = expected_id in returned_ids[:3]
            hit_at_10 = expected_id in returned_ids[:10]

            if hit_at_1: recall_at_1 += 1
            if hit_at_3: recall_at_3 += 1
            if hit_at_10: recall_at_10 += 1

            per_query.append({
                "q": m["q"],
                "expected_in_top1": hit_at_1,
                "expected_in_top3": hit_at_3,
                "expected_in_top10": hit_at_10,
                "latency_ms": round(ms, 2),
                "top_results": returned_ids[:5],
            })

        n = len(TEST_MEMORIES)
        result = {
            "n_queries": n,
            "recall_at_1": recall_at_1 / n,
            "recall_at_3": recall_at_3 / n,
            "recall_at_10": recall_at_10 / n,
            "per_query": per_query,
        }

        # Cleanup
        deleted = cleanup_peer(tunnel.local_port, created)
        result["cleanup_deleted"] = deleted
        print(f"  recall@1={result['recall_at_1']:.2f}  recall@3={result['recall_at_3']:.2f}  recall@10={result['recall_at_10']:.2f}")
        print(f"  cleaned up {deleted}/{len(created)} memories")
        return result


def probe_latency_at_scale(
    peer_ip: str, ssh_user: str, sizes: list[int]
) -> list[dict[str, Any]]:
    """For each store size, populate the peer with that many synthetic
    memories, then issue 30 warm queries from this machine and report
    p50/p95 latency. Cleanup between sizes.
    """
    print(f"[probe 2/2] latency at scale — sizes {sizes}")
    results = []

    # Generate a pool of synthetic memories large enough for the biggest size.
    rng = random.Random(42)
    pool: list[dict[str, str]] = []
    for i in range(max(sizes)):
        # Diverse semantic content; the query semantics will only match a few of these
        topic = rng.choice([
            "kubernetes pod scheduling", "redis cluster slot migration",
            "postgres vacuum frequency", "go goroutine leak detection",
            "python type hints generic", "rust ownership move semantics",
            "docker bridge networking", "terraform state locking",
            "nginx rate limiting", "apache cassandra compaction",
        ])
        pool.append({
            "q": f"how does {topic} work",
            "memory": f"Detail #{i}: {topic} involves coordinator-side decisions and is observable via metrics. "
                      f"Common pitfall is misconfiguring the timeout threshold. Random suffix {rng.randint(1000,9999)}.",
        })

    with SSHTunnel(ssh_user, peer_ip) as tunnel:
        for size in sizes:
            print(f"  size {size}: populating...")
            subset = pool[:size]
            created = populate_peer(tunnel.local_port, subset)
            time.sleep(0.3)

            print(f"  size {size}: querying 30 samples...")
            samples: list[float] = []
            query_text = "how does kubernetes pod scheduling work with multiple replicas"
            # Warm
            try: query_peer(peer_ip, query_text)
            except Exception: pass
            for _ in range(30):
                status, _, ms = query_peer(peer_ip, query_text, limit=10)
                if status == 200:
                    samples.append(ms)
                time.sleep(0.05)

            if samples:
                samples.sort()
                p50 = samples[len(samples)//2]
                p95 = samples[int(len(samples)*0.95)]
                results.append({
                    "store_size": size,
                    "n_samples": len(samples),
                    "p50_ms": round(p50, 2),
                    "p95_ms": round(p95, 2),
                    "min_ms": round(samples[0], 2),
                })
                print(f"  size {size}: p50={p50:.1f}ms  p95={p95:.1f}ms  min={samples[0]:.1f}ms")
            else:
                results.append({"store_size": size, "n_samples": 0, "error": "no successful samples"})

            print(f"  size {size}: cleanup...")
            cleanup_peer(tunnel.local_port, created)
            time.sleep(0.3)

    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Cross-machine peer retrieval bench")
    p.add_argument("--peer-mesh-ip", required=True, help="Peer's Potluck mesh IP, from this machine's mesh roster. The peer refuses any other address (Tailscale, LAN).")
    p.add_argument("--peer-ssh-user", required=True, help="SSH username for the peer (used to tunnel for populate)")
    p.add_argument("--peer-name", default="peer", help="Friendly name for output (e.g. 'linux', 'windows')")
    p.add_argument("--skip-latency-scale", action="store_true",
                   help="Skip the slower latency-at-scale probe (only run correctness check)")
    p.add_argument("--sizes", type=int, nargs="+", default=[10, 100, 500],
                   help="Store sizes to probe for the latency-at-scale run (default: 10 100 500). "
                        "Example: --sizes 1000 5000 10000")
    return p.parse_args()


def main():
    args = parse_args()
    started = time.perf_counter()

    print(f"=== Cross-machine peer retrieval bench: {args.peer_name} ({args.peer_mesh_ip}) ===")
    print(f"Project tag: {_RUN_PROJECT}")

    correctness = probe_correctness(args.peer_mesh_ip, args.peer_ssh_user)

    latency_at_scale: list[dict[str, Any]] = []
    if not args.skip_latency_scale:
        latency_at_scale = probe_latency_at_scale(
            args.peer_mesh_ip, args.peer_ssh_user, sizes=args.sizes,
        )

    wall_s = time.perf_counter() - started

    # Write results
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = _REPO_ROOT / "bench" / "results" / f"peer_retrieval_{args.peer_name}_{ts}"
    out_dir.mkdir(parents=True, exist_ok=True)

    results = {
        "peer_name": args.peer_name,
        "peer_mesh_ip": args.peer_mesh_ip,
        "project_tag": _RUN_PROJECT,
        "wall_time_s": round(wall_s, 1),
        "correctness": correctness,
        "latency_at_scale": latency_at_scale,
    }
    (out_dir / "results.json").write_text(json.dumps(results, indent=2))

    # Print summary
    print()
    print("=" * 72)
    print(f"SUMMARY — Cross-machine peer retrieval to {args.peer_name}")
    print("=" * 72)
    print(f"  Correctness: recall@1={correctness['recall_at_1']:.2f}  "
          f"recall@3={correctness['recall_at_3']:.2f}  "
          f"recall@10={correctness['recall_at_10']:.2f}  "
          f"(n={correctness['n_queries']} distinct queries)")
    if latency_at_scale:
        print(f"  Latency vs store size:")
        for r in latency_at_scale:
            if "p50_ms" in r:
                print(f"    {r['store_size']:5d} memories: p50={r['p50_ms']}ms  p95={r['p95_ms']}ms")
            else:
                print(f"    {r['store_size']:5d} memories: ERROR — {r.get('error')}")
    print(f"  Wall time: {wall_s:.1f}s")
    print(f"  Results: {out_dir / 'results.json'}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    sys.exit(main())
