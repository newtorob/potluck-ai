"""Chat completions router.

POST /chat/completions — OpenAI-compatible streaming/non-streaming chat.

Memory + task integration (guide §9.7, §9.8):
- Retrieves relevant memories and active tasks; injects them into the system
  prompt before sending to the model.
- After the response, fires memory extraction as a fire-and-forget asyncio task
  so it never blocks the client.

Streaming extraction strategy:
  The upstream runtime.stream_chat() is an async generator.  We wrap it in
  _accumulating_stream(), which both yields each SSE chunk to the client AND
  concatenates the assistant content.  When the generator is exhausted (or
  cancelled), it schedules extraction via asyncio.create_task().  Using
  asyncio.create_task rather than FastAPI BackgroundTasks means the work
  survives SSE streams where BackgroundTasks would only fire after the client
  disconnects.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from typing import AsyncGenerator, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from potluck_local.services.context_manager import handle_context_overflow
from potluck_local.services.memory_extraction import get_memory_extractor
from potluck_local.services.memory_retrieval import get_memory_retriever
from potluck_local.services.runtime import get_runtime
from potluck_local.services.settings_manager import get_settings
from potluck_local.services.task_state import get_task_manager

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Synthetic session for potluck-chat internal calls
# ---------------------------------------------------------------------------

_chat_session_id: str | None = None
_chat_session_lock = asyncio.Lock()


async def _get_chat_session_id() -> str:
    """Lazily open a synthetic 'potluck-chat' session and return its ID.

    The session is created once per sidecar process and reused across all chat
    requests.  Uses a lock to prevent a race on first creation.
    """
    global _chat_session_id
    if _chat_session_id is not None:
        return _chat_session_id
    async with _chat_session_lock:
        if _chat_session_id is not None:
            return _chat_session_id
        from potluck_local.services.agent_observability import get_observer

        observer = get_observer()
        _chat_session_id = await observer.open_session(
            client_name="potluck-chat",
            client_version=None,
            transport="internal",
            pid=os.getpid(),
        )
        logger.info("chat_synthetic_session_opened id=%s", _chat_session_id)
    return _chat_session_id

_DEFAULT_SYSTEM = "You are Potluck AI, a helpful local assistant."


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


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
    conversation_id: str | None = None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _build_enhanced_messages(
    request: ChatRequest,
) -> tuple[list[dict], str]:
    """Build the enriched message list and return it alongside conversation_id.

    Steps:
    1. Separate any existing system message from the user/assistant turns.
    2. Retrieve relevant memories if the feature is active.
    3. Pull active tasks (top-3 by recency).
    4. Assemble enhanced system content: user-supplied text wins at the top.
    5. Return the final message list with system at position 0.
    """
    settings = get_settings()
    conversation_id = request.conversation_id or str(uuid.uuid4())
    active_project: str | None = settings.active_project or None

    raw_messages = [m.model_dump() for m in request.messages]

    # Split off any existing system message so we can prepend it.
    existing_system = ""
    non_system: list[dict] = []
    for msg in raw_messages:
        if msg["role"] == "system":
            # User-supplied system content — keep it at the top.
            existing_system = msg["content"]
        else:
            non_system.append(msg)

    base_system = existing_system if existing_system else _DEFAULT_SYSTEM

    # --- Memory retrieval ---
    memory_block = ""
    memory_enabled = settings.privacy.memory_enabled and settings.memory.enabled
    if memory_enabled and settings.memory.auto_retrieve:
        try:
            retriever = get_memory_retriever()
            memories = await retriever.get_relevant_memories(
                current_messages=non_system,
                max_memories=settings.memory.max_retrieval,
                min_confidence=settings.memory.min_confidence_for_retrieval,
                project=active_project,
            )
            if memories:
                memory_block = retriever.format_memories_for_prompt(memories)
                logger.info(
                    "chat_memory_retrieved conversation_id=%s project=%s count=%d",
                    conversation_id,
                    active_project,
                    len(memories),
                )
        except Exception as exc:
            logger.warning(
                "chat_memory_retrieve_failed conversation_id=%s error=%s",
                conversation_id,
                type(exc).__name__,
            )

    # --- Active task context ---
    task_block = ""
    try:
        active_tasks = await get_task_manager().list_tasks(
            status="active", project=active_project
        )
        # Sort by updated_at descending; take top 3.
        active_tasks_sorted = sorted(
            active_tasks,
            key=lambda t: t.updated_at,
            reverse=True,
        )[:3]
        if active_tasks_sorted:
            task_block = "\n\n".join(
                t.to_context_string() for t in active_tasks_sorted
            )
            logger.info(
                "chat_tasks_injected conversation_id=%s count=%d",
                conversation_id,
                len(active_tasks_sorted),
            )
    except Exception as exc:
        logger.warning(
            "chat_task_retrieve_failed conversation_id=%s error=%s",
            conversation_id,
            type(exc).__name__,
        )

    # --- Assemble system content (user text wins at top) ---
    parts = [base_system]
    if memory_block:
        parts.append(memory_block)
    if task_block:
        parts.append(task_block)
    enhanced_system = "\n\n".join(parts)

    enhanced_messages: list[dict] = [
        {"role": "system", "content": enhanced_system},
        *non_system,
    ]

    # --- Context overflow compression ---
    try:
        enhanced_messages = await handle_context_overflow(
            enhanced_messages,
            max_context_tokens=settings.runtime.context_size,
            active_project=active_project,
        )
    except Exception as exc:
        logger.warning(
            "chat_context_overflow_failed conversation_id=%s error=%s",
            conversation_id,
            type(exc).__name__,
        )

    return enhanced_messages, conversation_id


async def _fire_extraction(
    raw_messages: list[dict],
    assistant_content: str,
    conversation_id: str,
    active_project: str | None = None,
) -> None:
    """Background coroutine: extract memories from a completed exchange.

    active_project: tag extracted memories with this scope (from settings.active_project).
    Logs an 'extract' tool call against the synthetic potluck-chat session.
    """
    import time as _time

    start = _time.monotonic()
    error_str: str | None = None
    result_count: int | None = None
    try:
        full_exchange = raw_messages + [
            {"role": "assistant", "content": assistant_content}
        ]
        memories = await get_memory_extractor().extract_from_conversation(
            messages=full_exchange,
            conversation_id=conversation_id,
            project=active_project,
        )
        result_count = len(memories)
        logger.info(
            "chat_extraction_complete conversation_id=%s project=%s extracted=%d",
            conversation_id,
            active_project,
            result_count,
        )
    except Exception as exc:
        error_str = type(exc).__name__
        logger.warning(
            "chat_extraction_failed conversation_id=%s error=%s",
            conversation_id,
            error_str,
        )
    finally:
        duration_ms = int((_time.monotonic() - start) * 1000)
        try:
            from potluck_local.services.agent_observability import get_observer

            session_id = await _get_chat_session_id()
            observer = get_observer()
            await observer.log_tool_call(
                session_id=session_id,
                tool_name="chat_extract",
                args={"project": active_project} if active_project else {},
                result_count=result_count,
                duration_ms=duration_ms,
                error=error_str,
            )
            if active_project:
                await observer.update_session(session_id, project=active_project)
        except Exception:
            logger.debug("chat_observer_log_failed conversation_id=%s", conversation_id)


async def _accumulating_stream(
    upstream: AsyncGenerator[str, None],
    raw_messages: list[dict],
    conversation_id: str,
    should_extract: bool,
    active_project: str | None = None,
) -> AsyncGenerator[str, None]:
    """Wrap the upstream SSE generator, accumulate assistant text, then extract.

    Yields each chunk unchanged.  After the generator finishes (or is
    cancelled by a client disconnect), schedules _fire_extraction as a
    fire-and-forget asyncio task.
    """
    accumulated: list[str] = []
    try:
        async for chunk in upstream:
            # Parse delta content out of each SSE line for accumulation.
            # Format: "data: {...}\n\n" or "data: [DONE]\n\n"
            if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
                import json as _json

                try:
                    payload = _json.loads(chunk[6:])
                    delta_content = (
                        payload.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta_content:
                        accumulated.append(delta_content)
                except Exception:
                    pass  # non-JSON data lines — skip silently
            yield chunk
    except asyncio.CancelledError:
        # Client disconnected mid-stream — still attempt extraction with
        # whatever we accumulated.
        raise
    finally:
        if should_extract and accumulated:
            full_content = "".join(accumulated)
            asyncio.create_task(
                _fire_extraction(raw_messages, full_content, conversation_id, active_project)
            )


# ---------------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------------


@router.post("/completions")
async def chat_completions(request: ChatRequest) -> object:
    """OpenAI-compatible chat completions with memory + task injection."""
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

    enhanced_messages, conversation_id = await _build_enhanced_messages(request)
    raw_messages = [m.model_dump() for m in request.messages]

    settings = get_settings()
    active_project: str | None = settings.active_project or None
    memory_active = settings.privacy.memory_enabled and settings.memory.enabled
    should_extract = memory_active and settings.memory.auto_extract

    logger.info(
        "chat_request model_id=%s message_count=%d stream=%s conversation_id=%s",
        runtime.current_model_id(),
        len(raw_messages),
        request.stream,
        conversation_id,
    )

    if request.stream:
        upstream = runtime.stream_chat(
            messages=enhanced_messages,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            stop=request.stop,
        )
        return StreamingResponse(
            _accumulating_stream(
                upstream,
                raw_messages,
                conversation_id,
                should_extract,
                active_project,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
                "X-Conversation-Id": conversation_id,
            },
        )

    # Non-streaming path.
    result = await runtime.chat(
        messages=enhanced_messages,
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )

    # Schedule extraction after the response is assembled.
    if should_extract:
        try:
            assistant_content = (
                result.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
            )
            if assistant_content:
                asyncio.create_task(
                    _fire_extraction(raw_messages, assistant_content, conversation_id, active_project)
                )
        except Exception as exc:
            logger.warning(
                "chat_extraction_schedule_failed conversation_id=%s error=%s",
                conversation_id,
                type(exc).__name__,
            )

    # Echo conversation_id in non-streaming response.
    if isinstance(result, dict):
        result["conversation_id"] = conversation_id

    return result
