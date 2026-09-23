"""OpenAI-compatible gateway — let external tools use Potluck as a drop-in LLM.

Point any tool that speaks the OpenAI API (Cursor, Continue, aider, Open WebUI,
LiteLLM, the openai SDK) at  http://127.0.0.1:8321/v1  and it talks to this
machine's loaded model. This is the distribution on-ramp: fit existing tools
instead of requiring the native Potluck UI.

Endpoints:
  POST /v1/chat/completions  (streaming + non-streaming, native OpenAI shape)
  POST /v1/completions       (legacy text-completion; routes the same as chat/completions)
  GET  /v1/models            (installed models)
  POST /v1/embeddings        (local embedding; no peer/pool routing)

Deliberately thin and clean: no memory injection or conversation persistence
(those are desktop-app features on /chat). A faithful OpenAI provider.

Auth: the app-level middleware gates /v1 with POTLUCK_GATEWAY_API_KEY (a separate
opt-in key for external tools), NOT the internal sidecar token. See app.py.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Literal

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services import gateway_routing, pool_routing
from potluck_local.services.remote_stream import RemoteStreamError
from potluck_local.services.model_registry import list_models
from potluck_local.services.runtime import get_runtime
from potluck_local.services.model_identity import ModelIdentityError, lock_headers

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_MAX_TOKENS = 2048


class GatewayMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    messages: list[GatewayMessage]
    model: str | None = None
    potluck_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    stop: list[str] | str | None = None


def _normalise_stop(stop: list[str] | str | None) -> list[str] | None:
    if stop is None:
        return None
    return [stop] if isinstance(stop, str) else stop


@router.get("/models")
async def list_models_openai() -> dict:
    """OpenAI /v1/models: advertise installed models (by slug)."""
    runtime = get_runtime()
    created = int(time.time())
    data: list[dict] = []
    seen: set[str] = set()
    try:
        for m in list_models():
            if not m.get("installed"):
                continue
            slug = m.get("slug")
            if slug and slug not in seen:
                seen.add(slug)
                data.append({"id": slug, "object": "model", "created": created, "owned_by": "potluck"})
    except Exception:
        logger.exception("gateway_list_models_failed")
    # Always include the currently loaded model, even if the catalog lookup missed it.
    cur = runtime.current_model_id()
    if cur and cur not in seen:
        seen.add(cur)
        data.append({"id": cur, "object": "model", "created": created, "owned_by": "potluck"})
    # Household peers' loaded models, so a tool sees the whole mesh's models and
    # can pick one that lives on another machine.
    try:
        for slug in (await gateway_routing.household_model_map()).keys():
            if slug not in seen:
                seen.add(slug)
                data.append({"id": slug, "object": "model", "created": created, "owned_by": "potluck-household"})
    except Exception:
        logger.exception("gateway_household_models_failed")
    # Open-pool models (other members' machines), only when the user opted in.
    # Lets a tool discover and request a model that lives only in the pool.
    if pool_routing.pool_enabled():
        try:
            for slug in await pool_routing.pool_model_slugs():
                if slug not in seen:
                    seen.add(slug)
                    data.append({"id": slug, "object": "model", "created": created, "owned_by": "potluck-pool"})
        except Exception:
            logger.exception("gateway_pool_models_failed")
    return {"object": "list", "data": data}


@router.get("/potluck/capabilities")
async def model_lock_capabilities() -> dict:
    return {"model_lock_version": 1, "scopes": ["local", "mine"], "artifact_format": "gguf"}


async def _locked_route(runtime, model: str | None, digest: str, scope: str):
    if not model:
        raise HTTPException(400, "A model ID is required with potluck_model_sha256.")
    if scope not in ("local", "mine"):
        raise HTTPException(400, "Project model locks require explicit local or mine scope; circle/pool are not supported.")
    try:
        runtime.assert_model_identity(model, digest)
        return None
    except ModelIdentityError as exc:
        if scope == "mine":
            peer = await gateway_routing.peer_for_model(model, digest)
            if peer:
                return peer
        raise HTTPException(409, str(exc)) from exc


class ReadinessRequest(BaseModel):
    model: str = Field(min_length=1, max_length=200)
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    scope: Literal["local", "mine"] = "local"


@router.post("/potluck/readiness")
async def model_readiness(req: ReadinessRequest) -> dict:
    """Read-only route check using the same exact-artifact gate as inference.

    A snapshot, not a reservation: inference rechecks under admission. No prompt
    is sent and no model is loaded. Household details stay inside the router.
    """
    route = None
    try:
        peer = await _locked_route(get_runtime(), req.model, req.sha256, req.scope)
        route = "household" if peer else "local"
    except HTTPException as exc:
        if exc.status_code != 409:
            raise
    return {"schemaVersion": 1, "model_lock_version": 1, "ready": route is not None,
            "model": req.model, "scope": req.scope, "route": route}


@router.post("/chat/completions")
async def chat_completions_openai(
    req: ChatCompletionRequest,
    x_potluck_scope: str | None = Header(default=None),
):
    """OpenAI /v1/chat/completions. Serves the local model, or routes to a
    household peer (your own machine over the mesh) that has the requested model
    loaded. Routing is by model id: local first, then a reachable peer.

    X-Potluck-Scope controls the routing tier for this single request:
      "local"  — this node only; 503 if no local model is loaded.
      "mine"   — local first, then household peer; pool never used.
      absent / any other value — default: local -> household -> pool-if-enabled.
    """
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    # Resolve the route. The X-Potluck-Scope header narrows which tiers are
    # eligible for this request. Absent or unrecognised values keep the existing
    # local -> household -> pool-if-enabled fall-through unchanged.
    scope = (x_potluck_scope or "").lower().strip()
    peer = None
    use_pool = False
    # coordinator_scope is the scope string sent in the job body when use_pool is
    # True. "pool" = open pool (strangers, single-turn by default); "circle" =
    # trusted members (always multi-turn).
    coordinator_scope = pool_routing._POOL_SCOPE

    digest = req.potluck_model_sha256
    identity = {"expected_model": req.model or local, "expected_sha256": digest}
    peer_identity = {"sha256": digest} if digest else {}
    if digest:
        peer = await _locked_route(runtime, req.model, digest, scope)
    elif scope == "local":
        # This node only. Fail immediately — no fall-through to peer or pool.
        if local is None:
            raise HTTPException(
                status_code=503,
                detail="No local model loaded; load one or retry with X-Potluck-Scope: mine",
            )
        if req.model and req.model != local:
            raise HTTPException(409, "Requested model is not loaded locally. Load that model or use mine scope.")
    elif scope == "mine":
        # Local first, then household peer. Pool is never reached.
        if req.model and req.model != local:
            peer = await gateway_routing.peer_for_model(req.model)
            if peer is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"model '{req.model}' is not loaded here or on a reachable household peer",
                )
        if peer is None and local is None:
            raise HTTPException(
                status_code=503,
                detail="No model loaded on this node, and no household peer matched.",
            )
    elif scope == "circle":
        # Trusted-circle routing: go directly to the coordinator with circle scope
        # (full messages always sent — trusted members). Requires coordinator URL.
        if not pool_routing.pool_available():
            raise HTTPException(
                status_code=503,
                detail="circle routing requires a configured coordinator URL",
            )
        use_pool = True
        coordinator_scope = pool_routing._CIRCLE_SCOPE
    else:
        # Default: local -> household peer -> open pool (if opted in). Existing
        # behavior preserved byte-for-byte when the header is absent.
        if req.model and req.model != local:
            peer = await gateway_routing.peer_for_model(req.model)
            if peer is None:
                if pool_routing.pool_enabled():
                    use_pool = True
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=f"model '{req.model}' is not loaded here or on a reachable household peer",
                    )
        if not use_pool and peer is None and local is None:
            raise HTTPException(
                status_code=503,
                detail="No model loaded on this node, and no household peer matched.",
            )

    messages = [m.model_dump() for m in req.messages]
    max_tokens = req.max_tokens or _DEFAULT_MAX_TOKENS
    stop = _normalise_stop(req.stop)
    served_model = req.model if (peer or use_pool) else local
    if use_pool:
        served_by = coordinator_scope  # "pool" or "circle"
    elif peer:
        served_by = "household"
    else:
        served_by = "local"

    logger.info(
        "gateway_chat served_by=%s model=%s messages=%d stream=%s",
        served_by, served_model, len(messages), req.stream,
    )

    if req.stream:
        async def event_stream():
            try:
                if use_pool:
                    # pool_routing applies the multi-turn privacy policy:
                    # circle always sends full messages; pool sends full messages
                    # only when pool_multiturn is explicitly opted in; otherwise
                    # only the last user message (prompt) crosses the wire.
                    async for chunk in pool_routing.stream_pool_openai(
                        messages, served_model, req.temperature, max_tokens,
                        scope=coordinator_scope,
                    ):
                        yield f"data: {json.dumps(chunk)}\n\n"
                elif peer:
                    async for chunk in gateway_routing.stream_peer_openai(
                        peer, messages, served_model, req.temperature, max_tokens, **peer_identity
                    ):
                        yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    async for chunk in runtime.stream_chat_raw(
                        messages, temperature=req.temperature, max_tokens=max_tokens, stop=stop, **identity
                    ):
                        if isinstance(chunk, dict) and "__error__" in chunk:
                            yield f'data: {json.dumps({"error": {"message": chunk["__error__"], "type": "inference_error"}})}\n\n'
                            break
                        yield f"data: {json.dumps(chunk)}\n\n"
                # Terminator on normal completion. Not in finally: a client
                # disconnect aclose()s this generator (GeneratorExit), and
                # yielding during that raises and can wedge the stream.
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gateway_stream_failed")
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error", "code": getattr(exc, "category", "inference_error")}})}\n\n'
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            _metered_consumed(event_stream(), _canonical_scope(served_by), served_model),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-Potluck-Served-By": served_by, **lock_headers(digest),
            },
        )

    # Non-streaming.
    try:
        if use_pool:
            result = await pool_routing.complete_pool_openai(
                messages, served_model, req.temperature, max_tokens,
                scope=coordinator_scope,
            )
        elif peer:
            result = await gateway_routing.complete_peer_openai(
                peer, messages, served_model, req.temperature, max_tokens, **peer_identity
            )
        else:
            result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens, **identity)
            if isinstance(result, dict):
                result.setdefault("model", local)
    except ModelIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RemoteStreamError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.category, "message": str(exc)}) from exc
    except pool_routing.PoolDispatchError as exc:
        # Surface the coordinator's real status when it's meaningful (e.g. 503
        # "no peer serves this model" — same condition as a local 404); any other
        # upstream failure is a bad-gateway (502).
        status = exc.status_code if exc.status_code in (404, 503) else 502
        raise HTTPException(status_code=status, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("gateway_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    _usage = result.get("usage", {}) if isinstance(result, dict) else {}
    await _record_consumed(
        _canonical_scope(served_by), served_model,
        input_tokens=int(_usage.get("prompt_tokens", 0) or 0),
        output_tokens=int(_usage.get("completion_tokens", 0) or 0),
    )
    return JSONResponse(result, headers={"X-Potluck-Served-By": served_by, **lock_headers(digest)})


# ---------------------------------------------------------------------------
# POST /v1/completions  — OpenAI legacy text-completion
# ---------------------------------------------------------------------------

class CompletionRequest(BaseModel):
    model: str | None = None
    potluck_model_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    prompt: str | list[str] = ""
    temperature: float = 0.7
    max_tokens: int | None = None
    stream: bool = False
    stop: list[str] | str | None = None


def _prompt_to_message(prompt: str | list[str]) -> str:
    """Collapse a prompt (string or string[]) to a single string."""
    if isinstance(prompt, list):
        return "\n".join(prompt)
    return prompt


@router.post("/completions")
async def text_completions(
    req: CompletionRequest,
    x_potluck_scope: str | None = Header(default=None),
):
    """OpenAI legacy /v1/completions. Converts prompt → user message and reuses
    the same local→household→pool routing path as /v1/chat/completions. Returns
    an OpenAI text_completion envelope (object "text_completion", choices[].text).

    Honours X-Potluck-Scope with the same semantics as /v1/chat/completions.
    """
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    scope = (x_potluck_scope or "").lower().strip()
    peer = None
    use_pool = False
    coordinator_scope = pool_routing._POOL_SCOPE

    digest = req.potluck_model_sha256
    identity = {"expected_model": req.model or local, "expected_sha256": digest}
    peer_identity = {"sha256": digest} if digest else {}
    if digest:
        peer = await _locked_route(runtime, req.model, digest, scope)
    elif scope == "local":
        if local is None:
            raise HTTPException(
                status_code=503,
                detail="No local model loaded; load one or retry with X-Potluck-Scope: mine",
            )
        if req.model and req.model != local:
            raise HTTPException(409, "Requested model is not loaded locally. Load that model or use mine scope.")
    elif scope == "mine":
        if req.model and req.model != local:
            peer = await gateway_routing.peer_for_model(req.model)
            if peer is None:
                raise HTTPException(
                    status_code=404,
                    detail=f"model '{req.model}' is not loaded here or on a reachable household peer",
                )
        if peer is None and local is None:
            raise HTTPException(
                status_code=503,
                detail="No model loaded on this node, and no household peer matched.",
            )
    elif scope == "circle":
        if not pool_routing.pool_available():
            raise HTTPException(
                status_code=503,
                detail="circle routing requires a configured coordinator URL",
            )
        use_pool = True
        coordinator_scope = pool_routing._CIRCLE_SCOPE
    else:
        if req.model and req.model != local:
            peer = await gateway_routing.peer_for_model(req.model)
            if peer is None:
                if pool_routing.pool_enabled():
                    use_pool = True
                else:
                    raise HTTPException(
                        status_code=404,
                        detail=f"model '{req.model}' is not loaded here or on a reachable household peer",
                    )
        if not use_pool and peer is None and local is None:
            raise HTTPException(
                status_code=503,
                detail="No model loaded on this node, and no household peer matched.",
            )

    prompt_text = _prompt_to_message(req.prompt)
    messages = [{"role": "user", "content": prompt_text}]
    max_tokens = req.max_tokens or _DEFAULT_MAX_TOKENS
    stop = _normalise_stop(req.stop)
    served_model = req.model if (peer or use_pool) else local
    if use_pool:
        served_by = coordinator_scope
    elif peer:
        served_by = "household"
    else:
        served_by = "local"

    cid = "cmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    logger.info(
        "gateway_completions served_by=%s model=%s stream=%s",
        served_by, served_model, req.stream,
    )

    if req.stream:
        async def event_stream():
            finish_reason = "stop"
            usage = None
            try:
                if use_pool:
                    source = pool_routing.stream_pool_openai(
                        messages, served_model, req.temperature, max_tokens,
                        scope=coordinator_scope,
                    )
                elif peer:
                    source = gateway_routing.stream_peer_openai(
                        peer, messages, served_model, req.temperature, max_tokens, **peer_identity
                    )
                else:
                    source = runtime.stream_chat_raw(
                        messages, temperature=req.temperature, max_tokens=max_tokens, stop=stop, **identity
                    )
                async for chunk in source:
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield f'data: {json.dumps({"error": {"message": chunk["__error__"], "type": "inference_error"}})}\n\n'
                        break
                    if chunk.get("choices") and chunk["choices"][0].get("finish_reason"):
                        finish_reason = chunk["choices"][0]["finish_reason"]
                    if chunk.get("usage") is not None:
                        usage = chunk["usage"]
                    # Extract text delta from OpenAI chat chunk shape
                    try:
                        delta_text = chunk["choices"][0].get("delta", {}).get("content") or ""
                    except (KeyError, IndexError, AttributeError):
                        delta_text = ""
                    if delta_text:
                        delta_chunk = {
                            "id": cid,
                            "object": "text_completion",
                            "created": created,
                            "model": served_model,
                            "choices": [{
                                "text": delta_text,
                                "index": 0,
                                "finish_reason": None,
                            }],
                        }
                        yield f"data: {json.dumps(delta_chunk)}\n\n"
                finish_chunk = {
                    "id": cid, "object": "text_completion", "created": created,
                    "model": served_model,
                    "choices": [{"text": "", "index": 0, "finish_reason": finish_reason}],
                    **({"usage": usage} if usage is not None else {}),
                }
                yield f"data: {json.dumps(finish_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gateway_completions_stream_failed")
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error", "code": getattr(exc, "category", "inference_error")}})}\n\n'
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-Potluck-Served-By": served_by, **lock_headers(digest),
            },
        )

    # Non-streaming.
    try:
        if use_pool:
            result = await pool_routing.complete_pool_openai(
                messages, served_model, req.temperature, max_tokens,
                scope=coordinator_scope,
            )
        elif peer:
            result = await gateway_routing.complete_peer_openai(
                peer, messages, served_model, req.temperature, max_tokens, **peer_identity
            )
        else:
            result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens, **identity)
            if isinstance(result, dict):
                result.setdefault("model", local)
    except ModelIdentityError as exc:
        raise HTTPException(409, str(exc)) from exc
    except RemoteStreamError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.category, "message": str(exc)}) from exc
    except pool_routing.PoolDispatchError as exc:
        status = exc.status_code if exc.status_code in (404, 503) else 502
        raise HTTPException(status_code=status, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("gateway_completions_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")

    # Convert chat.completion → text_completion envelope.
    try:
        text = result["choices"][0].get("message", {}).get("content", "")
    except (KeyError, IndexError, AttributeError):
        text = ""
    try:
        usage = result.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens") or 0
        completion_tokens = usage.get("completion_tokens") or 0
    except Exception:
        prompt_tokens = 0
        completion_tokens = 0

    envelope = {
        "id": cid,
        "object": "text_completion",
        "created": created,
        "model": served_model,
        "choices": [{
            "text": text,
            "index": 0,
            "finish_reason": result["choices"][0].get("finish_reason", "stop"),
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return JSONResponse(envelope, headers={"X-Potluck-Served-By": served_by, **lock_headers(digest)})


# ---------------------------------------------------------------------------
# POST /v1/embeddings  — local embeddings (no peer/pool routing)
# ---------------------------------------------------------------------------

class EmbeddingRequest(BaseModel):
    model: str | None = None
    input: str | list[str] = ""
    # encoding_format is accepted but ignored (always float)
    encoding_format: str | None = None


@router.post("/embeddings")
async def embeddings_openai(req: EmbeddingRequest):
    """OpenAI /v1/embeddings. Embeds text locally using the memory system's
    embedding service. No peer/pool routing — embeddings are cheap and local.
    Returns 503 when the embedding model is unavailable."""
    # Lazy import — must NOT be at module level (create_app torch-free invariant).
    try:
        from potluck_local.services.embeddings import get_embedder
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"embedding service unavailable: {exc}")

    embedder = get_embedder()

    inputs: list[str] = [req.input] if isinstance(req.input, str) else list(req.input)
    if not inputs:
        raise HTTPException(status_code=400, detail="input must be a non-empty string or list")

    try:
        vecs = await embedder.embed_many(inputs)
    except Exception as exc:
        logger.exception("gateway_embeddings_failed")
        raise HTTPException(status_code=503, detail=f"embedding failed: {exc}")

    if vecs is None:
        raise HTTPException(
            status_code=503,
            detail="embedding model is not available (sentence-transformers not installed or failed to load)",
        )

    model_name = req.model or embedder.MODEL_NAME
    token_count = sum(len(t.split()) for t in inputs)  # rough token estimate

    data = [
        {"object": "embedding", "index": i, "embedding": vecs[i].tolist()}
        for i in range(len(inputs))
    ]
    return JSONResponse({
        "object": "list",
        "data": data,
        "model": model_name,
        "usage": {
            "prompt_tokens": token_count,
            "total_tokens": token_count,
        },
    })


# ---------------------------------------------------------------------------
# Usage dashboard recording (metadata only; never content)
# ---------------------------------------------------------------------------
#
# The gateway is the external-tool consumed path (coding agents hitting the
# OpenAI-compatible API). We record one usage row per completed request with
# token counts + latency + scope, never prompt/response text.


def _canonical_scope(served_by: str) -> str:
    """Map the gateway's served_by string to a canonical dashboard scope."""
    s = (served_by or "").lower()
    if s == "household" or s.startswith("peer:"):
        return "my_machines"
    if s in ("pool", "circle"):
        return s
    return "local"


