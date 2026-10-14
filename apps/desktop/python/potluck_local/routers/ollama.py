"""Ollama-native API surface — for tools hardcoded to Ollama's endpoints.

Most modern tools accept an OpenAI-compatible base URL (use the /v1 gateway for
those). This module covers the long tail of tools that speak only the Ollama API
(/api/tags, /api/chat, /api/show, /api/version, /api/generate, /api/embeddings,
/api/embed). It maps onto the same runtime as the /v1 gateway.

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
from potluck_local.routers.gateway import model_not_found
from potluck_local.services import gateway_routing, pool_routing
from potluck_local.services.remote_stream import RemoteStreamError
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
    # Open-pool models (other members' machines), only when opted in.
    if pool_routing.pool_enabled():
        try:
            for slug in await pool_routing.pool_model_slugs():
                if slug not in seen:
                    seen.add(slug)
                    models.append({
                        "name": slug, "model": slug, "modified_at": created,
                        "size": 0, "digest": "", "details": {},
                    })
        except Exception:
            logger.exception("ollama_pool_tags_failed")
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
    use_pool = False
    if req.model and req.model != local:
        peer, silent = await gateway_routing.find_peer(req.model)
        if peer is None:
            if pool_routing.pool_enabled():
                use_pool = True
            else:
                raise model_not_found(req.model, silent)
    if not use_pool and peer is None and local is None:
        raise HTTPException(status_code=503, detail="No model loaded on this node, and no household peer matched.")

    messages = [m.model_dump() for m in req.messages]
    temperature, max_tokens = _opts(req)
    model_name = req.model if (peer or use_pool) else local
    served_by = "pool" if use_pool else ("household" if peer else "local")

    if req.stream:
        async def ndjson_stream():
            finish_reason = "stop"
            try:
                if use_pool:
                    # pool_routing applies the multi-turn privacy policy.
                    source = pool_routing.stream_pool_openai(
                        messages, model_name, temperature, max_tokens
                    )
                elif peer:
                    source = gateway_routing.stream_peer_openai(peer, messages, model_name, temperature, max_tokens)
                else:
                    source = runtime.stream_chat_raw(messages, temperature=temperature, max_tokens=max_tokens)
                async for chunk in source:
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield json.dumps({"model": model_name, "created_at": _now_iso(), "error": chunk["__error__"]}) + "\n"
                        return
                    if chunk.get("choices") and chunk["choices"][0].get("finish_reason"):
                        finish_reason = chunk["choices"][0]["finish_reason"]
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
                "done": True, "done_reason": finish_reason,
            }) + "\n"

        return StreamingResponse(
            ndjson_stream(), media_type="application/x-ndjson",
            headers={"X-Potluck-Served-By": served_by},
        )

    # Non-streaming: single JSON object.
    try:
        if use_pool:
            result = await pool_routing.complete_pool_openai(messages, model_name, temperature, max_tokens)
        elif peer:
            result = await gateway_routing.complete_peer_openai(peer, messages, model_name, temperature, max_tokens)
        else:
            result = await runtime.chat(messages, temperature=temperature, max_tokens=max_tokens)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except RemoteStreamError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.category, "message": str(exc)}) from exc
    except pool_routing.PoolDispatchError as exc:
        status = exc.status_code if exc.status_code in (404, 503) else 502
        raise HTTPException(status_code=status, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ollama_chat_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    return JSONResponse({
        "model": model_name,
        "created_at": _now_iso(),
        "message": {"role": "assistant", "content": content},
        "done": True,
        "done_reason": result["choices"][0].get("finish_reason", "stop"),
    }, headers={"X-Potluck-Served-By": served_by})


# ---------------------------------------------------------------------------
# POST /api/generate  — Ollama legacy single-prompt generate
# ---------------------------------------------------------------------------

class OllamaGenerateRequest(BaseModel):
    model: str | None = None
    prompt: str = ""
    stream: bool = True  # Ollama defaults to streaming
    options: dict | None = None


@router.post("/generate")
async def generate(req: OllamaGenerateRequest):
    """Ollama /api/generate. Accepts {model, prompt, stream, options}. Converts
    the prompt to a user message and reuses the same local→household→pool routing
    path as /api/chat. Returns Ollama's generate NDJSON shape."""
    runtime = get_runtime()
    local = runtime.current_model_id() if runtime.is_loaded() else None

    peer = None
    use_pool = False
    if req.model and req.model != local:
        peer, silent = await gateway_routing.find_peer(req.model)
        if peer is None:
            if pool_routing.pool_enabled():
                use_pool = True
            else:
                raise model_not_found(req.model, silent)
    if not use_pool and peer is None and local is None:
        raise HTTPException(status_code=503, detail="No model loaded on this node, and no household peer matched.")

    opts = req.options or {}
    temperature = float(opts.get("temperature", 0.7))
    max_tokens = int(opts.get("num_predict", _DEFAULT_MAX_TOKENS))
    if max_tokens <= 0:
        max_tokens = _DEFAULT_MAX_TOKENS

    messages = [{"role": "user", "content": req.prompt}]
    model_name = req.model if (peer or use_pool) else local
    served_by = "pool" if use_pool else ("household" if peer else "local")

    if req.stream:
        async def ndjson_stream():
            finish_reason = "stop"
            try:
                if use_pool:
                    source = pool_routing.stream_pool_openai(
                        messages, model_name, temperature, max_tokens
                    )
                elif peer:
                    source = gateway_routing.stream_peer_openai(peer, messages, model_name, temperature, max_tokens)
                else:
                    source = runtime.stream_chat_raw(messages, temperature=temperature, max_tokens=max_tokens)
                async for chunk in source:
                    if isinstance(chunk, dict) and "__error__" in chunk:
                        yield json.dumps({"model": model_name, "created_at": _now_iso(), "error": chunk["__error__"]}) + "\n"
                        return
                    if chunk.get("choices") and chunk["choices"][0].get("finish_reason"):
                        finish_reason = chunk["choices"][0]["finish_reason"]
                    try:
                        token = chunk["choices"][0].get("delta", {}).get("content") or ""
                    except (KeyError, IndexError, AttributeError):
                        token = ""
                    if token:
                        yield json.dumps({
                            "model": model_name,
                            "created_at": _now_iso(),
                            "response": token,
                            "done": False,
                        }) + "\n"
            except Exception as exc:  # noqa: BLE001
                logger.exception("ollama_generate_stream_failed")
                yield json.dumps({"model": model_name, "created_at": _now_iso(), "error": str(exc)}) + "\n"
                return
            yield json.dumps({
                "model": model_name,
                "created_at": _now_iso(),
                "response": "",
                "done": True,
                "done_reason": finish_reason,
            }) + "\n"

        return StreamingResponse(
            ndjson_stream(), media_type="application/x-ndjson",
            headers={"X-Potluck-Served-By": served_by},
        )

    # Non-streaming: single aggregated object.
    try:
        if use_pool:
            result = await pool_routing.complete_pool_openai(messages, model_name, temperature, max_tokens)
        elif peer:
            result = await gateway_routing.complete_peer_openai(peer, messages, model_name, temperature, max_tokens)
        else:
            result = await runtime.chat(messages, temperature=temperature, max_tokens=max_tokens)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    except RemoteStreamError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.category, "message": str(exc)}) from exc
    except pool_routing.PoolDispatchError as exc:
        status = exc.status_code if exc.status_code in (404, 503) else 502
        raise HTTPException(status_code=status, detail=exc.detail)
    except Exception as exc:  # noqa: BLE001
        logger.exception("ollama_generate_failed")
        raise HTTPException(status_code=500, detail=f"inference failed: {type(exc).__name__}")
    return JSONResponse({
        "model": model_name,
        "created_at": _now_iso(),
        "response": content,
        "done": True,
        "done_reason": result["choices"][0].get("finish_reason", "stop"),
    }, headers={"X-Potluck-Served-By": served_by})


