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

from fastapi import APIRouter, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

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
    top_p: float = Field(default=1.0, ge=0.0, le=1.0)
    top_k: int = Field(default=0, ge=0)
    seed: int | None = None
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


def _estimate_tokens(text: str) -> int:
    """Rough token count from character length (~4 chars/token). A metadata-only
    proxy for the Usage dashboard when the runtime doesn't return a usage block
    (streaming). Never inspects meaning, only length."""
    return max(0, round(len(text) / 4))


async def _record_local_usage(
    model_slug: str | None,
    input_tokens: int,
    output_tokens: int,
    ttft_ms: int | None,
    duration_ms: int | None,
) -> None:
    """Record one local (scope='local') consumed usage row. Metadata only."""
    try:
        from potluck_local.services.usage_store import record_usage_event
        await record_usage_event(
            direction="consumed",
            scope="local",
            model_slug=model_slug,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            ttft_ms=ttft_ms,
            duration_ms=duration_ms,
            ok=True,
        )
    except Exception:
        logger.debug("usage_record_local_failed", exc_info=True)


async def _accumulating_stream(
    upstream: AsyncGenerator[str, None],
    raw_messages: list[dict],
    conversation_id: str,
    should_extract: bool,
    active_project: str | None = None,
    model_slug: str | None = None,
) -> AsyncGenerator[str, None]:
    """Wrap the upstream SSE generator, accumulate assistant text, then extract.

    Yields each chunk unchanged.  After the generator finishes (or is
    cancelled by a client disconnect), schedules _fire_extraction as a
    fire-and-forget asyncio task and records one metadata-only usage row.
    """
    import time as _time

    accumulated: list[str] = []
    started = _time.monotonic()
    first_ms: int | None = None
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
                        if first_ms is None:
                            first_ms = int((_time.monotonic() - started) * 1000)
                        accumulated.append(delta_content)
                except Exception:
                    pass  # non-JSON data lines — skip silently
            yield chunk
    except asyncio.CancelledError:
        # Client disconnected mid-stream — still attempt extraction with
        # whatever we accumulated.
        raise
    finally:
        full_content = "".join(accumulated)
        if should_extract and accumulated:
            asyncio.create_task(
                _fire_extraction(raw_messages, full_content, conversation_id, active_project)
            )
        # Usage dashboard: one metadata-only consumed row for this local turn.
        input_text = "".join(str(m.get("content", "")) for m in raw_messages)
        await _record_local_usage(
            model_slug,
            _estimate_tokens(input_text),
            _estimate_tokens(full_content),
            first_ms,
            int((_time.monotonic() - started) * 1000),
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
            top_p=request.top_p,
            top_k=request.top_k,
            seed=request.seed,
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
                runtime.current_model_id(),
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

    # Usage dashboard: one metadata-only consumed row for this local turn.
    _usage = result.get("usage", {}) if isinstance(result, dict) else {}
    await _record_local_usage(
        runtime.current_model_id(),
        int(_usage.get("prompt_tokens", 0) or 0),
        int(_usage.get("completion_tokens", 0) or 0),
        None,
        None,
    )

    return result


# =====================================================================
# Conversation history
# =====================================================================
#
# Local-first persistence. Every read/write goes against the same
# SQLite file as memories/tasks. No third party ever sees this data
# unless the user explicitly exports it.
#
# Sync across the user's own machines is v0.5+ (membership + cloud
# sync layer). Schema is designed so sync is purely additive —
# conversations + messages each carry their own UUID and updated_at,
# enough for last-writer-wins or CRDT later.
# =====================================================================

import uuid as _uuid
from potluck_local.services.memory_db import (
    ChatHistoryDisabledError,
    append_message,
    clear_conversation_messages,
    create_conversation as _create_conversation,
    delete_conversation as _delete_conversation,
    get_conversation as _get_conversation,
    list_conversations as _list_conversations,
    update_conversation as _update_conversation,
    update_message,
)

# Privacy gate for the chat-history writes below, mirroring the memory router.
# The four content-writing verbs (create/rename a conversation, append/patch a
# message) refuse with 403 when privacy.chat_history_enabled is off. Reads and
# deletes are never gated: the user must always be able to see and erase what
# is already stored, and turning the switch off deletes nothing.
#
# The detail is a plain string starting with the stable code
# "chat_history_disabled" so the frontend can branch on the code rather than
# on prose that may be reworded.
CHAT_HISTORY_DISABLED_DETAIL = (
    "chat_history_disabled: Save chat history is off in Privacy settings"
)


def _chat_history_enabled() -> bool:
    """Return True when the user has opted into storing chat history."""
    return get_settings().privacy.chat_history_enabled


class ConversationCreateRequest(BaseModel):
    """Caller may pass an id (UUID) so the frontend can use it
    optimistically before the round-trip; if absent, server generates."""

    id: str | None = None
    title: str = "New chat"
    project: str | None = None
    network_mode: bool = False
    network_model_slug: str | None = None

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class ConversationUpdateRequest(BaseModel):
    title: str | None = None
    network_mode: bool | None = None
    network_model_slug: str | None = None
    project: str | None = None
    # Sentinel to distinguish "unchanged" from "set to NULL" — pydantic
    # treats explicit None and absent identically by default.
    project_set: bool = False

    @field_validator("project", mode="before")
    @classmethod
    def _coerce_empty_project(cls, v: object) -> str | None:
        return None if v == "" else v  # type: ignore[return-value]


class MessageAppendRequest(BaseModel):
    """One persisted turn. Caller-supplied id (UUID) so the frontend
    can stream into a placeholder bubble before persisting."""

    id: str | None = None
    role: str  # "system" | "user" | "assistant"
    content: str
    status: str = "complete"
    served_by: str | None = None  # NULL | "contributor" | "local"
    output_tokens: int | None = None
    duration_ms: int | None = None
    finish_reason: str | None = None


class MessageUpdateRequest(BaseModel):
    """Patch an in-flight message — used to finalize an assistant message
    after streaming completes (status flips, meta lands)."""

    content: str | None = None
    status: str | None = None
    served_by: str | None = None
    output_tokens: int | None = None
    duration_ms: int | None = None
    finish_reason: str | None = None


@router.get("/conversations")
async def list_conversations_endpoint(
    project: str | None = None,
    include_all: bool = False,
    limit: int = 200,
) -> list[dict]:
    """List conversations with summary fields (no messages). Sorted by
    most recently updated."""
    return await _list_conversations(
        project=project, include_all=include_all, limit=limit
    )


@router.post("/conversations", status_code=201)
async def create_conversation_endpoint(req: ConversationCreateRequest) -> dict:
    if not _chat_history_enabled():
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    convo_id = req.id or str(_uuid.uuid4())
    try:
        return await _create_conversation(
            conversation_id=convo_id,
            title=req.title,
            project=req.project,
            network_mode=req.network_mode,
            network_model_slug=req.network_model_slug,
        )
    except ChatHistoryDisabledError:
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)


