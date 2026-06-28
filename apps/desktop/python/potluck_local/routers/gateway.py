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

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from potluck_local.services import gateway_routing, pool_routing
from potluck_local.services.model_registry import list_models
from potluck_local.services.runtime import get_runtime

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_MAX_TOKENS = 2048


class GatewayMessage(BaseModel):
    role: str
    content: str = ""


class ChatCompletionRequest(BaseModel):
    messages: list[GatewayMessage]
    model: str | None = None
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


@router.post("/chat/completions")
async def chat_completions_openai(req: ChatCompletionRequest):
    """OpenAI /v1/chat/completions. Serves the local model, or routes to a
    household peer (your own machine over the mesh) that has the requested model
    loaded. Routing is by model id: local first, then a reachable peer."""
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    # Resolve the route: local -> household peer -> open pool. A requested model
    # that isn't local is looked up across reachable household peers; if none has
    # it and open-pool routing is opted in, the coordinator finds a pool peer;
    # otherwise fail loudly (no silent swap).
    peer = None
    use_pool = False
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
    served_by = "pool" if use_pool else (f"peer:{peer['node_name']}" if peer else "local")

    logger.info(
        "gateway_chat served_by=%s model=%s messages=%d stream=%s",
        served_by, served_model, len(messages), req.stream,
    )

    if req.stream:
        async def event_stream():
            try:
                if use_pool:
                    # Single-turn to the pool: only the latest user message,
                    # never the full conversation (privacy invariant).
                    prompt = pool_routing.last_user_prompt(messages)
                    async for chunk in pool_routing.stream_pool_openai(
                        prompt, served_model, req.temperature, max_tokens
                    ):
                        yield f"data: {json.dumps(chunk)}\n\n"
                elif peer:
                    async for chunk in gateway_routing.stream_peer_openai(
                        peer, messages, served_model, req.temperature, max_tokens
                    ):
                        yield f"data: {json.dumps(chunk)}\n\n"
                else:
                    async for chunk in runtime.stream_chat_raw(
                        messages, temperature=req.temperature, max_tokens=max_tokens, stop=stop
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
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error"}})}\n\n'
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-Potluck-Served-By": served_by,
            },
        )

    # Non-streaming.
    try:
        if use_pool:
            prompt = pool_routing.last_user_prompt(messages)
            result = await pool_routing.complete_pool_openai(
                prompt, served_model, req.temperature, max_tokens
            )
        elif peer:
            result = await gateway_routing.complete_peer_openai(
                peer, messages, served_model, req.temperature, max_tokens
            )
        else:
            result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens)
            if isinstance(result, dict):
                result.setdefault("model", local)
    except pool_routing.PoolDispatchError as exc:
        # Surface the coordinator's real status when it's meaningful (e.g. 503
        # "no peer serves this model" — same condition as a local 404); any other
        # upstream failure is a bad-gateway (502).
        status = exc.status_code if exc.status_code in (404, 503) else 502
        raise HTTPException(status_code=status, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("gateway_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    return JSONResponse(result, headers={"X-Potluck-Served-By": served_by})


# ---------------------------------------------------------------------------
# POST /v1/completions  — OpenAI legacy text-completion
# ---------------------------------------------------------------------------

class CompletionRequest(BaseModel):
    model: str | None = None
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
async def text_completions(req: CompletionRequest):
    """OpenAI legacy /v1/completions. Converts prompt → user message and reuses
    the same local→household→pool routing path as /v1/chat/completions. Returns
    an OpenAI text_completion envelope (object "text_completion", choices[].text).
    """
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    peer = None
    use_pool = False
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
    served_by = "pool" if use_pool else (f"peer:{peer['node_name']}" if peer else "local")

    cid = "cmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    logger.info(
        "gateway_completions served_by=%s model=%s stream=%s",
        served_by, served_model, req.stream,
    )

    if req.stream:
        async def event_stream():
            try:
                if use_pool:
                    source = pool_routing.stream_pool_openai(
                        prompt_text, served_model, req.temperature, max_tokens
                    )
                elif peer:
                    source = gateway_routing.stream_peer_openai(
                        peer, messages, served_model, req.temperature, max_tokens
                    )
                else:
                    source = runtime.stream_chat_raw(
                        messages, temperature=req.temperature, max_tokens=max_tokens, stop=stop
                    )
                async for chunk in source:
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield f'data: {json.dumps({"error": {"message": chunk["__error__"], "type": "inference_error"}})}\n\n'
                        break
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
                    "choices": [{"text": "", "index": 0, "finish_reason": "stop"}],
                }
                yield f"data: {json.dumps(finish_chunk)}\n\n"
                yield "data: [DONE]\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gateway_completions_stream_failed")
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error"}})}\n\n'
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                "Connection": "keep-alive", "X-Potluck-Served-By": served_by,
            },
        )

    # Non-streaming.
    try:
        if use_pool:
            result = await pool_routing.complete_pool_openai(
                prompt_text, served_model, req.temperature, max_tokens
            )
        elif peer:
            result = await gateway_routing.complete_peer_openai(
                peer, messages, served_model, req.temperature, max_tokens
            )
        else:
            result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens)
            if isinstance(result, dict):
                result.setdefault("model", local)
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
            "finish_reason": "stop",
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }
    return JSONResponse(envelope, headers={"X-Potluck-Served-By": served_by})


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
