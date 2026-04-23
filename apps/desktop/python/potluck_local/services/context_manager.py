"""Context window overflow detection and conversation compression.

When a conversation approaches the token budget, this module compresses the
middle messages into a summary and persists active task context to SQLite.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

_OVERFLOW_THRESHOLD = 0.8
_PLACEHOLDER_SUMMARY = "[Earlier conversation omitted — context window limit reached]"


def estimate_tokens(messages: list[dict]) -> int:
    """Estimate token count using the ~4 chars/token heuristic."""
    return sum(len(m.get("content") or "") for m in messages) // 4


async def handle_context_overflow(
    messages: list[dict],
    *,
    max_context_tokens: int,
    active_project: str | None = None,
) -> list[dict]:
    """Compress the conversation if it exceeds 80 % of the token budget.

    Keeps system + first 2 non-system + last 6 non-system messages. The
    middle is summarised via the loaded model (or replaced with a placeholder
    if the runtime is not loaded).
    """
    if estimate_tokens(messages) < max_context_tokens * _OVERFLOW_THRESHOLD:
        return messages

    # Read active_project from settings if not explicitly provided
    if active_project is None:
        try:
            from potluck_local.services.settings_manager import get_settings
            active_project = get_settings().active_project
        except Exception:
            active_project = None

    system_msgs = [m for m in messages if m.get("role") == "system"]
    non_system = [m for m in messages if m.get("role") != "system"]

    if len(non_system) <= 8:
        return messages  # Not enough history to compress

    keep_start = non_system[:2]
    keep_end = non_system[-6:]
    middle = non_system[2:-6]

    summary_content = await _summarise_middle(middle)

    # Persist active task context summary while we have the model available
    await _persist_task_summary(active_project=active_project)

    summary_msg = {
        "role": "system",
        "content": f"[Earlier conversation summary: {summary_content}]",
    }

    compressed = system_msgs + keep_start + [summary_msg] + keep_end
    logger.info(
        "context_compressed original_msgs=%d compressed_msgs=%d",
        len(messages),
        len(compressed),
    )
    return compressed


async def _summarise_middle(middle: list[dict]) -> str:
    """Ask the loaded model to summarise the middle conversation segment."""
    from potluck_local.services.runtime import get_runtime
    runtime = get_runtime()

    if not runtime.is_loaded():
        logger.warning("context_overflow_no_model using_placeholder")
        return _PLACEHOLDER_SUMMARY

    middle_text = "\n".join(
        f"{m['role'].upper()}: {(m.get('content') or '')[:300]}"
        for m in middle
    )
    prompt = (
        "Summarize this conversation section in 2-3 sentences, preserving: "
        "the user's original request, any decisions made, current progress, "
        "and any stated constraints or preferences. Be specific and factual.\n\n"
        f"Conversation:\n{middle_text}\n\nSummary:"
    )

    try:
        result = await runtime.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=300,
        )
        return result["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        logger.warning("context_overflow_summary_error type=%s", type(exc).__name__)
        return _PLACEHOLDER_SUMMARY


async def _persist_task_summary(active_project: str | None = None) -> None:
    """Update context_summary on the first active task using the loaded model."""
    from potluck_local.services.runtime import get_runtime
    from potluck_local.services.task_state import get_task_manager

    runtime = get_runtime()
    if not runtime.is_loaded():
        return

    manager = get_task_manager()
    try:
        active = await manager.list_tasks(status="active", project=active_project)
    except Exception:
        return

    if not active:
        return

    task = active[0]
    prompt = (
        f"Summarize the current state of this task in 2-3 sentences. "
        f"What has been accomplished? What remains?\n"
        f"Task: {task.task_name}\n"
        f"Objective: {task.objective}"
    )

    try:
        result = await runtime.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200,
        )
        summary = result["choices"][0]["message"]["content"].strip()
        await manager.update_task(task.id, {"context_summary": summary})
        logger.info("task_context_summary_updated id=%s", task.id)
    except Exception as exc:
        logger.warning("task_summary_error type=%s", type(exc).__name__)
