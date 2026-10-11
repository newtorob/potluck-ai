"""Helpers shared by the Anthropic Messages tests: reading and checking server-sent events."""
from __future__ import annotations

import json


def sse_events(text):
    """The events of a response body, each as the string that was sent."""
    return [block + "\n\n" for block in text.split("\n\n") if block]


def parse_sse(raw):
    """``[(event name, data)]`` from event strings, checking each is exactly ``event: x\\ndata: {...}\\n\\n``."""
    events = []
    for block in raw:
        assert block.endswith("\n\n") and block.count("\n") == 3, repr(block)
        head, data = block[:-2].split("\n")
        assert head.startswith("event: ") and data.startswith("data: "), repr(block)
        body = json.loads(data[6:])
        assert body["type"] == head[7:]
        events.append((head[7:], body))
    return events


def names(events):
    return [name for name, _ in events]


def assert_well_formed(events):
    """message_start first, blocks numbered from 0 and never interleaved, then message_delta and message_stop."""
    assert events[0][0] == "message_start" and events[-2][0] == "message_delta" and events[-1][0] == "message_stop"
    open_index, next_index = None, 0
    for name, body in events[1:-2]:
        if name == "ping":
            continue
        if name == "content_block_start":
            assert open_index is None and body["index"] == next_index
            open_index, next_index = body["index"], next_index + 1
        elif name == "content_block_delta":
            assert body["index"] == open_index
        elif name == "content_block_stop":
            assert body["index"] == open_index
            open_index = None
        else:
            raise AssertionError(name)
    assert open_index is None


def blocks(events):
    """The content blocks put back together from the events: start block, text, tool input JSON, signature."""
    out = []
    for name, body in events:
        if name == "content_block_start":
            out.append({"block": body["content_block"], "text": "", "json": "", "signature": None})
        elif name == "content_block_delta":
            d = body["delta"]
            cur = out[-1]
            if d["type"] == "text_delta":
                cur["text"] += d["text"]
            elif d["type"] == "thinking_delta":
                cur["text"] += d["thinking"]
            elif d["type"] == "input_json_delta":
                cur["json"] += d["partial_json"]
            elif d["type"] == "signature_delta":
                cur["signature"] = d["signature"]
    return out
