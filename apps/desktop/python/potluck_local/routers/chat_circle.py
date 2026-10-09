"""POST /chat/circle: the desktop app's circle chat, through the engine.

Today the app's renderer posts a circle chat straight to the coordinator's
/jobs/stream (apps/desktop/frontend/src/lib/localApi.ts streamNetworkJob), so
the engine never sees it and cannot try a direct link. This route takes the same
body the app sends today and streams back the same events, with the engine in
the middle (services/pool_routing.py -> services/circle_link.py: a direct link
first, the coordinator relay as the fallback). A later frontend change can point
streamNetworkJob here for circle chats; nothing in the app changes with this
route.

Body, as the app sends it now:
  {"prompt", "model_slug", "scope": "circle", "circle_id",
   "sampling": {"temperature", "max_tokens"}, "messages": [{role, content}]?}

Answer, the coordinator's own shapes:
  * Before the stream starts, a refusal is an HTTP error with {"detail": ...}
    and the coordinator's status (401 signed out, 404 no such circle, 503 no
    machine offering the model), which is what the app turns into its words.
  * Then server-sent events: {"choices": [{"delta": {"content": ...}}]} per
    token, {"meta": {...}} once when it worked, or {"error": {"category",
    "message"}} when it did not, and [DONE] last. The meta carries the receipt
    (path "direct" or "relay", transport) next to the fields it always had.

Only a circle request is accepted. A body for any other scope is refused (422):
this route never sends anything to the open pool.

Loopback only, behind the sidecar token like the rest of /chat (app.py's
peer_access_middleware). Nothing about the request is kept or logged beyond
counts: this engine's own user's prompt is on its way to other people's machines.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import aclosing
from typing import Any, AsyncGenerator, Literal

from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from potluck_local.services import pool_routing
from potluck_local.services.remote_stream import RemoteStreamError

logger = logging.getLogger(__name__)
router = APIRouter()


class CircleJobMessage(BaseModel):
    role: str
    content: str


class CircleJobSampling(BaseModel):
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)


class CircleJob(BaseModel):
    """What streamNetworkJob posts for a circle chat. Other keys are ignored, as
    the coordinator ignores them."""

    model_config = ConfigDict(extra="ignore")

    prompt: str
    model_slug: str = Field(min_length=1, max_length=128)
    scope: Literal["circle"]
    circle_id: uuid.UUID
    sampling: CircleJobSampling = Field(default_factory=CircleJobSampling)
    messages: list[CircleJobMessage] | None = None


_DONE = "data: [DONE]\n\n"
_END = object()


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _event(chunk: dict) -> str | None:
    """The coordinator's event for one engine chunk, or None for a chunk that
    carries nothing the app shows (a role-only opening)."""
    choice = chunk["choices"][0]
    if choice.get("finish_reason") is not None:
        meta = dict(chunk.get("potluck") or {})
        if not meta:
            return None
        meta.setdefault("served_by", "contributor")
        return _sse({"meta": meta})
    text = choice.get("delta", {}).get("content")
    if isinstance(text, str) and text:
        return _sse({"choices": [{"delta": {"content": text}}]})
    return None


def _error_event(exc: BaseException) -> str:
    category = getattr(exc, "category", None)
    message = str(exc) if isinstance(exc, RemoteStreamError) else "The request failed."
    return _sse({"error": {"category": category if isinstance(category, str) else "inference_error", "message": message}})


@router.post("/circle")
async def circle_chat(job: CircleJob) -> Response:
    """Stream a circle chat: a direct link to a circle member's machine when one
    can be made, the coordinator's relay otherwise."""
    messages = (
        [{"role": m.role, "content": m.content} for m in job.messages]
        if job.messages
        else [{"role": "user", "content": job.prompt}]
    )
    logger.info("circle_chat_started message_count=%d", len(messages))
    chunks = pool_routing.stream_pool_openai(
        messages,
        job.model_slug,
        job.sampling.temperature,
        job.sampling.max_tokens,
        scope=pool_routing._CIRCLE_SCOPE,
        circle_id=str(job.circle_id),
    )
    # Wait for the first event before answering, so that a refusal can still be an
    # HTTP status the app already knows how to word.
    try:
        first: Any = await anext(chunks)
    except StopAsyncIteration:
        first = _END
    except pool_routing.PoolDispatchError as exc:
        await chunks.aclose()
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:  # noqa: BLE001 - reported inside the stream, in the app's own shape
        await chunks.aclose()
        logger.info("circle_chat_failed error=%s", type(exc).__name__)
        return StreamingResponse(_single(_error_event(exc)), media_type="text/event-stream", headers=_HEADERS)
    return StreamingResponse(_stream(chunks, first), media_type="text/event-stream", headers=_HEADERS)


_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


async def _single(error: str) -> AsyncGenerator[str, None]:
    yield error
    yield _DONE


async def _stream(chunks: AsyncGenerator[dict, None], first: Any) -> AsyncGenerator[str, None]:
    try:
        async with aclosing(chunks):
            if first is not _END:
                event = _event(first)
                if event is not None:
                    yield event
            async for chunk in chunks:
                event = _event(chunk)
                if event is not None:
                    yield event
    except Exception as exc:  # noqa: BLE001
        logger.info("circle_chat_failed error=%s", type(exc).__name__)
        yield _error_event(exc)
    yield _DONE
