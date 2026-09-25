"""Live, read-only model inventory for the owner's mesh.

Only responding, opted-in runtimes count as serving capacity. Device memory
is not a model-fit or speed prediction. No catalog guesses, downloads, leases,
cloud lookups or sharing changes occur during discovery.
"""
from __future__ import annotations

import asyncio
import ipaddress
import re
import time

import httpx

from .gateway_routing import household_peers, PEER_SIDECAR_PORT
from .runtime import get_runtime
from .split_manager import get_split_manager, SPLIT_PEER_PORT
from .split_native import PIN

MAX_PEERS = 32
PROBE_TIMEOUT = 2.0


def _model_id(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,199}", value) else None


def _digest(value):
    return value if isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) else None


def _positive(value):
    return value if type(value) is int and 0 < value < 2**53 else None


async def model_availability():
    runtime = get_runtime()
    local_model = _model_id(runtime.current_model_id()) if runtime.is_loaded() else None
    local_sha = _digest(runtime.current_model_sha256()) if local_model else None
    local = {"model": local_model, "sha256": local_sha}
    models = {}
    if local_model:
        models[(local_model, local_sha)] = {"id": local_model, "sha256": local_sha,
            "local": True, "machines": [], "idleMachineCount": 0}

    # Only use mesh addresses obtained from the local daemon, never remote
    # URLs/hostnames supplied in a peer capability response.
    roster = {}
    for peer in await household_peers():
        try:
            address = ipaddress.ip_address(peer["mesh_ip"])
            if address not in ipaddress.ip_network("100.64.0.0/10"):
                continue
        except (ValueError, KeyError, TypeError):
            continue
        roster[str(address)] = peer
    chosen = sorted(roster)[:MAX_PEERS]
    semaphore = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, trust_env=False, follow_redirects=False) as client:
        async def probe(ip, port, path):
            async with semaphore:
                try:
                    async with asyncio.timeout(PROBE_TIMEOUT):
                        async with client.stream("GET", f"http://{ip}:{port}{path}") as response:
                            if response.status_code != 200:
                                return None
                            data = bytearray()
                            async for chunk in response.aiter_bytes():
                                data.extend(chunk)
                                if len(data) > 65536:
                                    return None
                            import json
                            result = json.loads(data)
                            return result if isinstance(result, dict) else None
                except (httpx.HTTPError, ValueError, TimeoutError):
                    return None
        results = await asyncio.gather(*(asyncio.gather(
            probe(ip, PEER_SIDECAR_PORT, "/peer/capabilities"),
            probe(ip, SPLIT_PEER_PORT, "/peer/split/status")) for ip in chosen))

    # A peer removed during polling cannot remain advertised by this snapshot.
    current = {p.get("mesh_ip") for p in await household_peers()}
    machines = []
    local_split = get_split_manager().status()
    for ip, (cap, split) in zip(chosen, results):
        if ip not in current:
            continue
        peer = roster[ip]
        name = peer.get("node_name")
        name = name[:128] if isinstance(name, str) else ip
        engine = split.get("engine") if split else None
        compatible = isinstance(engine, dict) and engine.get("available") is True \
            and engine.get("commit") == PIN["commit"] and split.get("protocol") == PIN["protocol"]
        worker_ready = bool(compatible and split.get("available") is True)
        machine = {"name": name, "meshIp": ip, "responding": cap is not None or split is not None,
                   "splitReady": worker_ready, "splitCompatible": bool(compatible),
                   "backend": engine.get("backend") if compatible and engine.get("backend") in ("cpu", "metal", "cuda", "vulkan") else None,
                   "freeMemoryBytes": _positive(split.get("freeMemoryBytes")) if split else None}
        machines.append(machine)
        slug = _model_id(cap.get("loaded_model")) if cap else None
        if not slug or cap.get("available_for_inference") is not True:
            continue
        detail = cap.get("loaded_model_detail")
        sha = _digest(detail.get("sha256")) if isinstance(detail, dict) and cap.get("model_lock_version") == 1 else None
        key = (slug, sha)
        row = models.setdefault(key, {"id": slug, "sha256": sha, "local": False,
                                     "machines": [], "idleMachineCount": 0})
        load = cap.get("inference_load")
        idle = isinstance(load, dict) and load.get("busy") is False
        row["machines"].append({"name": name, "meshIp": ip, "idle": idle})
        row["idleMachineCount"] += int(idle)
    rows = sorted(models.values(), key=lambda row: (row["id"], row["sha256"] or ""))
    return {"schemaVersion": 1, "scope": "mine", "checkedAt": time.time(), "local": local,
            "models": rows, "machines": machines,
            "additionalModels": len({row["id"] for row in rows if row["id"] != local_model}),
            "splitWorkerCount": sum(m["splitReady"] for m in machines),
            "splitEngineAvailable": local_split["engine"]["available"],
            "truncated": len(roster) > MAX_PEERS,
            "note": "Ready models are loaded on responding runtimes. Split workers add potential capacity; exact placement is verified after loading. Availability can change before a request."}
