"""OpenAI-compatible gateway — let external tools use Potluck as a drop-in LLM.

Point any tool that speaks the OpenAI API (Cursor, Continue, aider, Open WebUI,
LiteLLM, the openai SDK) at  http://127.0.0.1:8321/v1  and it talks to this
machine's loaded model. This is the distribution on-ramp: fit existing tools
instead of requiring the native Potluck UI.

Slice 1 (this file): local only, serving the currently loaded model, reusing the
existing runtime. Endpoints:
  POST /v1/chat/completions  (streaming + non-streaming, native OpenAI shape)
  GET  /v1/models            (installed models)

Deliberately thin and clean: no memory injection or conversation persistence
(those are desktop-app features on /chat). A faithful OpenAI provider.

Auth: the app-level middleware gates /v1 with POTLUCK_GATEWAY_API_KEY (a separate
opt-in key for external tools), NOT the internal sidecar token. See app.py.

Slice 2 (follow-on): route through the coordinator to household/pool peers, and
add the Ollama-native API (/api/chat, /api/tags).
"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services import gateway_routing
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
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions_openai(req: ChatCompletionRequest):
    """OpenAI /v1/chat/completions. Serves the local model, or routes to a
    household peer (your own machine over the mesh) that has the requested model
    loaded. Routing is by model id: local first, then a reachable peer."""
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    # Resolve the route. A requested model that isn't local is looked up across
    # reachable household peers; if none has it, fail loudly (no silent swap).
    peer = None
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

    messages = [m.model_dump() for m in req.messages]
    max_tokens = req.max_tokens or _DEFAULT_MAX_TOKENS
    stop = _normalise_stop(req.stop)
    served_model = req.model if peer else local
    served_by = f"peer:{peer['node_name']}" if peer else "local"

    logger.info(
        "gateway_chat served_by=%s model=%s messages=%d stream=%s",
        served_by, served_model, len(messages), req.stream,
    )

    if req.stream:
        async def event_stream():
            try:
                if peer:
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
                            return
                        yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gateway_stream_failed")
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error"}})}\n\n'
            finally:
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
        if peer:
            result = await gateway_routing.complete_peer_openai(
                peer, messages, served_model, req.temperature, max_tokens
            )
        else:
            result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens)
            if isinstance(result, dict):
                result.setdefault("model", local)
    except Exception as exc:  # noqa: BLE001
        logger.exception("gateway_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    return JSONResponse(result, headers={"X-Potluck-Served-By": served_by})
