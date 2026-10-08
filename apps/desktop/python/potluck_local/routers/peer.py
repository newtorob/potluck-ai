"""Peer-facing endpoints — household-scoped inference dispatch and (opt-in) memory aggregation.

The Python sidecar exposes a separate, **stateless** API surface for
requests coming from OTHER machines in the same household (via the
WireGuard mesh). Two categories of peer endpoint:

  Inference dispatch (always available):
    - GET  /peer/capabilities      — hardware advertisement
    - POST /peer/chat/completions  — stateless inference dispatch

  Memory aggregation (OPT-IN via POTLUCK_PEER_MEMORY_ENABLED=1):
    - POST /peer/memory/search     — read-only semantic search

Tasks, conversation history, and agent observability still stay strictly
on the local side. Memory writes also remain strictly local — the peer
memory endpoint is read-only.

Trust model:
  v0: the caller is a member of this machine's household. All of these must
  hold (see services/peer_trust.py and app.py's peer_access_middleware): the
  source IP is inside the WireGuard mesh CIDR (POTLUCK_MESH_CIDR, default
  100.64.0.0/10), this machine's own mesh sidecar lists it as a reachable
  peer, it is not this machine's own mesh address, and the connection was
  accepted on this machine's own mesh address. The CIDR alone is not enough:
  Tailscale and some carrier and campus networks use the same range, and the
  sidecar binds 0.0.0.0, so the bare socket is reachable from any other
  device on the same network. If the sidecar cannot be read, nobody is a
  member. Household membership is the credential. Signed-nonce challenge is
  a v0.5+ hardening item.

  For memory aggregation specifically: the env var gate exists because
  even within a household-trusted mesh, sharing memory by default would
  violate the privacy posture the rest of the sidecar enforces. Owners
  opt machines into participating; each machine decides independently.

Privacy invariants enforced here:
  * No memory injection on inference (no `_build_enhanced_messages` call).
  * No conversation persistence (no `chat_conversations` write).
  * No agent observability session (no `_get_chat_session_id` call).
  * No memory extraction trigger (no `_fire_extraction` task).
  * Memory search endpoint is read-only — no write paths reachable.
  * Memory search returns the same projection a local user would get
    via /memory/search; no extra metadata, no source attribution.
  * Request body MAY be logged in dev mode but content stays in-process.

Path access control: see `app.py`'s `peer_access_middleware`. Anything
that isn't /peer/* or /health gets 403 from non-loopback callers, and
/peer/* additionally 403s a non-loopback caller that is not a household
member. A non-member gets only {"status": "ok"} from /health.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from contextlib import aclosing
from typing import AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services.hardware_detect import detect_hardware
from potluck_local.services.runtime import get_runtime
from potluck_local.services.model_identity import ModelIdentityError, lock_headers
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
    sha256: str | None = None
    context_size: int | None = None
    tokens_per_sec: float | None = None  # last measured benchmark speed, if run


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
    protocol_version: int = 4
    model_lock_version: int = 1
    # Batch v1 preserves native finish reasons so token-limit truncation cannot
    # be mistaken for a complete durable result. Older chat peers remain valid.
    batch_protocol_version: int = 1

    # Additive fields (protocol_version >= 2)
    hardware: PeerHardware | None = None
    loaded_model_detail: PeerLoadedModel | None = None
    inference_load: PeerInferenceLoad | None = None

    # Big Model Mode (protocol_version >= 3)
    rpc_endpoint: str | None = None
    available_for_big_model: bool = False

    # Pipeline-parallel stage worker (protocol_version >= 4): true when this node can
    # serve a layer-block stage (tail/mid) for an upstream head -- contributor mode on
    # AND torch is installed. The WAN-viable distributed path; sibling of big-model RPC.
    # If a stage is already running, POST /peer/pipeline/stage returns 409 (the head
    # checks GET /peer/pipeline/stage); kept out of this hot capability poll to avoid
    # importing the torch-heavy engine module.
    available_for_pipeline: bool = False

    # Link type (protocol_version >= 4): this node's primary network link, self-detected
    # ('wired' | 'wifi' | 'unknown'). Pipeline-parallel round-trips through every hop on
    # EVERY token, so a WiFi peer (esp. a USB adapter) hurts the per-token critical path
    # with both higher latency and jitter; the orchestrator uses this to prefer wired,
    # low-jitter peers. Detected once + cached; torch-free so it stays on this hot path.
    link_type: str | None = None


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
        bench = runtime.benchmarks().get(loaded)
        loaded_detail = PeerLoadedModel(
            slug=loaded,
            sha256=runtime.current_model_sha256(),
            context_size=runtime.loaded_n_ctx(),
            tokens_per_sec=(bench or {}).get("tokens_per_sec"),
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

    # Pipeline-parallel eligibility: contributor mode on + torch installed. Probed via
    # find_spec so this hot capability poll never imports the torch-heavy engine module.
    import importlib.util
    available_for_pipeline = bool(
        contributor_on
        and importlib.util.find_spec("torch")
        and os.environ.get("POTLUCK_PIPELINE_ENABLED") == "1"
    )

    # Self-detected primary link type (wired/wifi/unknown). Cached after the first call;
    # the link_detect module is stdlib-only + torch-free, safe for this hot path. The
    # head reads this to deprioritize WiFi/jittery peers in pipeline-parallel placement.
    from potluck_local.services.link_detect import detect_link_type
    link_type = detect_link_type()

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
        available_for_pipeline=available_for_pipeline,
        link_type=link_type,
    )


# ---------------------------------------------------------------------------
# Pipeline-parallel stage worker
# ---------------------------------------------------------------------------
#
# This node serves a layer-block stage (tail or mid) for an UPSTREAM head running a model
# too big for any single peer. The head orchestrates the chain: it assigns each peer a
# contiguous block + a listen port (and, for a mid, the downstream tail). Reachable only
# from the mesh via the peer access middleware; gated on contributor mode. Scope:
# my-machines / trusted-circle (activation privacy unsolved -> never the open pool).


class PipelineStageRequest(BaseModel):
    """Ask this node to run a pipeline-parallel stage for an upstream head."""

    model_id: str  # HF model id every node in the chain loads
    role: Literal["tail", "mid"]
    split: int  # this stage's layer range derives from split/split2 + role (see load_parts)
    port: int  # the port this stage listens on for the upstream head/mid
    split2: int = 0
    device: str = ""  # cuda|mps|cpu|dml; autodetect if empty
    dtype: str | None = None  # must match every node in the chain
    peer2: str = ""  # mid only: downstream tail mesh IP
    port2: int = 0   # mid only: downstream tail port


def _to_torch_dtype(name: str | None):
    """Lazily map a dtype string to torch.dtype (None => engine default)."""
    if not name:
        return None
    import torch
    return getattr(torch, name)


def _pipeline_serving_enabled() -> bool:
    """Pipeline stage serving needs contributor mode AND an explicit opt-in
    (POTLUCK_PIPELINE_ENABLED=1). Starting a stage runs arbitrary model downloads + GPU
    load for a remote head -- a higher-impact action than read-path inference dispatch --
    so it is gated separately."""
    return bool(get_settings().network.contributor_mode) and os.environ.get("POTLUCK_PIPELINE_ENABLED") == "1"


@router.post("/pipeline/stage", status_code=200)
async def pipeline_stage_start(request: PipelineStageRequest) -> dict:
    """Start a pipeline stage on this node for an upstream head. 409 if one is already
    running (a node hosts one stage at a time); 503 unless pipeline serving is enabled."""
    if not _pipeline_serving_enabled():
        raise HTTPException(
            status_code=503,
            detail="Pipeline stage serving is not enabled here (needs contributor mode + POTLUCK_PIPELINE_ENABLED=1).",
        )
    # Lazy import: pulls torch + transformers, kept off the hot capability path.
    from potluck_local.services.pipeline_parallel import get_stage_worker

    worker = get_stage_worker()
    loop = asyncio.get_running_loop()
    try:
        info = await loop.run_in_executor(
            None,
            lambda: worker.start(
                request.model_id, role=request.role, split=request.split, port=request.port,
                split2=request.split2, device=request.device, dtype=_to_torch_dtype(request.dtype),
                peer2=request.peer2, port2=request.port2,
            ),
        )
    except RuntimeError as exc:  # already running, or failed to bind in time
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception as exc:
        logger.error("pipeline_stage_start_error error=%s", type(exc).__name__)
        raise HTTPException(status_code=500, detail=f"pipeline stage failed to start: {exc}")
    logger.info("pipeline_stage_started role=%s model_id=%s port=%s", request.role, request.model_id, request.port)
    return {"status": "started", **info}


@router.get("/pipeline/stage", status_code=200)
async def pipeline_stage_status() -> dict:
    """Status of this node's pipeline stage (running flag + role/model/port), if any."""
    from potluck_local.services.pipeline_parallel import get_stage_worker
    return get_stage_worker().status()


