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

Capabilities: each machine's /peer/capabilities is read at the same time as the
others. A request for a model that no answering machine has asks the machines that
gave no reply once more, with a longer limit, before it is refused (see _search).
GET /v1/models never asks twice.

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
from collections.abc import Callable
from contextlib import aclosing

import httpx

from .inference_errors import CONTEXT_LENGTH_EXCEEDED
from .remote_stream import RemoteStreamError, collect_completion, openai_chunks
from .peer_liveness import watch_peer_stream

logger = logging.getLogger(__name__)

PEER_SIDECAR_PORT = 8321
_ROSTER_TIMEOUT_S = 2.0
_CAPS_TIMEOUT_S = 2.0
# The second ask, only for the machines that did not answer the first in time. A
# machine whose mesh service has just restarted can take longer than the first limit.
_CAPS_RETRY_TIMEOUT_S = 6.0
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


class HouseholdSilent(RuntimeError):
    """No household machine that answered has the model, and a reachable machine did
    not answer even when asked a second time, so the model may be on that machine.
    The text is fit to show a caller and does not name the machine."""

    def __init__(self, model: str) -> None:
        super().__init__(f"A household machine did not answer in time, so model '{model}' could not be found. "
                         "The request can be tried again.")


async def _read_caps(peers: list[dict], timeout: float) -> list[dict | None]:
    """Each machine's /peer/capabilities, asked at the same time, so one slow machine
    delays only its own answer. None for a machine that did not answer in time."""
    if not peers:
        return []
    async with httpx.AsyncClient(timeout=timeout) as client:
        return list(await asyncio.gather(*(_peer_caps(client, peer) for peer in peers)))


def _first_match(peers: list[dict], caps: list[dict | None], wanted: Callable[[dict], bool]) -> dict | None:
    for peer, cap in zip(peers, caps):
        if cap and wanted(cap):
            return {**peer, "_capabilities": cap}
    return None


async def _search(peers: list[dict], wanted: Callable[[dict], bool], model: str, *,
                  ask_again: bool) -> dict | None:
    """The first machine, in roster order, whose capabilities satisfy ``wanted``.

    A machine that did not answer in time is silent. A silent machine may have the
    model; a machine that answered without it, or that has no engine listening, does
    not. When nothing matches and some machines are silent, ``ask_again`` asks just
    those once more with a longer limit. If the model is still not found and one of
    them is still silent, ``HouseholdSilent`` is raised; None means every machine
    answered and none has the model.
    """
    caps = await _read_caps(peers, _CAPS_TIMEOUT_S)
    found = _first_match(peers, caps, wanted)
    silent = [i for i, cap in enumerate(caps) if cap is None]
    if found is not None or not silent or not ask_again:
        return found
    logger.info("household_caps_second_ask silent=%d asked=%d", len(silent), len(peers))
    for i, cap in zip(silent, await _read_caps([peers[i] for i in silent], _CAPS_RETRY_TIMEOUT_S)):
        caps[i] = cap
    found = _first_match(peers, caps, wanted)
    if found is None and any(cap is None for cap in caps):
        logger.warning("household_caps_unanswered silent=%d asked=%d", sum(1 for cap in caps if cap is None), len(peers))
        raise HouseholdSilent(model)
    return found


def listed_markers(cap: dict) -> frozenset[str]:
    """The tool-call markers a machine's capabilities say its engine proves (see
    services/tool_markers.py). Empty for an older machine or a malformed list."""
    listed = cap.get("tool_markers")
    return frozenset(m for m in listed if isinstance(m, str)) if isinstance(listed, list) else frozenset()


