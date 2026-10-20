"""Helpers shared by the Responses API tests: reading and checking server-sent events."""
from __future__ import annotations

import json


def sse_events(text):
    """The events of a response body, each as the string that was sent. Comment lines
    (keepalives) are not events and are left out."""
    return [block + "\n\n" for block in text.split("\n\n") if block and not block.startswith(":")]


def parse_sse(raw):
    """``[(event name, data)]`` from event strings, checking each is exactly
    ``event: x\\ndata: {...}\\n\\n`` and that sequence numbers count up from 0."""
    events = []
    for number, block in enumerate(raw):
        assert block.endswith("\n\n") and block.count("\n") == 3, repr(block)
        head, data = block[:-2].split("\n")
        assert head.startswith("event: ") and data.startswith("data: "), repr(block)
        body = json.loads(data[6:])
        assert body["type"] == head[7:]
        assert body["sequence_number"] == number
        events.append((head[7:], body))
    return events


def names(events):
    return [name for name, _ in events]


def items(events):
    """The finished output items, in order, from the output_item.done events."""
    return [body["item"] for name, body in events if name == "response.output_item.done"]


def assert_well_formed(events):
    """created and in_progress first, items numbered from 0 and never interleaved,
    one terminal response event last, whose output equals the finished items."""
    assert names(events)[:2] == ["response.created", "response.in_progress"]
    assert names(events)[-1] in ("response.completed", "response.incomplete", "response.failed")
    open_index, next_index = None, 0
    for name, body in events[2:-1]:
        if name == "response.output_item.added":
            assert open_index is None and body["output_index"] == next_index
            open_index, next_index = body["output_index"], next_index + 1
        elif name == "response.output_item.done":
            assert body["output_index"] == open_index
            open_index = None
        else:
            assert body["output_index"] == open_index, name
    assert open_index is None
    last = events[-1][1]["response"]
    if events[-1][0] != "response.failed":
        assert last["output"] == items(events)
