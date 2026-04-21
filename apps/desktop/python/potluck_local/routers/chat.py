"""Chat completions router.

POST /chat/completions — OpenAI-compatible streaming/non-streaming chat.

Implementation is stubbed until services/runtime.py is wired up (guide §4.2).
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

# TODO: wire up LLMRuntime once services/runtime.py is implemented (guide §4.2)
# from potluck_local.services.runtime import LLMRuntime

router = APIRouter()


class ChatMessage(BaseModel):
    """A single message in the conversation."""

    role: str = Field(..., description="One of: system, user, assistant")
    content: str = Field(..., description="Message text")


class ChatRequest(BaseModel):
    """Request body for POST /chat/completions."""

    messages: list[ChatMessage]
    model: str | None = Field(default=None, description="Model ID to use (optional override)")
    stream: bool = Field(default=True, description="Stream tokens via SSE")
    temperature: float = Field(default=0.7, ge=0.0, le=2.0)
    max_tokens: int = Field(default=2048, ge=1, le=131072)


@router.post("/completions")
async def chat_completions(request: ChatRequest) -> dict[str, object]:
    """Handle a chat completion request.

    Currently returns 503 until the LLMRuntime is implemented.

    TODO: Replace stub with real inference via services/runtime.py (guide §4.2):
        runtime = get_runtime()   # app-state singleton
        if not runtime.is_loaded():
            raise HTTPException(503, "No model loaded")
        if request.stream:
            return StreamingResponse(runtime.stream_chat(...), media_type="text/event-stream")
        return await runtime.chat(...)
    """
    raise HTTPException(status_code=503, detail="No model loaded")
