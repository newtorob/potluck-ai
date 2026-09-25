"""Owned-machine split sessions. Control, transport and native lifetime are bounded.

Native RPC binds only loopback. A leased, source-bound WebSocket carries its
bytes over the existing WireGuard mesh. No cloud dispatch or arbitrary URLs.
"""
from __future__ import annotations

import asyncio
from contextlib import suppress
import hmac
import ipaddress
import os
import secrets
import time
from uuid import uuid4

import httpx
import psutil
import websockets

from .gateway_routing import household_peers, PEER_SIDECAR_PORT
from .model_identity import artifact_identity
from .model_lifecycle import get_model_lifecycle
from .runtime import get_runtime
from .settings_manager import get_settings, save_settings
from .split_native import PIN, NativeProcess, SplitError, SplitModel, engine_bundle, free_port
from .split_memory import probe_fit_arguments

WORKER_KEY = "potluck.split_worker_enabled"
LEASE_SECONDS = 30
LOAD_SECONDS = 1200
# Separate validation/development installations may run the same sidecar on a
# different port. This is process configuration, never a request-supplied URL.
SPLIT_PEER_PORT = int(os.environ.get("POTLUCK_SPLIT_PEER_PORT", str(PEER_SIDECAR_PORT)))


async def paired_host(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
        if address.is_loopback or address.is_unspecified or address.is_multicast:
            return False
    except ValueError:
        return False
    return any(p["mesh_ip"] == host for p in await household_peers())


async def pipe_pair(first, second):
    tasks = [asyncio.create_task(first), asyncio.create_task(second)]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


class SplitManager:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.worker = None
        self.head = None
        self.state = "idle"
        self.error = None
        self.load_task = None
        self.monitor_task = None
        self.stopping = False

    def enabled(self):
        return get_settings().ui.get(WORKER_KEY) == "true"

    def start_monitor(self):
        if self.monitor_task is None:
            self.monitor_task = asyncio.create_task(self._monitor())

    def status(self):
        try:
            _, manifest = engine_bundle()
            engine = {"available": True, "commit": manifest["commit"], "backend": manifest["backend"]}
        except SplitError:
            engine = {"available": False, "commit": PIN["commit"]}
        runtime = get_runtime()
        return {"schemaVersion": 1, "protocol": PIN["protocol"], "engine": engine,
                "workerEnabled": self.enabled(), "workerBusy": self.worker is not None,
                "available": self.enabled() and engine["available"] and self.worker is None
                    and self.head is None and not runtime.is_loaded() and not self.stopping,
                "freeMemoryBytes": psutil.virtual_memory().available,
                "state": self.state, "error": self.error,
                "session": None if not self.head else {
                    "id": self.head["id"], "model": self.head["model"],
                    "context": self.head["context"],
                    "phase": self.head.get("phase", self.state),
                    "elapsedSeconds": round(max(0, self.head.get("endedMono", time.monotonic()) - self.head.get("startedMono", time.monotonic())), 1),
                    "loadSeconds": self.head.get("loadSeconds"),
                    "workers": [{"name": w["node_name"], "sentBytes": w["sent"], "receivedBytes": w["received"],
                                 "modelMiB": w.get("modelMiB", 0)}
                                for w in self.head["workers"]],
                    "placement": [{"device": p["device"], "modelMiB": p["modelMiB"]}
                                  for p in self.head["process"].placement] if self.head.get("process") else [],
                }}

    async def enable_worker(self, enabled: bool):
        async with self.lock:
            if enabled:
                engine_bundle()
                if get_runtime().is_loaded() or self.head:
                    raise SplitError("Unload the local model before enabling this computer as a split worker.", code="split_model_loaded")
            else:
                await self._stop_worker()
            settings = get_settings().model_copy(deep=True)
            settings.ui[WORKER_KEY] = "true" if enabled else "false"
            save_settings(settings)
            self.start_monitor()
        return self.status()

    async def acquire(self, host: str, request: dict):
        if not await paired_host(host):
            raise SplitError("Split workers accept only current owned mesh members.", 403)
        if request["commit"] != PIN["commit"]:
            raise SplitError("Both machines need the same split engine build.", code="split_engine_mismatch")
        async with self.lock:
            if self.worker and (self.worker["expires"] <= time.monotonic() or not self.enabled()):
                await self._stop_worker()
            if self.worker:
                if self.worker["host"] == host and self.worker["request_id"] == request["request_id"]:
                    return self._lease_response()
                raise SplitError("This split worker is reserved by another session.")
            if not self.status()["available"]:
                raise SplitError("Worker is disabled, busy or has no compatible engine.")
            if psutil.virtual_memory().available < 1024 ** 3:
                raise SplitError("Worker needs at least 1 GiB of available system memory.")
            root, _ = engine_bundle()
            async with get_model_lifecycle().command():
                if get_runtime().is_loaded():
                    raise SplitError("Unload this computer's local model before sharing split compute.")
                get_runtime()._split_worker_reserved = True
                port = free_port()
                try:
                    process = NativeProcess([str(root / "rpc-server"), "--host", "127.0.0.1", "--port", str(port)])
                except Exception:
                    get_runtime()._split_worker_reserved = False
                    raise SplitError("Could not start the packaged split worker.", 503) from None
            self.worker = {"host": host, "request_id": request["request_id"], "token": secrets.token_hex(32),
                           "expires": time.monotonic() + LEASE_SECONDS, "process": process,
                           "port": port, "connections": set()}
            self.start_monitor()
            return self._lease_response()

    def _lease_response(self):
        return {"lease": self.worker["token"], "ttl": LEASE_SECONDS,
                "freeMemoryBytes": psutil.virtual_memory().available}

    def authenticate_lease(self, host: str, token: str):
        worker = self.worker
        if not worker or worker["host"] != host or not hmac.compare_digest(worker["token"], token) \
                or worker["expires"] <= time.monotonic() or not self.enabled():
            raise SplitError("Split lease is missing, expired or belongs to another machine.", 403)
        return worker

    async def renew(self, host: str, token: str):
        if not await paired_host(host):
            raise SplitError("Machine is no longer paired.", 403)
        async with self.lock:
            worker = self.authenticate_lease(host, token)
            worker["process"].heartbeat()
            worker["expires"] = time.monotonic() + LEASE_SECONDS
        return {"ttl": LEASE_SECONDS}

    async def release(self, host: str, token: str):
        async with self.lock:
            self.authenticate_lease(host, token)
            await self._stop_worker()
        return {"released": True}

    async def _stop_worker(self):
        worker, self.worker = self.worker, None
        if worker:
            for task in tuple(worker["connections"]):
                task.cancel()
            await asyncio.gather(*worker["connections"], return_exceptions=True)
            await asyncio.to_thread(worker["process"].close)
            get_runtime()._split_worker_reserved = False

    async def worker_tunnel(self, websocket, token: str):
        host = websocket.client.host if websocket.client else ""
        if not await paired_host(host):
            raise SplitError("Machine is not paired.", 403)
        worker = self.authenticate_lease(host, token)
        if len(worker["connections"]) >= 8:
            raise SplitError("Too many split connections.", 429)
        task = asyncio.current_task()
        worker["connections"].add(task)
        writer = None
        try:
            await websocket.accept()
            deadline = time.monotonic() + 10
            while True:
                self.authenticate_lease(host, token)
                try:
                    reader, writer = await asyncio.open_connection("127.0.0.1", worker["port"])
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise SplitError("Native worker did not become ready.", 503)
                    await asyncio.sleep(.1)

            async def inbound():
                while True:
                    data = await websocket.receive_bytes()
                    if len(data) > 262144:
                        raise SplitError("Split frame is too large.", 413)
                    writer.write(data)
                    await writer.drain()

            async def outbound():
                while data := await reader.read(65536):
                    await websocket.send_bytes(data)

            await pipe_pair(inbound(), outbound())
        finally:
            worker["connections"].discard(task)
            if writer:
                writer.close()
                with suppress(Exception):
                    await writer.wait_closed()
            with suppress(Exception):
                await websocket.close()

    async def plan(self, model: dict, peer_names: list[str], context: int):
        engine_bundle()
        if len(set(peer_names)) != len(peer_names) or not 1 <= len(peer_names) <= 3:
            raise SplitError("Select one to three distinct owned workers.", 400)
        from potluck_local.routers.models import _safe_model_dir
        path = _safe_model_dir(model["id"]) / "model.gguf"
        try:
            identity = await asyncio.to_thread(artifact_identity, path)
        except OSError:
            raise SplitError("Install the pinned model on this head computer with potluck models sync.", code="split_model_missing") from None
        if identity[0] != model["sha256"] or identity[1][2] != model["sizeBytes"]:
            raise SplitError("Installed GGUF does not match potluck.lock.", code="split_pin_mismatch")
        roster = await household_peers()
        selected = []
        for name in peer_names:
            matches = [p for p in roster if p["node_name"] == name or p["mesh_ip"] == name]
            if len(matches) != 1:
                raise SplitError("Worker name is missing or ambiguous in the live mesh roster. Use its mesh IP.", code="split_peer_missing")
            if not await paired_host(matches[0]["mesh_ip"]):
                raise SplitError("Invalid worker address.", 403)
            selected.append(matches[0])
        if len({p["mesh_ip"] for p in selected}) != len(selected):
            raise SplitError("The same machine cannot be selected twice.", 400)
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            for peer in selected:
                try:
                    response = await client.get(self._url(peer, "/status"))
                    cap = response.json()
                    if response.status_code != 200 or not cap.get("available") \
                            or cap.get("engine", {}).get("commit") != PIN["commit"]:
                        raise ValueError()
                    peer["backend"] = cap["engine"].get("backend")
                except (httpx.HTTPError, ValueError, KeyError):
                    raise SplitError("A selected worker is unavailable. On it, unload any model and run potluck split worker on.", code="split_peer_unavailable") from None
        return {"model": model, "context": context, "workers": selected,
                "engineCommit": PIN["commit"], "note": "Capacity depends on available device memory. Placement is verified after loading; no speedup is promised."}

    @staticmethod
    def _url(worker, suffix):
        return f"http://{worker['mesh_ip']}:{SPLIT_PEER_PORT}/peer/split{suffix}"

    async def start(self, model, peer_names, context):
        async with self.lock:
            if self.head and self.state in ("failed", "stopped"):
                await self._cleanup_head(self.head)
                self.head = None
            if self.head and self.state in ("loading", "ready") and self.head["model"] == model \
                    and self.head["context"] == context and self.head.get("selection") == sorted(peer_names):
                return self.status()
            if self.head or self.worker or self.stopping:
                raise SplitError("Stop the current split before starting another.", code="split_busy")
            plan = await self.plan(model, peer_names, context)
            self.head = {**plan, "id": str(uuid4()), "process": None, "adapter": None,
                         "selection": sorted(peer_names), "startedMono": time.monotonic(), "phase": "reserving",
                         "workers": [{**p, "sent": 0, "received": 0, "lease": None, "server": None,
                                      "connections": set()} for p in plan["workers"]]}
            self.state, self.error = "loading", None
            self.start_monitor()
            self.load_task = asyncio.create_task(self._load(self.head))
        return self.status()

    async def _head_tunnel(self, worker, reader, writer):
        task = asyncio.current_task()
        worker["connections"].add(task)
        try:
            url = self._url(worker, "/tunnel").replace("http:", "ws:", 1)
            async with websockets.connect(url, extra_headers={"Authorization": "Bearer " + worker["lease"]},
                    compression=None, open_timeout=5, close_timeout=2,
                    ping_interval=5, ping_timeout=5, max_size=262144) as ws:
                async def outbound():
                    while data := await reader.read(65536):
                        worker["sent"] += len(data)
                        await ws.send(data)
                async def inbound():
                    async for data in ws:
                        if not isinstance(data, bytes):
                            raise SplitError("Invalid split frame.", 502)
                        worker["received"] += len(data)
                        writer.write(data)
                        await writer.drain()
                await pipe_pair(outbound(), inbound())
        except (Exception, asyncio.CancelledError):
            pass  # native EOF fails the head; health/lease monitor stops the group
        finally:
            worker["connections"].discard(task)
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    async def _load(self, head):
        try:
            async with asyncio.timeout(LOAD_SECONDS):
                async with httpx.AsyncClient(timeout=15, trust_env=False) as client:
                    for worker in head["workers"]:
                        response = await client.post(self._url(worker, "/lease"),
                            json={"commit": PIN["commit"], "request_id": head["id"]})
                        if response.status_code != 200:
                            raise SplitError("Could not reserve an owned worker.")
                        lease = response.json()
                        worker["lease"] = lease["lease"]
                        worker["freeMemoryBytes"] = lease.get("freeMemoryBytes")
                        worker["server"] = await asyncio.start_server(
                            lambda r, w, peer=worker: self._head_tunnel(peer, r, w), "127.0.0.1", 0)
                endpoints = [f"127.0.0.1:{w['server'].sockets[0].getsockname()[1]}" for w in head["workers"]]
                root, _ = engine_bundle()
                memory_args = await probe_fit_arguments(root, endpoints,
                    {str(w["server"].sockets[0].getsockname()[1]): w for w in head["workers"]})
                from potluck_local.routers.models import _safe_model_dir
                path = _safe_model_dir(head["model"]["id"]) / "model.gguf"
                port, key = free_port(), secrets.token_hex(32)
                async with get_model_lifecycle().command():
                    runtime = get_runtime()
                    if runtime.is_loaded():
                        raise SplitError("Unload the head's current model before starting a split.")
                    # Keep other model commands out until the new process has
                    # loaded and its original pinned file has been rechecked.
                    process = NativeProcess([str(root / "llama-server"), "--model", str(path),
                        "--host", "127.0.0.1", "--port", str(port), "--ctx-size", str(head["context"]),
                        "--threads", str(get_settings().runtime.threads or 4),
                        "--rpc", ",".join(endpoints), *memory_args, "--split-mode", "layer",
                        "--parallel", "1", "--no-warmup", "--no-webui",
                        # The pinned engine emits allocation records at level
                        # four. The supervisor forwards only numeric counters.
                        "--log-verbosity", "4"], env={"LLAMA_API_KEY": key})
                    head["process"] = process
                    head["phase"] = "loading"
                    loop = asyncio.get_running_loop()
                    def closed():
                        with suppress(RuntimeError):
                            loop.call_soon_threadsafe(lambda: asyncio.create_task(self._adapter_closed(head)))
                    adapter = SplitModel(process, port, key, closed)
                    head["adapter"] = adapter
                    async with httpx.AsyncClient(timeout=2, trust_env=False) as client:
                        while True:
                            process.heartbeat()
                            try:
                                response = await client.get(adapter.base + "/health", headers=adapter.headers)
                                if response.status_code == 200:
                                    break
                            except httpx.HTTPError:
                                pass
                            await asyncio.sleep(.25)
                    head["phase"] = "verifying"
                    identity = await asyncio.to_thread(artifact_identity, path)
                    if identity[0] != head["model"]["sha256"] or identity[1][2] != head["model"]["sizeBytes"]:
                        raise SplitError("The pinned model changed while loading.")
                    # RPC buffers plus actual transport activity prove remote
                    # placement; a silently local load does not count as split.
                    for worker in head["workers"]:
                        port = worker["server"].sockets[0].getsockname()[1]
                        worker["modelMiB"] = sum(p["modelMiB"] for p in process.placement
                                                 if p.get("rpcPort") == port and p["device"] == "RPC")
                    if not all(w["sent"] > min(65536, head["model"]["sizeBytes"] // 100)
                               and w["received"] > 0 and w["modelMiB"] > 0 for w in head["workers"]):
                        raise SplitError("Engine did not place work on every selected worker.")
                    await runtime.install_split_async(adapter, path, head["model"]["id"], identity, head["context"])
                    get_model_lifecycle().remember(None)
                    head["loadSeconds"] = round(time.monotonic() - head["startedMono"], 1)
                    head["phase"] = "ready"
                    self.state = "ready"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.error = str(exc) if isinstance(exc, SplitError) else "Split loading failed. Check workers, engine compatibility and free device memory."
            self.state = "failed"
            await self._cleanup_head(head)

    async def _adapter_closed(self, head):
        async with self.lock:
            if self.head is head and self.state == "ready":
                self.error = head["adapter"].failure
                self.state = "failed" if self.error else "stopped"
                await self._cleanup_head(head)

    async def stop(self):
        async with self.lock:
            head = self.head
            self.state = "stopping"
            if self.load_task and not self.load_task.done():
                self.load_task.cancel()
                await asyncio.gather(self.load_task, return_exceptions=True)
            if head:
                await self._cleanup_head(head)
            self.head = None
            self.state, self.error = "idle", None
        return self.status()

    async def _cleanup_head(self, head):
        head.setdefault("endedMono", time.monotonic())
        head["phase"] = self.state
        adapter = head.get("adapter")
        if adapter:
            await asyncio.to_thread(adapter.close)
            await get_runtime().release_split_async(adapter)
        elif head.get("process"):
            await asyncio.to_thread(head["process"].close)
        async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
            for worker in head["workers"]:
                if worker.get("server"):
                    worker["server"].close()
                    await worker["server"].wait_closed()
                tasks = list(worker["connections"])
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                if worker.get("lease"):
                    with suppress(Exception):
                        await client.delete(self._url(worker, "/lease"),
                                            headers={"Authorization": "Bearer " + worker["lease"]})
                    worker["lease"] = None

    async def _monitor(self):
        while not self.stopping:
            await asyncio.sleep(5)
            # Serialize monitoring with stop/start/worker changes. Otherwise
            # an old failed renewal can tear down a newly started session.
            async with self.lock:
                try:
                    if self.worker:
                        if self.worker["expires"] <= time.monotonic() or not self.enabled():
                            await self._stop_worker()
                        else:
                            self.worker["process"].heartbeat()
                    head = self.head
                    if head and self.state in ("loading", "ready"):
                        if head.get("adapter") and head["adapter"].closed:
                            self.error = head["adapter"].failure
                            self.state = "failed" if self.error else "stopped"
                            await self._cleanup_head(head)
                            continue
                        if head.get("process"):
                            head["process"].heartbeat()
                        async with httpx.AsyncClient(timeout=4, trust_env=False) as client:
                            for worker in head["workers"]:
                                if worker.get("lease"):
                                    response = await client.post(self._url(worker, "/lease/renew"),
                                        headers={"Authorization": "Bearer " + worker["lease"]})
                                    if response.status_code != 200:
                                        raise SplitError("A worker lease was lost.", 503)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    if self.worker:
                        await self._stop_worker()
                    if self.head and self.state in ("loading", "ready"):
                        if self.load_task and not self.load_task.done():
                            self.load_task.cancel()
                            await asyncio.gather(self.load_task, return_exceptions=True)
                        self.state, self.error = "failed", "A split worker or engine stopped responding. Start again after restoring the worker."
                        await self._cleanup_head(self.head)

    async def close(self):
        self.stopping = True
        if self.monitor_task:
            self.monitor_task.cancel()
            await asyncio.gather(self.monitor_task, return_exceptions=True)
        await self.stop()
        async with self.lock:
            await self._stop_worker()


_manager = None


def get_split_manager():
    global _manager
    if _manager is None:
        _manager = SplitManager()
    return _manager