@router.delete("/pipeline/stage", status_code=200)
async def pipeline_stage_stop() -> dict:
    """Stop this node's pipeline stage and free the device (also used by the head's
    rollback path). 503 unless pipeline serving is enabled here."""
    if not _pipeline_serving_enabled():
        raise HTTPException(status_code=503, detail="Pipeline stage serving is not enabled here.")
    from potluck_local.services.pipeline_parallel import get_stage_worker
    worker = get_stage_worker()
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, worker.stop)


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
    potluck_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    potluck_batch: bool = Field(default=False, strict=True)
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

    digest = request.potluck_model_sha256
    if request.potluck_batch and (not digest or not request.stream):
        raise HTTPException(400, "Batch inference requires a model pin and streaming.")
    if digest:
        if not request.model:
            raise HTTPException(400, "A model ID is required with potluck_model_sha256.")
        try:
            runtime.assert_model_identity(request.model, digest)
        except ModelIdentityError as exc:
            raise HTTPException(409, str(exc)) from exc
    identity = {"expected_model": request.model or runtime.current_model_id(), "expected_sha256": digest}
    messages_for_runtime = [m.model_dump() for m in request.messages]

    logger.info(
        "peer_chat_request model=%s message_count=%d stream=%s",
        runtime.current_model_id(),
        len(messages_for_runtime),
        request.stream,
    )

    served_model = runtime.current_model_id()

    if request.potluck_batch:
        async def batch_stream():
            try:
                source = runtime.stream_chat_raw(messages_for_runtime, temperature=request.temperature,
                                                 max_tokens=request.max_tokens, stop=request.stop, **identity)
                async with aclosing(source):
                    async for chunk in source:
                        if "__error__" in chunk:
                            yield 'data: {"error":{"message":"Inference failed","category":"inference_error"}}\n\n'
                            return
                        yield f"data: {json.dumps(chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except ModelIdentityError:
                yield 'data: {"error":{"message":"Model identity changed","category":"model_mismatch"}}\n\n'

        return StreamingResponse(_served_metering_stream(batch_stream(), served_model), media_type="text/event-stream",
                                 headers={**lock_headers(digest), "X-Potluck-Batch-Protocol": "1",
                                          "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    if request.stream:
        upstream = runtime.stream_chat(
            messages=messages_for_runtime,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stop=request.stop,
            **identity,
        )
        # Pass through unmodified — no memory accumulation, no extraction. The
        # only thing recorded is a metadata-only usage COUNT (tokens + duration,
        # never content) for the local Usage dashboard's "served" side; the
        # serving node still remembers nothing about the request itself.
        return StreamingResponse(
            _served_metering_stream(upstream, served_model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                **lock_headers(digest),
            },
        )

    # Non-streaming path — collect the full response.
    started = time.monotonic()
    chunks: list[str] = []
    try:
        async for chunk in runtime.stream_chat(
            messages=messages_for_runtime,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stop=request.stop,
            **identity,
        ):
            chunks.append(chunk)
    except ModelIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    await _record_served(served_model, _count_content_chunks(chunks),
                         int((time.monotonic() - started) * 1000))
    # Same OpenAI-compatible non-streaming envelope the local chat
    # endpoint would emit. The runtime's chat() helper would do this
    # for us in a future refactor; for now we just stitch the stream.
    return JSONResponse({"raw_chunks": chunks}, headers=lock_headers(digest))


def _count_content_chunks(chunks: list[str]) -> int:
    """Output-token proxy: count non-empty SSE data chunks, ignoring the
    [DONE] sentinel. Coarse but metadata-only; good enough for usage stats."""
    return sum(1 for c in chunks if c and "[DONE]" not in c)


async def _record_served(model_slug: str, output_tokens: int, duration_ms: int) -> None:
    """Record a my_machines SERVED usage row (metadata only, no peer identity)."""
    try:
        from potluck_local.services.usage_store import record_usage_event
        await record_usage_event(
            direction="served",
            scope="my_machines",
            model_slug=model_slug,
            output_tokens=output_tokens,
            duration_ms=duration_ms,
            ok=True,
        )
    except Exception:
        logger.debug("usage_record_served_peer_failed", exc_info=True)


async def _served_metering_stream(
    upstream: AsyncGenerator[str, None],
    model_slug: str,
) -> AsyncGenerator[str, None]:
    """Yield each upstream SSE chunk unchanged while counting output tokens and
    wall-clock, then record one metadata-only 'served' usage row. No content is
    ever inspected or stored — only the count and duration."""
    started = time.monotonic()
    count = 0
    try:
        async with aclosing(upstream):
            async for chunk in upstream:
                if chunk and "[DONE]" not in chunk:
                    count += 1
                yield chunk
    except ModelIdentityError as exc:
        yield f'data: {json.dumps({"error": {"message": str(exc), "code": exc.category}})}\n\n'
        yield "data: [DONE]\n\n"
    finally:
        await _record_served(model_slug, count, int((time.monotonic() - started) * 1000))


# ---------------------------------------------------------------------------
# Memory search (own-fleet aggregation, opt-in)
# ---------------------------------------------------------------------------
#
# Read-only semantic search across this machine's memory store, exposed to
# household-mesh peers only when POTLUCK_PEER_MEMORY_ENABLED=1 is set on
# this machine. The endpoint mirrors /memory/search semantically (same
# retriever, same projection, same pinned-injection behaviour) but never
# triggers extraction, never writes, never opens an observability session.
#
# Trust model: mesh-IP reachability (peer_access_middleware already gates
# /peer/* paths to non-loopback callers). The env var is the user's
# explicit opt-in beyond mesh membership.

class PeerMemorySearchRequest(BaseModel):
    """Cross-machine memory search request from a household peer."""

    query: str = Field(..., min_length=1, max_length=4000)
    project: str | None = Field(default=None)
    limit: int = Field(default=10, ge=1, le=50)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class PeerMemorySearchResponse(BaseModel):
    """Response shape: list of memory dicts plus a count for cheap parsing."""

    memories: list[dict]
    count: int


def _peer_memory_enabled() -> bool:
    """Read POTLUCK_PEER_MEMORY_ENABLED on every call (no module-level cache).

    Reading per-request lets the user toggle the setting without restarting
    the sidecar, and avoids the test-isolation pain a cached module-level
    flag would introduce.
    """
    return os.environ.get("POTLUCK_PEER_MEMORY_ENABLED", "0") == "1"


@router.post("/memory/search", response_model=PeerMemorySearchResponse)
async def peer_memory_search(
    request: PeerMemorySearchRequest,
) -> PeerMemorySearchResponse:
    """Stateless semantic search for household peers (opt-in).

    Mirrors /memory/search (loopback-only) semantically. Trust = mesh
    membership; opt-in = POTLUCK_PEER_MEMORY_ENABLED=1.

    Side effects: none. No extraction, no access-log writes (the local
    /memory/search updates last_accessed_at; the peer endpoint deliberately
    skips that to avoid letting peers signal back into the local user's
    view of their own memory usage).
    """
    # Import here to avoid pulling memory services into the peer module's
    # import graph when memory aggregation is off. Keeps the inference-only
    # boot path fast for users who never enable peer memory.
    from potluck_local.routers.memory import _memory_active, _DISABLED_DETAIL
    from potluck_local.services.memory_retrieval import semantic_search

    if not _peer_memory_enabled():
        raise HTTPException(
            status_code=503,
            detail=(
                "Peer memory search is opt-in and disabled on this machine. "
                "Set POTLUCK_PEER_MEMORY_ENABLED=1 to enable household-fleet "
                "memory aggregation."
            ),
        )

    if not _memory_active():
        raise HTTPException(status_code=403, detail=_DISABLED_DETAIL)

    pairs = await semantic_search(
        query=request.query,
        project=request.project,
        limit=request.limit,
        min_confidence=request.min_confidence,
        include_pinned=True,
    )

    memories = [m.model_dump() for m, _ in pairs]

    logger.info(
        "peer_memory_search query_len=%d project=%s result_count=%d",
        len(request.query),
        request.project,
        len(memories),
    )

    return PeerMemorySearchResponse(memories=memories, count=len(memories))
