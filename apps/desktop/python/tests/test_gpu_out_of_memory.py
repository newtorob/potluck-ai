"""A GPU that runs out of memory during inference, and who is told what.

On the Windows machine with an RX 6600 XT the Vulkan backend threw
vk::OutOfDeviceMemoryError from inside llama_decode. Nothing in llama.cpp caught
it, ctypes turned the structured exception into
OSError(winerror=-529697949), and the user and a peer both received the
exception text "[WinError -529697949] Windows Error 0xe06d7363". The user gets a
sentence they can act on now. Another machine gets only fixed text chosen by
category, never an exception string.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from potluck_local.routers import gateway, models, peer
from potluck_local.services import inference_errors as ie
from potluck_local.services.runtime import LLMRuntime

OWN_TEXT = "The GPU stopped this reply, most likely because it ran out of memory. Close other programs that use the GPU, or load a smaller model."
RAW = "[WinError -529697949] Windows Error 0xe06d7363"
LEAKS = ("WinError", "0xe06d7363", "-529697949", "secret-path", "OSError")


class CppException(OSError):
    """What ctypes raises for an uncaught C++ exception on Windows."""

    winerror = -529697949

    def __str__(self) -> str:
        return RAW


class OutOfMemoryError(RuntimeError):
    """Stands in for torch.OutOfMemoryError, which is matched by name."""


# ---------------------------------------------------------------------------
# Recognition
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc,gpu,expected", [
    # the failure on the machine
    (CppException(), True, ie.GPU_OUT_OF_MEMORY),
    # a CPU-only load cannot throw a device allocation error
    (CppException(), False, ie.INFERENCE_ERROR),
    # the same code with the number re-wrapped into text
    (RuntimeError("OSError: [WinError -529697949] Windows Error 0xe06d7363"), True, ie.INFERENCE_ERROR),
    (OSError("[WinError -529697949] Windows Error 0xE06D7363"), True, ie.GPU_OUT_OF_MEMORY),
    # signals the GPU runtimes emit themselves
    (RuntimeError("vk::Device::allocateMemory: ErrorOutOfDeviceMemory"), False, ie.GPU_OUT_OF_MEMORY),
    (RuntimeError("CUDA out of memory. Tried to allocate 20.00 MiB"), False, ie.GPU_OUT_OF_MEMORY),
    (RuntimeError("CUDA error: out of memory"), False, ie.GPU_OUT_OF_MEMORY),
    (OutOfMemoryError("Tried to allocate 2 GiB"), False, ie.GPU_OUT_OF_MEMORY),
    # not out-of-memory
    (OSError("disk full"), True, ie.INFERENCE_ERROR),
    (FileNotFoundError(2, "No such file", "model.gguf"), True, ie.INFERENCE_ERROR),
    (ConnectionResetError("peer closed"), True, ie.INFERENCE_ERROR),
    (MemoryError(), True, ie.INFERENCE_ERROR),  # host RAM
    (RuntimeError("llama_decode returned -3"), True, ie.INFERENCE_ERROR),
    (ValueError("the cache ran out of memory slots"), True, ie.INFERENCE_ERROR),
    (RuntimeError("Failed to create llama_context"), True, ie.INFERENCE_ERROR),
])
def test_only_gpu_out_of_memory_signals_are_labelled_out_of_memory(exc, gpu, expected):
    assert ie.classify_inference_error(exc, gpu_offload=gpu) == expected


def test_an_os_error_with_another_windows_code_is_not_out_of_memory():
    class Other(OSError):
        winerror = 5  # access denied

    assert ie.classify_inference_error(Other(), gpu_offload=True) == ie.INFERENCE_ERROR


def test_the_machines_own_user_gets_the_sentence_and_other_machines_get_fixed_text():
    own = ie.describe_failure(CppException(), gpu_offload=True)
    assert (own.category, own.message) == ("gpu_out_of_memory", OWN_TEXT)
    other = ie.describe_failure(RuntimeError("an ordinary failure"), gpu_offload=True)
    assert (other.category, other.message) == ("inference_error", "an ordinary failure")  # unchanged for the owner
    assert ie.other_machine_error("gpu_out_of_memory") == {
        "category": "gpu_out_of_memory",
        "message": "The serving machine's GPU most likely ran out of memory.",
    }
    assert ie.other_machine_text("inference_error") == "Inference failed"


@pytest.mark.parametrize("hostile", [None, 7, ["gpu_out_of_memory"], "x" * 5000, "GPU_OUT_OF_MEMORY", "../../etc/passwd", ""])
def test_other_machines_never_get_text_from_an_unknown_category(hostile):
    assert ie.other_machine_error(hostile) == {"category": "inference_error", "message": "Inference failed"}


# ---------------------------------------------------------------------------
# The runtime
# ---------------------------------------------------------------------------


class Model:
    def __init__(self, exc: BaseException | None = None):
        self.exc = exc

    def create_chat_completion(self, **kwargs):
        if self.exc is not None:
            raise self.exc
        if kwargs.get("stream"):
            return iter([{"choices": [{"delta": {"content": "hello"}, "finish_reason": None}]}])
        return {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}


def make_runtime(exc: BaseException | None, *, on_gpu: bool = True) -> LLMRuntime:
    rt = LLMRuntime()
    rt._model, rt._model_id = Model(exc), "test"
    rt._load_report = {
        "offloaded_layers": 37 if on_gpu else 0,
        "used_n_gpu_layers": -1 if on_gpu else 0,
    }
    return rt


def collect(rt: LLMRuntime, **kwargs) -> list[str]:
    async def go():
        return [chunk async for chunk in rt.stream_chat([{"role": "user", "content": "hi"}], **kwargs)]

    return asyncio.run(go())


def test_the_streamed_error_for_the_own_user_is_the_sentence_with_its_category():
    events = collect(make_runtime(CppException()))
    assert events[0] == f'data: {json.dumps({"error": OWN_TEXT, "category": "gpu_out_of_memory"})}\n\n'
    assert events[-1] == "data: [DONE]\n\n"
    assert not any(leak in "".join(events) for leak in ("WinError", "0xe06d7363"))


def test_the_same_exception_on_a_cpu_load_keeps_the_engines_own_message():
    events = collect(make_runtime(CppException(), on_gpu=False))
    payload = json.loads(events[0][6:])
    assert payload == {"error": RAW, "category": "inference_error"}


def test_the_log_names_the_category_and_not_the_message(caplog):
    caplog.set_level("ERROR")
    collect(make_runtime(CppException()))
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("error=CppException category=gpu_out_of_memory" in m for m in errors)
    assert not any("0xe06d7363" in m for m in errors)


def test_the_raw_stream_carries_the_category_beside_the_message():
    rt = make_runtime(CppException())

    async def go():
        return [c async for c in rt.stream_chat_raw([{"role": "user", "content": "hi"}])]

    assert asyncio.run(go()) == [{"__error__": OWN_TEXT, "__category__": "gpu_out_of_memory"}]


def test_a_non_streaming_call_raises_the_classified_failure():
    rt = make_runtime(CppException())

    async def go():
        return await rt.chat([{"role": "user", "content": "hi"}])

    with pytest.raises(ie.InferenceFailure) as raised:
        asyncio.run(go())
    assert raised.value.category == "gpu_out_of_memory" and str(raised.value) == OWN_TEXT


def test_a_non_streaming_ordinary_error_is_raised_as_it_was():
    rt = make_runtime(RuntimeError("an ordinary failure"))

    async def go():
        return await rt.chat([{"role": "user", "content": "hi"}])

    with pytest.raises(RuntimeError, match="an ordinary failure") as raised:
        asyncio.run(go())
    assert not isinstance(raised.value, ie.InferenceFailure)


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.fixture
def served(monkeypatch):
    """A FastAPI app over a runtime whose model can be made to fail."""
    holder = SimpleNamespace(runtime=make_runtime(CppException()))
    monkeypatch.setattr(gateway, "get_runtime", lambda: holder.runtime)
    monkeypatch.setattr(peer, "get_runtime", lambda: holder.runtime)
    monkeypatch.setattr(models, "get_runtime", lambda: holder.runtime)
    monkeypatch.setattr(peer, "get_settings", lambda: SimpleNamespace(network=SimpleNamespace(contributor_mode=True)))

    async def record(*a, **kw):
        pass

    monkeypatch.setattr(gateway, "_record_consumed", record)
    monkeypatch.setattr(peer, "_record_served", record)
    app = FastAPI()
    app.include_router(gateway.router, prefix="/v1")
    app.include_router(peer.router, prefix="/peer")
    app.include_router(models.router, prefix="/models")
    holder.client = TestClient(app)
    return holder


BODY = {"model": "test", "messages": [{"role": "user", "content": "hi"}]}


def sse_events(text: str) -> list[dict | str]:
    out = []
    for block in text.split("\n\n"):
        if block.startswith("data: "):
            data = block[6:]
            out.append(data if data == "[DONE]" else json.loads(data))
    return out


def test_the_local_gateway_stream_tells_the_user_what_to_do(served):
    resp = served.client.post("/v1/chat/completions", json={**BODY, "stream": True}, headers={"X-Potluck-Scope": "local"})
    error = sse_events(resp.text)[0]["error"]
    assert error["message"] == OWN_TEXT and error["code"] == "gpu_out_of_memory"
    assert "0xe06d7363" not in resp.text


def test_the_local_gateway_without_streaming_returns_a_clear_503(served):
    resp = served.client.post("/v1/chat/completions", json={**BODY, "stream": False}, headers={"X-Potluck-Scope": "local"})
    assert resp.status_code == 503
    assert resp.json()["detail"] == {"code": "gpu_out_of_memory", "message": OWN_TEXT}


def test_the_model_test_route_says_why(served):
    resp = served.client.post("/models/test/test", json={})
    assert resp.status_code == 503 and resp.json()["detail"] == OWN_TEXT
    served.runtime = make_runtime(RuntimeError("an ordinary failure"))
    resp = served.client.post("/models/test/test", json={})
    assert resp.status_code == 503
    assert resp.json()["detail"] == "Local inference test failed. Check model and runtime settings."


@pytest.mark.parametrize("mode", ["stream", "whole", "batch"])
def test_a_peer_gets_fixed_text_chosen_by_category_for_the_gpu_failure(served, mode):
    body = dict(BODY)
    if mode == "stream":
        body["stream"] = True
    elif mode == "whole":
        body["stream"] = False
    else:
        body.update(stream=True, potluck_batch=True, potluck_model_sha256="a" * 64)
        served.runtime.assert_model_identity = lambda *a, **k: None
    resp = served.client.post("/peer/chat/completions", json=body)
    assert resp.status_code == 200
    assert not any(leak in resp.text for leak in LEAKS), resp.text
    assert "The serving machine's GPU most likely ran out of memory." in resp.text
    assert "gpu_out_of_memory" in resp.text
    assert OWN_TEXT not in resp.text  # the owner's advice is not for someone else's machine


@pytest.mark.parametrize("mode", ["stream", "whole", "batch"])
def test_a_peer_never_receives_the_text_of_an_ordinary_failure(served, mode):
    served.runtime = make_runtime(RuntimeError("secret-path C:\\Users\\me\\prompt.txt exploded"))
    body = dict(BODY)
    if mode == "stream":
        body["stream"] = True
    elif mode == "whole":
        body["stream"] = False
    else:
        body.update(stream=True, potluck_batch=True, potluck_model_sha256="a" * 64)
        served.runtime.assert_model_identity = lambda *a, **k: None
    resp = served.client.post("/peer/chat/completions", json=body)
    assert resp.status_code == 200
    assert not any(leak in resp.text for leak in LEAKS), resp.text
    assert "Inference failed" in resp.text and "inference_error" in resp.text


def test_the_peer_stream_still_delivers_a_good_answer_untouched(served):
    served.runtime = make_runtime(None)
    resp = served.client.post("/peer/chat/completions", json={**BODY, "stream": True})
    events = sse_events(resp.text)
    assert events[0]["choices"][0]["delta"]["content"] == "hello" and events[-1] == "[DONE]"


def test_a_token_that_says_error_is_not_mistaken_for_an_error_event():
    chunk = 'data: {"choices": [{"delta": {"content": "an \\"error\\" in the text"}}]}\n\n'
    assert peer._fixed_error_event(chunk) == chunk
    assert peer._fixed_error_event("data: [DONE]\n\n") == "data: [DONE]\n\n"


# ---------------------------------------------------------------------------
# Circle members and the relayed job path
# ---------------------------------------------------------------------------


def test_the_borrowing_side_knows_the_category_and_words_it_itself():
    from potluck_local.services import circle_link

    assert circle_link.fixed_category("gpu_out_of_memory") == "gpu_out_of_memory"
    assert circle_link.ERROR_TEXT["gpu_out_of_memory"] == "The circle member's GPU most likely ran out of memory."


def test_the_standalone_contributor_client_sends_fixed_text_to_the_coordinator():
    from potluck_local.contributor import client

    assert client._job_error_fields(CppException(), gpu_offload=True) == (
        "oom", "The serving machine's GPU most likely ran out of memory.",
    )
    category, message = client._job_error_fields(RuntimeError("secret-path exploded"), gpu_offload=True)
    assert (category, message) == ("internal", "RuntimeError")
    assert client._job_error_fields(CppException(), gpu_offload=False)[0] == "internal"


def test_the_gpu_self_test_says_why_when_the_card_is_out_of_memory():
    from potluck_local.routers import readiness

    ok, _, detail = asyncio.run(readiness._run_inference_probe(make_runtime(CppException())))
    assert ok is False and detail == OWN_TEXT
    ok, _, detail = asyncio.run(readiness._run_inference_probe(make_runtime(RuntimeError("an ordinary failure"))))
    assert ok is False and detail == "Loaded-model self-test failed"