@router.get("/conversations/{conversation_id}")
async def get_conversation_endpoint(conversation_id: str) -> dict:
    convo = await _get_conversation(conversation_id)
    if convo is None:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id!r} not found")
    return convo


@router.patch("/conversations/{conversation_id}")
async def patch_conversation_endpoint(
    conversation_id: str,
    req: ConversationUpdateRequest,
) -> dict:
    if not _chat_history_enabled():
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    try:
        found = await _update_conversation(
            conversation_id=conversation_id,
            title=req.title,
            network_mode=req.network_mode,
            network_model_slug=req.network_model_slug,
            project=req.project,
            project_set=req.project_set,
        )
    except ChatHistoryDisabledError:
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    if not found:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id!r} not found")
    convo = await _get_conversation(conversation_id)
    return convo or {}


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation_endpoint(conversation_id: str) -> Response:
    """Delete a conversation + cascade its messages."""
    existed = await _delete_conversation(conversation_id)
    if not existed:
        raise HTTPException(status_code=404, detail=f"Conversation {conversation_id!r} not found")
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/messages", status_code=201)
async def append_message_endpoint(
    conversation_id: str,
    req: MessageAppendRequest,
) -> dict:
    if not _chat_history_enabled():
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    msg_id = req.id or str(_uuid.uuid4())
    try:
        return await append_message(
            message_id=msg_id,
            conversation_id=conversation_id,
            role=req.role,
            content=req.content,
            status=req.status,
            served_by=req.served_by,
            output_tokens=req.output_tokens,
            duration_ms=req.duration_ms,
            finish_reason=req.finish_reason,
        )
    except ChatHistoryDisabledError:
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)


@router.patch("/messages/{message_id}")
async def patch_message_endpoint(
    message_id: str,
    req: MessageUpdateRequest,
) -> dict:
    if not _chat_history_enabled():
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    try:
        found = await update_message(
            message_id=message_id,
            content=req.content,
            status=req.status,
            served_by=req.served_by,
            output_tokens=req.output_tokens,
            duration_ms=req.duration_ms,
            finish_reason=req.finish_reason,
        )
    except ChatHistoryDisabledError:
        raise HTTPException(status_code=403, detail=CHAT_HISTORY_DISABLED_DETAIL)
    if not found:
        raise HTTPException(status_code=404, detail=f"Message {message_id!r} not found")
    return {"id": message_id, "updated": True}


@router.delete("/conversations/{conversation_id}/messages")
async def clear_messages_endpoint(conversation_id: str) -> dict:
    """Clear all messages in a conversation but keep the conversation row."""
    removed = await clear_conversation_messages(conversation_id)
    return {"removed": removed}
