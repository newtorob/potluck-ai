"""Peer-facing endpoints — household-scoped inference dispatch.

The Python sidecar exposes a separate, **stateless** API surface for
inference requests coming from OTHER machines in the same household
(via the WireGuard mesh). The local user's memory, tasks, conversation
history, and agent observability stay strictly on the local side
(/chat/completions, /memory, /tasks, /agents); peer endpoints here
do pure inference and nothing else.

Trust model:
  v0 — request comes in on a non-loopback interface, which means the
  caller dialed our mesh IP, which means they're a paired household
  peer. Mesh-IP reachability is the credential. Signed-nonce challenge
  is a v0.5+ hardening item.

Privacy invariants enforced here:
  * No memory injection (no `_build_enhanced_messages` call).
  * No conversation persistence (no `chat_conversations` write).
  * No agent observability session (no `_get_chat_session_id` call).
  * No memory extraction trigger (no `_fire_extraction` task).
  * Request body MAY be logged in dev mode but content stays in-process.

Path access control: see `app.py`'s `peer_access_middleware`. Anything
that isn't /peer/* or /health gets 403 from non-loopback callers.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
from typing import AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services.hardware_detect import detect_hardware
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings

logger = logging.getLogger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Capabilities probe
# ---------------------------------------------------------------------------
#
# Schema evolution policy: ADDITIVE ONLY. The requesting peer is responsible
# for handling missing optional fields gracefully (e.g. a household running
# mixed protocol versions during a rolling update). When adding a field,
# default it to None and document the protocol_version bump in the file
# header. Removing or renaming a field requires a major protocol bump and
# parallel-support window.


class PeerHardware(BaseModel):
    """Static hardware advertisement. Detected once on first access and
    cached (hardware doesn't change at runtime). Used by the requesting
    peer to score this machine for capability-aware routing."""

    platform: str | None = None  # 'darwin' | 'linux' | 'windows'
    arch: str | None = None  # 'arm64' | 'x86_64' | etc.
    cpu_cores_physical: int | None = None
    cpu_cores_logical: int | None = None
    ram_total_gb: float | None = None
    gpu_name: str | None = None
    gpu_vram_gb: float | None = None
    recommended_tier: str | None = None  # 'low' | 'mid' | 'high'


class PeerLoadedModel(BaseModel):
    """Information about the currently-loaded model. None when no model
    is loaded — `PeerCapabilities.loaded_model` (the bare slug) stays
    populated as the canonical 'is something loaded' check; this struct
    is the extended detail."""

    slug: str
    context_size: int | None = None


class PeerInferenceLoad(BaseModel):
    """Live load signal. The requesting peer uses `busy` to deprioritise
    a peer that's already serving an inference, and `recent_completions`
    for tiebreaks in capability scoring."""

    busy: bool = False
    recent_completions: int | None = None  # last-N-minutes counter, optional


class PeerCapabilities(BaseModel):
    """What the local node can do for inbound household inference jobs.

    The frontend on a requesting machine reads this to decide whether
    to dispatch a job here. `available_for_inference` is true when:
      - contributor mode is on in settings
      - a model is currently loaded

    `loaded_model` is null when no model is loaded, otherwise the slug
    the runtime reports (matches `/models/{slug}/load` keys).

    Extended fields (protocol_version=2):
      - hardware: static hardware advertisement
      - loaded_model_detail: extended info about the loaded model
      - inference_load: live load signal (busy / recent_completions)

    Protocol_version=3 (Big Model Mode):
      - rpc_endpoint: where this peer's llama.cpp rpc-server listens
        (e.g. "100.64.0.6:50052"). Set when the peer is running rpc-server
        AND wants to contribute compute to household-wide Big Model Mode
        dispatches. The requesting orchestrator includes these endpoints
        in `rpc_servers` when calling its own /models/{slug}/load.
      - available_for_big_model: true when rpc_endpoint is advertised AND
        contributor mode is on. Distinct from available_for_inference
        which is about serving full inference locally.

    All extended fields are optional — older requesters parsing this
    payload should treat missing fields as None.
    """

    hostname: str
    loaded_model: str | None
    contributor_mode: bool
    available_for_inference: bool
    protocol_version: int = 3

    # Additive fields (protocol_version >= 2)
    hardware: PeerHardware | None = None
    loaded_model_detail: PeerLoadedModel | None = None
    inference_load: PeerInferenceLoad | None = None

    # Big Model Mode (protocol_version >= 3)
    rpc_endpoint: str | None = None
    available_for_big_model: bool = False


# Cache the hardware detect — it shells out to system tools (lspci, system_profiler,
# GPUtil) on first call. Hardware doesn't change at runtime, so we re-detect once
# per process lifetime. None means "not yet detected"; an empty dict would mean
# "detected nothing useful" which is a legitimate state.
_hardware_cache: dict | None = None


async def _cached_hardware() -> dict:
    """Return hardware info, detecting on first call via a thread-pool executor.

    `detect_hardware()` may shell out to system_profiler / GPUtil which can
    block for several seconds.  Off-loading to run_in_executor ensures the
    uvicorn event loop stays unblocked on that cold path.  The result is
    cached in `_hardware_cache` so every subsequent call is a fast dict
    lookup — the executor is never entered again.
    """
    global _hardware_cache
    if _hardware_cache is None:
        loop = asyncio.get_running_loop()
        try:
            _hardware_cache = await loop.run_in_executor(None, detect_hardware)
        except Exception as exc:
            logger.warning("peer_hardware_detect_failed err=%s", type(exc).__name__)
            _hardware_cache = {}
    return _hardware_cache


@router.get("/capabilities", response_model=PeerCapabilities)
async def peer_capabilities() -> PeerCapabilities:
    runtime = get_runtime()
    settings = get_settings()

    loaded = runtime.current_model_id() if runtime.is_loaded() else None
    contributor_on = bool(settings.network.contributor_mode)

    # Build PeerHardware from the cached detect_hardware() result. Fields
    # default to None when detection failed or didn't fill them in.
    hw_raw = await _cached_hardware()
    hw = PeerHardware(
        platform=hw_raw.get("platform"),
        arch=hw_raw.get("arch"),
        cpu_cores_physical=hw_raw.get("cpu_cores_physical"),
        cpu_cores_logical=hw_raw.get("cpu_cores_logical"),
        ram_total_gb=hw_raw.get("ram_total_gb"),
        gpu_name=hw_raw.get("gpu_name"),
        gpu_vram_gb=hw_raw.get("gpu_vram_gb"),
        recommended_tier=hw_raw.get("recommended_tier"),
    )

    loaded_detail: PeerLoadedModel | None = None
    if loaded:
        loaded_detail = PeerLoadedModel(
            slug=loaded,
            context_size=runtime.loaded_n_ctx(),
        )

    # "busy" reflects whether the runtime's inference lock is currently
    # held (i.e. an inference is in flight). asyncio.Lock.locked() returns
    # True while held; safe to call without acquiring.
    inference_load = PeerInferenceLoad(
        busy=runtime._inference_lock.locked(),  # noqa: SLF001 — intentional, single-process state
    )

    # Big Model Mode advertisement. The peer's rpc-server endpoint comes
    # from an env var the contributor setup script writes when it starts
    # rpc-server alongside the Python sidecar. Format: "mesh-ip:port"
    # (e.g. "100.64.0.6:50052"). Empty / unset = no rpc-server here.
    #
    # Why an env var instead of a setting: the rpc-server is a separate
    # process whose lifecycle is managed outside the Python sidecar; the
    # sidecar just advertises the fact that it's running. Future work
    # (potluck-parallel project) could bundle rpc-server with the sidecar
    # and manage it lifecycle-wise.
    rpc_endpoint = os.environ.get("POTLUCK_RPC_ENDPOINT", "").strip() or None
    available_for_big_model = bool(rpc_endpoint and contributor_on)

    return PeerCapabilities(
        hostname=socket.gethostname(),
        loaded_model=loaded,
        contributor_mode=contributor_on,
        available_for_inference=contributor_on and loaded is not None,
        hardware=hw,
        loaded_model_detail=loaded_detail,
        inference_load=inference_load,
        rpc_endpoint=rpc_endpoint,
        available_for_big_model=available_for_big_model,
    )


# ---------------------------------------------------------------------------
# Stateless chat completions
# ---------------------------------------------------------------------------


class PeerChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class PeerChatRequest(BaseModel):
    """Same wire shape as the local ChatRequest, MINUS conversation_id
    (peer endpoints don't persist anything) and MINUS the model field's
    server-side coercion logic (peer either has the requested model or
    it doesn't — caller has to retry elsewhere)."""

    messages: list[PeerChatMessage]
    model: str | None = Field(default=None)
    stream: bool = Field(default=True)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)
    stop: list[str] | None = None


@router.post("/chat/completions")
async def peer_chat_completions(request: PeerChatRequest) -> object:
    """Stateless OpenAI-compatible chat completions for household peers.

    The peer's local user gets none of the request, none of the response,
    and none of the inferred memories from this call. The request hits
    the runtime, the runtime streams back, and that's it.
    """
    runtime = get_runtime()

    if not runtime.is_loaded():
        raise HTTPException(
            status_code=503,
            detail="No model loaded on this peer. Load one via /models/{slug}/load first.",
        )

    if request.model is not None and request.model != runtime.current_model_id():
        raise HTTPException(
            status_code=409,
            detail=(
                f"Requested model '{request.model}' is not loaded on this peer "
                f"(loaded: '{runtime.current_model_id()}'). Caller should pick "
                f"another household peer or change the slug."
            ),
        )

    settings = get_settings()
    if not settings.network.contributor_mode:
        # Contributor mode off = explicit opt-out from serving inbound work.
        # Distinct from "no model loaded" — let the caller try another peer.
        raise HTTPException(
            status_code=503,
            detail="Peer is not in contributor mode (opted out of serving inbound jobs).",
        )

    messages_for_runtime = [m.model_dump() for m in request.messages]

    logger.info(
        "peer_chat_request model=%s message_count=%d stream=%s",
        runtime.current_model_id(),
        len(messages_for_runtime),
        request.stream,
    )

    if request.stream:
        upstream = runtime.stream_chat(
            messages=messages_for_runtime,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stop=request.stop,
        )
        # Pass through unmodified — no memory accumulation, no extraction,
        # no observability hooks. The runtime already emits OpenAI-format
        # SSE chunks the frontend's streamTokens() parser understands.
        return StreamingResponse(
            _passthrough_stream(upstream),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )

    # Non-streaming path — collect the full response.
    chunks: list[str] = []
    async for chunk in runtime.stream_chat(
        messages=messages_for_runtime,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
        stop=request.stop,
    ):
        chunks.append(chunk)
    # Same OpenAI-compatible non-streaming envelope the local chat
    # endpoint would emit. The runtime's chat() helper would do this
    # for us in a future refactor; for now we just stitch the stream.
    return {"raw_chunks": chunks}


async def _passthrough_stream(
    upstream: AsyncGenerator[str, None],
) -> AsyncGenerator[str, None]:
    """Yield each upstream SSE chunk unchanged. No accumulation, no
    side effects — the entire point of the peer endpoint is that the
    serving node remembers nothing about the request."""
    async for chunk in upstream:
        yield chunk
