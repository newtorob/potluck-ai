"""Household mesh routing for the OpenAI/Ollama gateway.

Lets the gateway serve a request from one of the user's OWN machines over the
WireGuard mesh instead of only the local model. Routing is model-based and
predictable: the gateway advertises the union of models across the household
(local + each reachable peer's loaded model), and a request for a model that
isn't local is dispatched to the peer that has it loaded.

How it gets the roster: the Go mesh sidecar serves GET /peers over a unix socket
(mac/linux) or named pipe (windows). We read it directly. If the socket is
absent or unreadable (mesh not running, or Windows where we don't speak the
named pipe yet), household routing simply degrades to local-only.

Privacy: this stays entirely within the user's own machines over the mesh. The
coordinator is not involved on this path. Open-pool routing is a separate, later
slice.
"""

from __future__ import annotations

import asyncio
import copy
import logging
import os
import platform
import sys

import httpx

from .remote_stream import RemoteStreamError, collect_completion, openai_chunks

logger = logging.getLogger(__name__)

PEER_SIDECAR_PORT = 8321
_ROSTER_TIMEOUT_S = 2.0
_CAPS_TIMEOUT_S = 2.0
_DISPATCH_CONNECT_TIMEOUT_S = 5.0
MAX_DISPATCH_ATTEMPTS = 3
FIRST_TOKEN_TIMEOUT_S = 90.0
IDLE_TIMEOUT_S = 30.0
REQUEST_TIMEOUT_S = 240.0


class PeerDispatchError(RemoteStreamError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"Household machine could not serve the request (HTTP {status_code})",
                         "peer_unavailable")


async def _replacement_peer(model: str, excluded: set[str], sha256: str | None = None) -> dict | None:
    # Read current membership on every retry. Never route through the pool or
    # relax the requested model because an earlier household machine failed.
    peers = [p for p in await household_peers() if p["mesh_ip"] not in excluded]
    async with httpx.AsyncClient(timeout=_CAPS_TIMEOUT_S) as client:
        caps = await asyncio.gather(*(_peer_caps(client, peer) for peer in peers))
    for peer, cap in zip(peers, caps):
        if cap and cap.get("available_for_inference") and cap.get("loaded_model") == model and (
            sha256 is None or (cap.get("model_lock_version") == 1
                               and (cap.get("loaded_model_detail") or {}).get("sha256") == sha256)
        ):
            return {**peer, "_capabilities": cap}
    return None


def mesh_socket_path() -> str | None:
    """Resolve the Go mesh sidecar's local-API socket. Mirrors electron/main.js
    meshSocketPath(). Returns None on Windows (named pipe, not yet supported here)."""
    if sys.platform == "win32" or platform.system() == "Windows":
        return None  # named pipe; household routing degrades to local-only for now
    configured = os.environ.get("POTLUCK_LOCAL_API_SOCKET")
    if configured:
        return configured
    if sys.platform == "darwin":
        return "/tmp/potluck-mesh.sock"
    # The packaged Linux daemon uses a fixed socket. Match CLI discovery so
    # diagnostics and actual inference consult the same household roster.
    if os.path.exists("/run/potluck-mesh.sock"):
        return "/run/potluck-mesh.sock"
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, "potluck-mesh.sock")
    return f"/run/user/{os.getuid()}/potluck-mesh.sock"


async def household_peers() -> list[dict]:
    """Reachable peers from the mesh sidecar roster. [] on any failure (degrade)."""
    sock = mesh_socket_path()
    if not sock or not os.path.exists(sock):
        return []
    try:
        transport = httpx.AsyncHTTPTransport(uds=sock)
        async with httpx.AsyncClient(transport=transport, timeout=_ROSTER_TIMEOUT_S) as c:
            r = await c.get("http://localhost/peers")
            data = r.json()
        peers = data.get("peers", []) if isinstance(data, dict) else (data or [])
        return [
            {"node_name": p.get("node_name"), "mesh_ip": p.get("mesh_ip")}
            for p in peers
            if p.get("reachable") and p.get("mesh_ip")
        ]
    except Exception:
        logger.debug("household_roster_unavailable", exc_info=True)
        return []


async def _peer_caps(client: httpx.AsyncClient, peer: dict) -> dict | None:
    try:
        r = await client.get(f"http://{peer['mesh_ip']}:{PEER_SIDECAR_PORT}/peer/capabilities")
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


