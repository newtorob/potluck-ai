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
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

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
        data.append({"id": cur, "object": "model", "created": created, "owned_by": "potluck"})
    return {"object": "list", "data": data}


@router.post("/chat/completions")
async def chat_completions_openai(req: ChatCompletionRequest):
    """OpenAI /v1/chat/completions against this machine's loaded model."""
    runtime = get_runtime()
    if not runtime.is_loaded():
        raise HTTPException(status_code=503, detail="No model loaded on this Potluck node.")

    # OpenAI clients pass a model id; we serve whatever is loaded. Reject a
    # mismatch loudly rather than silently serving a different model.
    if req.model and req.model != runtime.current_model_id():
        raise HTTPException(
            status_code=404,
            detail=f"model '{req.model}' is not loaded (loaded: '{runtime.current_model_id()}')",
        )

    messages = [m.model_dump() for m in req.messages]
    max_tokens = req.max_tokens or _DEFAULT_MAX_TOKENS
    stop = _normalise_stop(req.stop)

    logger.info(
        "gateway_chat model_id=%s messages=%d stream=%s",
        runtime.current_model_id(), len(messages), req.stream,
    )

    if req.stream:
        async def event_stream():
            try:
                async for chunk in runtime.stream_chat_raw(
                    messages, temperature=req.temperature, max_tokens=max_tokens, stop=stop
                ):
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield f'data: {json.dumps({"error": {"message": chunk["__error__"], "type": "inference_error"}})}\n\n'
                        break
                    yield f"data: {json.dumps(chunk)}\n\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("gateway_stream_failed")
                yield f'data: {json.dumps({"error": {"message": str(exc), "type": "inference_error"}})}\n\n'
            finally:
                yield "data: [DONE]\n\n"

        return StreamingResponse(
            event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
        )

    # Non-streaming: runtime.chat returns llama-cpp's native OpenAI-shaped dict.
    try:
        result = await runtime.chat(messages, temperature=req.temperature, max_tokens=max_tokens)
    except Exception as exc:  # noqa: BLE001
        logger.exception("gateway_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    if isinstance(result, dict):
        result.setdefault("model", runtime.current_model_id())
    return result
