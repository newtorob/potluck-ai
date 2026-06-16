"""Ollama-native API surface — for tools hardcoded to Ollama's endpoints.

Most modern tools accept an OpenAI-compatible base URL (use the /v1 gateway for
those). This module covers the long tail of tools that speak only the Ollama API
(/api/tags, /api/chat, /api/show, /api/version). It maps onto the same runtime
as the /v1 gateway.

Auth: gated identically to /v1 by the gateway key (see app.py middleware), so the
tool must be pointed at this base URL and given the key. We are not on Ollama's
default port, so auto-detection is not a goal; explicit configuration is.

Streaming uses NDJSON (one JSON object per line), which is Ollama's wire format
(distinct from the OpenAI gateway's SSE).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from potluck_local import __version__
from potluck_local.services import gateway_routing
from potluck_local.services.model_registry import list_models
from potluck_local.services.runtime import get_runtime

logger = logging.getLogger(__name__)

router = APIRouter()

_DEFAULT_MAX_TOKENS = 2048


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class OllamaMessage(BaseModel):
    role: str
    content: str = ""


class OllamaChatRequest(BaseModel):
    model: str | None = None
    messages: list[OllamaMessage]
    stream: bool = True  # Ollama defaults to streaming
    options: dict | None = None


def _opts(req: OllamaChatRequest) -> tuple[float, int]:
    """Extract (temperature, max_tokens) from Ollama-style options."""
    opts = req.options or {}
    temperature = float(opts.get("temperature", 0.7))
    max_tokens = int(opts.get("num_predict", _DEFAULT_MAX_TOKENS))
    if max_tokens <= 0:
        max_tokens = _DEFAULT_MAX_TOKENS
    return temperature, max_tokens


@router.get("/version")
async def version() -> dict:
    return {"version": __version__}


@router.get("/tags")
async def tags() -> dict:
    """Ollama /api/tags: list installed models."""
    runtime = get_runtime()
    created = _now_iso()
    models: list[dict] = []
    seen: set[str] = set()
    try:
        for m in list_models():
            if not m.get("installed"):
                continue
            slug = m.get("slug")
            if not slug or slug in seen:
                continue
            seen.add(slug)
            models.append({
                "name": slug,
                "model": slug,
                "modified_at": created,
                "size": int(m.get("size_on_disk") or 0),
                "digest": "",
                "details": {
                    "family": "",
                    "parameter_size": "",
                    "quantization_level": "",
                },
            })
    except Exception:
        logger.exception("ollama_tags_failed")
    cur = runtime.current_model_id()
    if cur and cur not in seen:
        seen.add(cur)
        models.append({
            "name": cur, "model": cur, "modified_at": created,
            "size": 0, "digest": "", "details": {},
        })
    # Household peers' loaded models, so an Ollama tool sees the whole mesh.
    try:
        for slug in (await gateway_routing.household_model_map()).keys():
            if slug not in seen:
                seen.add(slug)
                models.append({
                    "name": slug, "model": slug, "modified_at": created,
                    "size": 0, "digest": "", "details": {},
                })
    except Exception:
        logger.exception("ollama_household_tags_failed")
    return {"models": models}


@router.post("/show")
async def show(body: dict) -> dict:
    """Ollama /api/show: minimal model metadata (enough for tools that probe it)."""
    name = body.get("model") or body.get("name") or get_runtime().current_model_id() or ""
    return {
        "model": name,
        "details": {"family": "", "parameter_size": "", "quantization_level": ""},
        "model_info": {},
        "capabilities": ["completion"],
    }


@router.post("/chat")
async def chat(req: OllamaChatRequest):
    """Ollama /api/chat. Serves the local model, or routes to a household peer
    that has the requested model loaded."""
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    peer = None
    if req.model and req.model != local:
        peer = await gateway_routing.peer_for_model(req.model)
        if peer is None:
            raise HTTPException(
                status_code=404,
                detail=f"model '{req.model}' is not loaded here or on a reachable household peer",
            )
    if peer is None and local is None:
        raise HTTPException(status_code=503, detail="No model loaded on this node, and no household peer matched.")

    messages = [m.model_dump() for m in req.messages]
    temperature, max_tokens = _opts(req)
    model_name = req.model if peer else local
    served_by = f"peer:{peer['node_name']}" if peer else "local"

    if req.stream:
        async def ndjson_stream():
            try:
                source = (
                    gateway_routing.stream_peer_openai(peer, messages, model_name, temperature, max_tokens)
                    if peer
                    else runtime.stream_chat_raw(messages, temperature=temperature, max_tokens=max_tokens)
                )
                async for chunk in source:
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield json.dumps({"model": model_name, "created_at": _now_iso(), "error": chunk["__error__"]}) + "\n"
                        return
                    delta = ""
                    try:
                        delta = chunk["choices"][0].get("delta", {}).get("content") or ""
                    except (KeyError, IndexError, AttributeError):
                        delta = ""
                    if delta:
                        yield json.dumps({
                            "model": model_name, "created_at": _now_iso(),
                            "message": {"role": "assistant", "content": delta},
                            "done": False,
                        }) + "\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("ollama_stream_failed")
                yield json.dumps({"model": model_name, "created_at": _now_iso(), "error": str(exc)}) + "\n"
                return
            yield json.dumps({
                "model": model_name, "created_at": _now_iso(),
                "message": {"role": "assistant", "content": ""},
                "done": True, "done_reason": "stop",
            }) + "\n"

        return StreamingResponse(
            ndjson_stream(), media_type="application/x-ndjson",
            headers={"X-Potluck-Served-By": served_by},
        )

    # Non-streaming: single JSON object.
    try:
        if peer:
            result = await gateway_routing.complete_peer_openai(peer, messages, model_name, temperature, max_tokens)
        else:
            result = await runtime.chat(messages, temperature=temperature, max_tokens=max_tokens)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except Exception as exc:  # noqa: BLE001
        logger.exception("ollama_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    return JSONResponse({
        "model": model_name,
        "created_at": _now_iso(),
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": "stop",
    }, headers={"X-Potluck-Served-By": served_by})