async def _replacement_peer(model: str, excluded: set[str], sha256: str | None = None, *,
                            tools: bool = False, markers: frozenset[str] | None = None,
                            ask_again: bool = False) -> dict | None:
    # Read current membership on every retry. Never route through the pool or
    # relax the requested model because an earlier household machine failed.
    # A request with tools only goes to a machine that says it supports them.
    # With markers, only to a machine that proves exactly those: the reply is
    # read with the first machine's markers (routers/gateway.py).
    # ask_again is for the first lookup of a request, which can refuse (see _search);
    # a retry in the middle of a stream does not wait for a silent machine.
    peers = [p for p in await household_peers() if p["mesh_ip"] not in excluded]

    def wanted(cap: dict) -> bool:
        return bool(cap.get("available_for_inference") and cap.get("loaded_model") == model and (
            not tools or cap.get("supports_tools") is True
        ) and (
            markers is None or listed_markers(cap) == markers
        ) and (
            sha256 is None or (cap.get("model_lock_version") == 1
                               and (cap.get("loaded_model_detail") or {}).get("sha256") == sha256)
        ))

    return await _search(peers, wanted, model, ask_again=ask_again)


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
    if os.path.exists("/run/potluck-mesh/potluck-mesh.sock"):
        return "/run/potluck-mesh/potluck-mesh.sock"
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
    """The machine's capabilities. None when it did not answer in time: it is silent,
    and may answer if asked again. An empty dict when it did answer and has nothing to
    offer: the connection was refused (no engine is listening there), the reply was
    not a 200, or it was not a JSON object. Asking such a machine again changes
    nothing, so it is not silent."""
    try:
        r = await client.get(f"http://{peer['mesh_ip']}:{PEER_SIDECAR_PORT}/peer/capabilities")
    except httpx.TimeoutException:
        return None
    except Exception:
        return {}
    if r.status_code != 200:
        return {}
    try:
        data = r.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def household_model_map() -> dict[str, dict]:
    """Map of {model_slug: peer} for models loaded on reachable peers that are
    serving inbound jobs (contributor mode on). Used for /v1/models aggregation.
    Excludes peers that have a model loaded but opted out. Reads every machine at
    the same time with the short limit and never asks a silent machine again, so
    it stays quick."""
    peers = await household_peers()
    out: dict[str, dict] = {}
    for peer, caps in zip(peers, await _read_caps(peers, _CAPS_TIMEOUT_S)):
        if not caps:
            continue
        slug = caps.get("loaded_model")
        if slug and caps.get("available_for_inference"):
            # First peer wins for a given slug (stable).
            out.setdefault(slug, {"node_name": peer["node_name"], "mesh_ip": peer["mesh_ip"], "_capabilities": caps})
    return out


async def peer_for_model(model: str, sha256: str | None = None) -> dict | None:
    """The household peer serving `model`, or None when every reachable machine
    answered and none has it. Raises HouseholdSilent when none that answered has it
    and a reachable machine still had not answered after a second ask."""
    if not model:
        return None
    return await _replacement_peer(model, set(), sha256, ask_again=True)


async def find_peer(model: str) -> tuple[dict | None, HouseholdSilent | None]:
    """``peer_for_model`` for a caller that may fall through to something else: the
    silent-machine case comes back as the second item and is not raised."""
    try:
        return await peer_for_model(model), None
    except HouseholdSilent as silent:
        return None, silent


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