async def household_model_map() -> dict[str, dict]:
    """Map of {model_slug: peer} for models loaded on reachable peers that are
    serving inbound jobs (contributor mode on). Used for /v1/models aggregation
    and for routing. Excludes peers that have a model loaded but opted out."""
    peers = await household_peers()
    if not peers:
        return {}
    out: dict[str, dict] = {}
    async with httpx.AsyncClient(timeout=_CAPS_TIMEOUT_S) as c:
        for peer in peers:
            caps = await _peer_caps(c, peer)
            if not caps:
                continue
            slug = caps.get("loaded_model")
            if slug and caps.get("available_for_inference"):
                # First peer wins for a given slug (stable).
                out.setdefault(slug, {"node_name": peer["node_name"], "mesh_ip": peer["mesh_ip"], "_capabilities": caps})
    return out


async def peer_for_model(model: str, sha256: str | None = None) -> dict | None:
    """The household peer serving `model`, or None."""
    if not model:
        return None
    if sha256 is not None:
        return await _replacement_peer(model, set(), sha256)
    return (await household_model_map()).get(model)


def _peer_url(peer: dict) -> str:
    return f"http://{peer['mesh_ip']}:{PEER_SIDECAR_PORT}/peer/chat/completions"


def _payload(messages: list[dict], model: str, temperature: float, max_tokens: int, *, sha256: str | None = None) -> dict:
    return {
        "messages": messages,
        "model": model,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


async def stream_peer_openai(peer: dict, messages: list[dict], model: str,
                             temperature: float, max_tokens: int, *, sha256: str | None = None):
    """Retry another household machine only while no output has been yielded.

    One absolute budget spans headers, prefill, every attempt and decoding.
    Cancelling the caller closes the HTTP stream and never triggers a retry.
    """
    body = copy.deepcopy(_payload(messages, model, temperature, max_tokens))
    if sha256:
        body["potluck_model_sha256"] = sha256
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REQUEST_TIMEOUT_S
    excluded: set[str] = set()
    committed = False
    last_error: Exception = RemoteStreamError("No compatible household machine is available")
    for attempt in range(MAX_DISPATCH_ATTEMPTS):
        try:
            if attempt:
                async with asyncio.timeout_at(deadline):
                    peer = await _replacement_peer(model, excluded, **({"sha256": sha256} if sha256 else {}))
                if peer is None:
                    break
            excluded.add(peer["mesh_ip"])
            rate = (peer.get("_capabilities", {}).get("loaded_model_detail") or {}).get("tokens_per_sec")
            first_budget = FIRST_TOKEN_TIMEOUT_S
            if isinstance(rate, (int, float)) and rate > 0:
                prompt_tokens = sum(len(m.get("content", "")) for m in body["messages"]) / 4
                first_budget = min(first_budget, 10 + prompt_tokens / rate)
            first_deadline = min(deadline, loop.time() + first_budget)
            timeout = httpx.Timeout(None, connect=_DISPATCH_CONNECT_TIMEOUT_S,
                                    write=_DISPATCH_CONNECT_TIMEOUT_S, pool=_DISPATCH_CONNECT_TIMEOUT_S)
            # Reschedule from first-token to decode-idle only on useful output.
            # Role chunks, keepalives and malformed/no-op frames don't buy time.
            async with asyncio.timeout_at(first_deadline) as budget:
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", _peer_url(peer), json=body) as response:
                        if response.status_code != 200:
                            raise PeerDispatchError(response.status_code)
                        if sha256 and (response.headers.get("x-potluck-model-lock") != "1"
                                       or response.headers.get("x-potluck-model-sha256") != sha256):
                            raise RemoteStreamError("Household runtime did not acknowledge the exact model lock; upgrade it.", "model_mismatch")
                        async for chunk in openai_chunks(response, model):
                            choice = chunk["choices"][0]
                            delta = choice["delta"]
                            useful = any(value for key, value in delta.items() if key != "role")
                            finished = choice["finish_reason"] is not None
                            if not useful and not finished:
                                continue
                            committed = True
                            budget.reschedule(None if finished else min(deadline, loop.time() + IDLE_TIMEOUT_S))
                            yield chunk
                        return
        except (httpx.TransportError, RemoteStreamError, TimeoutError) as exc:
            if isinstance(exc, TimeoutError):
                exc = RemoteStreamError("Household inference stopped making progress", "stream_timeout")
            if committed:
                raise exc
            if isinstance(exc, PeerDispatchError) and exc.status_code not in (408, 409, 429, 500, 502, 503, 504):
                raise exc
            last_error = exc
            if loop.time() >= deadline:
                break
    raise last_error


async def complete_peer_openai(peer: dict, messages: list[dict], model: str,
                               temperature: float, max_tokens: int, *, sha256: str | None = None) -> dict:
    """Dispatch to a peer (streamed under the hood) and return one OpenAI
    chat.completion envelope."""
    return await collect_completion(
        stream_peer_openai(peer, messages, model, temperature, max_tokens, **({"sha256": sha256} if sha256 else {}))
    )
