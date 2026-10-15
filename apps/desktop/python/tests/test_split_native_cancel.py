"""Request-scoped cancellation at the native adapter boundary; no real engine."""
from __future__ import annotations

import json
import threading

import httpx
import pytest

from potluck_local.services import split_native as sn


class Process:
    def __init__(self):
        self.process = self
        self.closes = 0
        self.closed = threading.Event()

    def poll(self):
        return 0 if self.closed.is_set() else None

    def close(self):
        self.closes += 1
        self.closed.set()


CONTENT = b'data: {"choices":[{"delta":{"content":"first"},"finish_reason":null}]}\n\n'
FINISH = b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


@pytest.fixture
def adapter(monkeypatch):
    requests = []
    real_client = httpx.Client

    def serve(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, content=CONTENT + FINISH)

    monkeypatch.setattr(sn.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(serve), **kwargs))
    process = Process()
    closed = []
    return sn.SplitModel(process, 12345, "fixture-key", lambda: closed.append(True)), process, requests, closed


@pytest.mark.parametrize("stream", [False, True])
def test_cancel_before_executor_start_sends_nothing_and_next_request_works(adapter, stream):
    model, process, requests, _ = adapter
    cancelled = threading.Event()
    cancelled.set()
    model.cancel(cancelled)
    with pytest.raises(sn.SplitError) as error:
        model.create_chat_completion(messages=[], stream=stream, cancel_event=cancelled)
    assert error.value.code == "split_cancelled"
    assert requests == [] and not model.closed and process.closes == 0
    result = list(model.create_chat_completion(messages=[], stream=True))
    assert result[-1]["choices"][0]["finish_reason"] == "stop"
    assert "cancel_event" not in requests[0] and requests[0]["cache_prompt"] is True


def test_cancel_unstarted_iterator_does_not_block_or_cancel_its_successor(adapter):
    model, process, requests, _ = adapter
    previous = threading.Event()
    stream = model.create_chat_completion(messages=[], stream=True, cancel_event=previous)
    model.cancel(previous)
    current = threading.Event()
    next_stream = model.create_chat_completion(messages=[], stream=True, cancel_event=current)
    with pytest.raises(sn.SplitError, match="cancelled"):
        list(stream)
    model.cancel(previous)
    assert not current.is_set()
    assert list(next_stream)[-1]["choices"][0]["finish_reason"] == "stop"
    assert len(requests) == 1 and not model.closed and process.closes == 0


def test_cancel_after_completed_request_cannot_target_next_request(adapter):
    model, process, _, _ = adapter
    previous, current = threading.Event(), threading.Event()
    list(model.create_chat_completion(messages=[], stream=True, cancel_event=previous))
    next_stream = model.create_chat_completion(messages=[], stream=True, cancel_event=current)
    model.cancel(previous)
    assert not current.is_set()
    assert list(next_stream)[-1]["choices"][0]["finish_reason"] == "stop"
    assert process.closes == 0


def test_explicit_close_remains_idempotent_across_threads(adapter):
    model, process, _, closed = adapter
    threads = [threading.Thread(target=model.close) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert model.closed and process.closes == 1 and closed == [True]


def test_missing_transport_trace_forces_shutdown_and_unblocks_worker(monkeypatch):
    process = Process()
    real_client = httpx.Client
    first = threading.Event()
    errors = []

    class Stream(httpx.SyncByteStream):
        def __iter__(self):
            yield CONTENT
            assert process.closed.wait(1), "forced shutdown must unblock a transport with no cancel trace"
            yield FINISH

    monkeypatch.setattr(sn.httpx, "Client", lambda **kwargs: real_client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stream())), **kwargs))
    monkeypatch.setattr(sn, "CANCEL_GRACE_SECONDS", 0.03)
    model = sn.SplitModel(process, 12345, "fixture-key", lambda: None)
    cancelled = threading.Event()

    def consume():
        try:
            for _ in model.create_chat_completion(messages=[], stream=True, cancel_event=cancelled):
                first.set()
        except sn.SplitError as error:
            errors.append(error)

    worker = threading.Thread(target=consume)
    worker.start()
    assert first.wait(1)
    model.cancel(cancelled)
    worker.join(1)
    assert not worker.is_alive()
    assert model.closed and process.closes == 1 and model.failure is None
    assert len(errors) == 1 and errors[0].code == "split_cancelled"