# ---------------------------------------------------------------------------
# POST /api/embeddings  — Ollama legacy single-input embedding
# POST /api/embed       — Ollama newer batch embedding
# ---------------------------------------------------------------------------

async def _embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed texts using the local embedding service. Raises HTTPException on failure."""
    try:
        from potluck_local.services.embeddings import get_embedder
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"embedding service unavailable: {exc}")

    embedder = get_embedder()
    try:
        vecs = await embedder.embed_many(texts)
    except Exception as exc:
        logger.exception("ollama_embeddings_failed")
        raise HTTPException(status_code=503, detail=f"embedding failed: {exc}")

    if vecs is None:
        raise HTTPException(
            status_code=503,
            detail="embedding model is not available (sentence-transformers not installed or failed to load)",
        )
    return [vecs[i].tolist() for i in range(len(texts))]


@router.post("/embeddings")
async def embeddings_ollama(body: dict) -> dict:
    """Ollama /api/embeddings. Accepts {model, prompt} (legacy single-input shape).
    Returns {embedding: [...]}. Embeddings are local-only; no peer/pool routing."""
    prompt = body.get("prompt", "")
    if not isinstance(prompt, str) or not prompt:
        raise HTTPException(status_code=400, detail="prompt must be a non-empty string")
    vecs = await _embed_texts([prompt])
    return {"embedding": vecs[0]}


@router.post("/embed")
async def embed_ollama(body: dict) -> dict:
    """Ollama /api/embed. Accepts {model, input} where input is a string or list of
    strings (newer batch shape). Returns {embeddings: [[...]]}."""
    raw = body.get("input", "")
    if isinstance(raw, str):
        texts = [raw] if raw else []
    elif isinstance(raw, list):
        texts = [t for t in raw if isinstance(t, str) and t]
    else:
        texts = []
    if not texts:
        raise HTTPException(status_code=400, detail="input must be a non-empty string or list of strings")
    vecs = await _embed_texts(texts)
    return {"embeddings": vecs}
