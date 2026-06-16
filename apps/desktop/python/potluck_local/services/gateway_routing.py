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

import json
import logging
import os
import platform
import sys
import time
import uuid

import httpx

logger = logging.getLogger(__name__)

PEER_SIDECAR_PORT = 8321
_ROSTER_TIMEOUT_S = 2.0
_CAPS_TIMEOUT_S = 2.0
_DISPATCH_CONNECT_TIMEOUT_S = 5.0


def mesh_socket_path() -> str | None:
    """Resolve the Go mesh sidecar's local-API socket. Mirrors electron/main.js
    meshSocketPath(). Returns None on Windows (named pipe, not yet supported here)."""
    if sys.platform == "win32" or platform.system() == "Windows":
        return None  # named pipe; household routing degrades to local-only for now
    if sys.platform == "darwin":
        return "/tmp/potluck-mesh.sock"
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
                out.setdefault(slug, {"node_name": peer["node_name"], "mesh_ip": peer["mesh_ip"]})
    return out


async def peer_for_model(model: str) -> dict | None:
    """The household peer serving `model`, or None."""
    if not model:
        return None
    return (await household_model_map()).get(model)


def _peer_url(peer: dict) -> str:
    return f"http://{peer['mesh_ip']}:{PEER_SIDECAR_PORT}/peer/chat/completions"


def _payload(messages: list[dict], model: str, temperature: float, max_tokens: int) -> dict:
    return {
        "messages": messages,
        "model": model,
        "stream": True,
        "temperature": temperature,
        "max_tokens": max_tokens,
    }


def _delta_of(line: str) -> str | None:
    """Pull delta content out of one peer SSE line, or None for non-content lines."""
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if "error" in obj:
        raise RuntimeError(str(obj.get("error")))
    try:
        return obj["choices"][0].get("delta", {}).get("content")
    except (KeyError, IndexError, AttributeError):
        return None


async def stream_peer_openai(peer: dict, messages: list[dict], model: str,
                             temperature: float, max_tokens: int):
    """Dispatch to a peer and yield OpenAI chat.completion.chunk dicts."""
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    def chunk(delta: dict, finish: str | None) -> dict:
        return {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    timeout = httpx.Timeout(None, connect=_DISPATCH_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream("POST", _peer_url(peer),
                            json=_payload(messages, model, temperature, max_tokens)) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"peer returned {resp.status_code}: {body}")
            yield chunk({"role": "assistant"}, None)
            async for line in resp.aiter_lines():
                content = _delta_of(line)
                if content:
                    yield chunk({"content": content}, None)
            yield chunk({}, "stop")


async def complete_peer_openai(peer: dict, messages: list[dict], model: str,
                               temperature: float, max_tokens: int) -> dict:
    """Dispatch to a peer (streamed under the hood) and return one OpenAI
    chat.completion envelope."""
    parts: list[str] = []
    timeout = httpx.Timeout(None, connect=_DISPATCH_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream("POST", _peer_url(peer),
                            json=_payload(messages, model, temperature, max_tokens)) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise RuntimeError(f"peer returned {resp.status_code}: {body}")
            async for line in resp.aiter_lines():
                content = _delta_of(line)
                if content:
                    parts.append(content)
    text = "".join(parts)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
    }