async def _peer_chunks(peer: dict, body: dict, model: str, sha256: str | None):
    timeout = httpx.Timeout(None, connect=_DISPATCH_CONNECT_TIMEOUT_S,
                            write=_DISPATCH_CONNECT_TIMEOUT_S, pool=_DISPATCH_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as client:
        async with client.stream("POST", _peer_url(peer), json=body) as response:
            if response.status_code != 200:
                raise PeerDispatchError(response.status_code)
            if sha256 and (response.headers.get("x-potluck-model-lock") != "1"
                           or response.headers.get("x-potluck-model-sha256") != sha256):
                raise RemoteStreamError("Household runtime did not acknowledge the exact model lock; upgrade it.", "model_mismatch")
            if body.get("potluck_batch") and response.headers.get("x-potluck-batch-protocol") != "1":
                raise RemoteStreamError("Household runtime does not support complete batch results; upgrade it.", "model_mismatch")
            async with aclosing(openai_chunks(response, model, require_finish=body.get("potluck_batch", False))) as chunks:
                async for chunk in chunks:
                    yield chunk


async def stream_peer_openai(peer: dict, messages: list[dict], model: str,
                             temperature: float, max_tokens: int, *, sha256: str | None = None,
                             retry: bool = True, batch: bool = False,
                             tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                             stop: list[str] | None = None, tool_proof: bool = False):
    """Retry another household machine only while no output has been yielded.

    One absolute budget spans headers, prefill, every attempt and decoding.
    Cancelling the caller closes the HTTP stream and never triggers a retry.
    """
    body = copy.deepcopy(_payload(messages, model, temperature, max_tokens))
    if sha256:
        body["potluck_model_sha256"] = sha256
    if batch:
        body["potluck_batch"] = True
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice
        if tool_proof:
            body["potluck_tool_proof"] = True
    if stop:
        body["stop"] = stop
    # A proof request's reply is read with this machine's markers, so a retry needs the same ones.
    markers = listed_markers(peer.get("_capabilities") or {}) if tools and tool_proof else None
    loop = asyncio.get_running_loop()
    deadline = loop.time() + REQUEST_TIMEOUT_S
    excluded: set[str] = set()
    committed = False
    last_error: Exception = RemoteStreamError("No compatible household machine is available")
    # A machine that said the prompt does not fit. Another machine may have a larger
    # window, so the next one is still tried. If none can serve it, this is the error
    # to raise: a machine on an older release says "inference_error" for the same
    # thing, and the last attempt would hide what the first one found out.
    too_long: RemoteStreamError | None = None
    for attempt in range(MAX_DISPATCH_ATTEMPTS if retry else 1):
        try:
            if attempt:
                async with asyncio.timeout_at(deadline):
                    peer = await _replacement_peer(model, excluded, **({"sha256": sha256} if sha256 else {}),
                                                   **({"tools": True} if tools else {}),
                                                   **({"markers": markers} if markers is not None else {}))
                if peer is None:
                    break
            excluded.add(peer["mesh_ip"])
            rate = (peer.get("_capabilities", {}).get("loaded_model_detail") or {}).get("tokens_per_sec")
            first_budget = FIRST_TOKEN_TIMEOUT_S
            if isinstance(rate, (int, float)) and rate > 0:
                prompt_tokens = sum(len(m.get("content", "")) for m in body["messages"]) / 4
                first_budget = min(first_budget, 10 + prompt_tokens / rate)
            first_deadline = min(deadline, loop.time() + first_budget)
            # Reschedule from first-token to decode-idle only on useful output.
            # Role chunks, keepalives and malformed/no-op frames don't buy time.
            async with asyncio.timeout_at(first_deadline) as budget:
                guarded = watch_peer_stream(
                    _peer_chunks(peer, body, model, sha256),
                    f"http://{peer['mesh_ip']}:{PEER_SIDECAR_PORT}/health",
                )
                async with aclosing(guarded):
                    async for chunk in guarded:
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
            if isinstance(exc, RemoteStreamError) and exc.category == CONTEXT_LENGTH_EXCEEDED:
                too_long = exc
            if loop.time() >= deadline:
                break
    raise last_error if too_long is None else too_long


async def complete_peer_openai(peer: dict, messages: list[dict], model: str,
                               temperature: float, max_tokens: int, *, sha256: str | None = None,
                               tools: list[dict] | None = None, tool_choice: str | dict | None = None,
                               stop: list[str] | None = None, tool_proof: bool = False) -> dict:
    """Dispatch to a peer (streamed under the hood) and return one OpenAI
    chat.completion envelope."""
    return await collect_completion(
        stream_peer_openai(peer, messages, model, temperature, max_tokens, **({"sha256": sha256} if sha256 else {}),
                           **({"tools": tools, "tool_choice": tool_choice} if tools else {}),
                           **({"tool_proof": True} if tool_proof else {}),
                           **({"stop": stop} if stop else {}))
    )
