"""Chat completions router.

POST /chat/completions — OpenAI-compatible streaming/non-streaming chat.
"""

from __future__ import annotations

import logging
from typing import Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services.runtime import get_runtime

logger = logging.getLogger(__name__)
router = APIRouter()


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    model: str | None = Field(default=None)
    stream: bool = Field(default=True)
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)
    stop: list[str] | None = None


@router.post("/completions")
async def chat_completions(request: ChatRequest) -> object:
    runtime = get_runtime()

    if not runtime.is_loaded():
        raise HTTPException(
            status_code=503,
            detail="No model loaded. POST /models/{slug}/load first.",
        )

    if request.model is not None and request.model != runtime.current_model_id():
        raise HTTPException(
            status_code=409,
            detail=f"Requested model '{request.model}' is not loaded. Load it first.",
        )

    messages = [m.model_dump() for m in request.messages]
    logger.info(
        "chat_request model_id=%s message_count=%d stream=%s",
        runtime.current_model_id(),
        len(messages),
        request.stream,
    )

    if request.stream:
        return StreamingResponse(
            runtime.stream_chat(
                messages=messages,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                stop=request.stop,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )

    return await runtime.chat(
        messages=messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
