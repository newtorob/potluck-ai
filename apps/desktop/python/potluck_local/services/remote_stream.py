"""Validate remote SSE completion before reporting a successful gateway result."""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import AsyncIterator

import httpx


class RemoteStreamError(RuntimeError):
    def __init__(self, message: str, category: str = "stream_interrupted") -> None:
        self.category = category
        super().__init__(message)


async def _events(response: httpx.Response) -> AsyncIterator[str]:
    # httpx normalizes CRLF/CR. Parse complete SSE events, including multiline
    # data, instead of assuming each network line is a complete JSON object.
    data: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        if not line:
            if data:
                yield "\n".join(data)
            data, size = [], 0
        elif line.startswith("data:"):
            value = line[5:].removeprefix(" ")
            size += len(value)
            if size > 1_048_576:
                raise RemoteStreamError("Remote stream event is too large", "stream_protocol_error")
            data.append(value)
    # A producer may close immediately after its final data line.
    if data:
        yield "\n".join(data)


async def openai_chunks(
    response: httpx.Response, model: str, *, coordinator: bool = False,
) -> AsyncIterator[dict]:
    """Normalize direct-peer/coordinator streams, retaining their terminal state.

    Legacy household peers send content + DONE with no finish chunk. DONE is
    their explicit success signal, unless an error preceded it. Coordinator
    streams additionally require a valid success meta envelope before DONE.
    EOF alone is never success. Hold finish/usage until the terminator arrives.
    """
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    finish: str | None = None
    meta: dict | None = None
    usage = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}

    def chunk(delta: dict, reason: str | None = None) -> dict:
        return {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
        }

    async for data in _events(response):
        if data.strip() == "[DONE]":
            if coordinator and (meta is None or finish is None):
                raise RemoteStreamError("Remote job ended without a success summary")
            terminal = chunk({}, finish or "stop")
            terminal["usage"] = usage
            if meta is not None:
                # Whitelist only aggregate completion metadata, never identity.
                terminal["potluck"] = meta
            yield terminal
            return
        try:
            obj = json.loads(data)
        except ValueError as exc:
            raise RemoteStreamError("Malformed remote stream event", "stream_protocol_error") from exc
        if not isinstance(obj, dict):
            raise RemoteStreamError("Invalid remote stream event", "stream_protocol_error")
        if "error" in obj:
            error = obj["error"]
            category = error.get("category") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else error
            raise RemoteStreamError(
                message if isinstance(message, str) and message else "Remote inference failed",
                category if isinstance(category, str) and category else "inference_error",
            )
        if "meta" in obj:
            summary = obj["meta"]
            if not isinstance(summary, dict) or not isinstance(summary.get("finish_reason"), str):
                raise RemoteStreamError("Invalid remote success summary", "stream_protocol_error")
            finish = summary["finish_reason"]
            meta = {"finish_reason": finish}
            scope = summary.get("served_scope")
            if scope in ("mine", "circle", "pool"):
                meta["served_scope"] = scope
            for key in ("output_tokens", "duration_ms"):
                value = summary.get(key)
                if type(value) is int and value >= 0:
                    meta[key] = value
            usage["completion_tokens"] = meta.get("output_tokens")
        if isinstance(obj.get("usage"), dict):
            for key in usage:
                value = obj["usage"].get(key)
                if value is None or (type(value) is int and value >= 0):
                    usage[key] = value
        choices = obj.get("choices", [])
        if not isinstance(choices, list):
            raise RemoteStreamError("Invalid remote choices", "stream_protocol_error")
        if choices:
            first = choices[0]
            if not isinstance(first, dict) or not isinstance(first.get("delta", {}), dict):
                raise RemoteStreamError("Invalid remote delta", "stream_protocol_error")
            delta = first.get("delta", {})
            if delta:
                if finish is not None:
                    raise RemoteStreamError("Output after remote completion", "stream_protocol_error")
                yield chunk(delta)
            reason = first.get("finish_reason")
            if reason is not None:
                if not isinstance(reason, str) or not reason:
                    raise RemoteStreamError("Invalid remote finish reason", "stream_protocol_error")
                finish = reason
        if finish is not None and finish not in {"stop", "length", "tool_calls", "function_call", "content_filter"}:
            raise RemoteStreamError("Remote inference did not complete", finish)
    raise RemoteStreamError("Remote connection ended before the reply finished")


async def collect_completion(chunks: AsyncIterator[dict]) -> dict:
    """Collect the same validated stream used by streaming gateway callers."""
    parts: list[str] = []
    terminal: dict | None = None
    async for chunk in chunks:
        choice = chunk["choices"][0]
        content = choice.get("delta", {}).get("content")
        if content:
            parts.append(content)
        if choice.get("finish_reason") is not None:
            terminal = chunk
    if terminal is None:
        raise RemoteStreamError("Remote reply has no successful terminal event")
    result = {
        **{key: terminal[key] for key in ("id", "created", "model")},
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "".join(parts)},
                     "finish_reason": terminal["choices"][0]["finish_reason"]}],
        "usage": terminal["usage"],
    }
    if "potluck" in terminal:
        result["potluck"] = terminal["potluck"]
    return result
