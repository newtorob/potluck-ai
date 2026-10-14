"""Token counts from text length, for the prompts and replies the engine did not count.

A reply from another machine carries no token counts, and a streamed reply from this
one carries them only sometimes. The gateways then estimate at about four characters
a token, rounded up, and say so in the X-Potluck-Usage header. Nothing here does I/O
or logs.
"""

from __future__ import annotations

import json

_CHARS_PER_TOKEN = 4


def tokens_for_chars(chars: int) -> int:
    """About four characters a token, rounded up."""
    return -(-chars // _CHARS_PER_TOKEN)


def estimate_tokens(text: str) -> int:
    return tokens_for_chars(len(text))


def engine_count(value: object) -> int | None:
    """An engine token count: a whole number, zero or more. Anything else is no count."""
    return value if type(value) is int and value >= 0 else None


def prompt_text(messages: list[dict], tools: list[dict] | None) -> str:
    """The text a prompt token count is taken over: the message text, the calls in the
    history, and the tools the model is shown, as JSON. The chat template's own markup
    is not in it."""
    parts = []
    for message in messages:
        parts.append(message.get("content") or "")
        for call in message.get("tool_calls") or ():
            parts.append(call["function"]["name"])
            parts.append(json.dumps(call["function"]["arguments"]))
    if tools:
        parts.append(json.dumps(tools))
    return "\n".join(parts)