async def _record_consumed(
    scope: str,
    model_slug: str,
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    ttft_ms: int | None = None,
    duration_ms: int | None = None,
    ok: bool = True,
) -> None:
    try:
        from potluck_local.services.usage_store import record_usage_event
        await record_usage_event(
            direction="consumed",
            scope=scope,
            model_slug=model_slug,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            ok=ok,
        )
    except Exception:
        logger.debug("usage_record_consumed_gateway_failed", exc_info=True)


async def _metered_consumed(gen, scope: str, model_slug: str):
    """Wrap the SSE generator to count output tokens + first-token latency +
    duration, then record one consumed usage row. Content is never inspected:
    a data chunk that is not the [DONE] sentinel and carries no error counts as
    one output token. Records in finally so a client disconnect still lands."""
    started = time.monotonic()
    first_ms: int | None = None
    count = 0
    ok = True
    try:
        async for chunk in gen:
            if isinstance(chunk, str) and chunk.startswith("data: ") and "[DONE]" not in chunk:
                if '"error"' in chunk:
                    ok = False
                else:
                    if first_ms is None:
                        first_ms = int((time.monotonic() - started) * 1000)
                    count += 1
            yield chunk
    finally:
        await _record_consumed(
            scope, model_slug,
            output_tokens=count,
            ttft_ms=first_ms,
            duration_ms=int((time.monotonic() - started) * 1000),
            ok=ok,
        )
