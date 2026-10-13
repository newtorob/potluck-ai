"""Whole-model local-load tests - the auto-pipeline planner's mode=="whole" fallback.

When the topology-aware planner finds a model fits on the head's own VRAM, it returns
mode="whole" and no pipeline is built. This path must actually load and serve the full
model locally.

Three properties are covered:
  1. A real local smoke: load a tiny instruct model via WholeModelEngine on cpu/fp32,
     stream a short completion, and assert non-empty coherent text comes back.
  2. describe() shape - the /health + Models-page contract (mirrors the pipeline engine).
  3. The runtime slot + /health: load_whole_model occupies the same slot the pipeline
     engine does, so is_loaded() flips true and /health's loaded_pipeline reflects it
     with strategy=="whole-model".

The whole file imports torch (the engine top-imports it via pipeline_parallel) and loads
a model, so it is gated on `pytest.importorskip("torch")` - in a torch-free base env the
suite still passes (these simply skip). The model (Qwen2.5-0.5B-Instruct) is expected in
the local HF cache; the load is offline-friendly and small (~1GB fp32).
"""

from __future__ import annotations

import pytest

# Gate the whole module: WholeModelEngine top-imports torch + transformers via
# pipeline_parallel, and these tests materialize a real model.
pytest.importorskip("torch")
pytest.importorskip("transformers")

from fastapi.testclient import TestClient  # noqa: E402

from potluck_local.app import create_app  # noqa: E402
from potluck_local.services.pipeline_parallel import WholeModelEngine  # noqa: E402
from potluck_local.services.runtime import get_runtime  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"


@pytest.fixture
def whole_engine():
    """A WholeModelEngine on cpu/fp32 - deterministic, no GPU, in the HF cache."""
    import torch

    return WholeModelEngine(MODEL_ID, device="cpu", dtype=torch.float32)


def test_whole_model_describe_shape(whole_engine) -> None:
    """describe() returns the /health contract: model_id, strategy, n_layers, device,
    dtype - same shape family as PipelineParallelEngine.describe() so /health is uniform."""
    d = whole_engine.describe()
    assert d["model_id"] == MODEL_ID
    assert d["strategy"] == "whole-model"
    assert isinstance(d["n_layers"], int) and d["n_layers"] > 0
    assert d["device"] == "cpu"
    assert "float32" in d["dtype"]


def test_whole_model_stream_smoke(whole_engine) -> None:
    """Load locally, stream a short completion, assert non-empty coherent text.

    stream_tokens is a blocking generator of text deltas (the same interface the runtime
    serves for the pipeline engine). We greedily decode a tiny answerable prompt and check
    real text - not whitespace, not control tokens - comes back, suffix by suffix."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    deltas = list(whole_engine.stream_tokens(messages, max_new=16))

    text = "".join(deltas)
    assert deltas, "stream produced no deltas"
    assert text.strip(), f"stream produced only whitespace: {text!r}"
    # Greedy decode on a trivial instruct prompt should contain the asked-for word.
    assert "hello" in text.lower(), f"unexpected completion: {text!r}"
    # Deltas are suffixes (never the special EOS marker as a literal).
    assert "<|im_end|>" not in text


def test_runtime_load_whole_model_and_health() -> None:
    """load_whole_model occupies the pipeline slot: is_loaded() flips, current_model_id
    matches, and /health's loaded_pipeline reflects it with strategy=='whole-model'. The
    runtime serves it through the unchanged pipeline machinery (same slot, same describe
    surface)."""
    import torch

    runtime = get_runtime()
    # Start clean in case another test left a model loaded.
    if runtime.is_loaded():
        runtime.unload_model()

    try:
        result = runtime.load_whole_model(MODEL_ID, device="cpu", dtype="float32")
        assert result["strategy"] == "whole-model"
        assert result["model_id"] == MODEL_ID
        assert "load_time_seconds" in result

        assert runtime.is_loaded()
        assert runtime.current_model_id() == MODEL_ID

        # /health surfaces it through loaded_pipeline (the shared slot's describe()).
        app = create_app()
        # Loopback, like the frontend: only loopback and household members get
        # the full /health body.
        with TestClient(app, client=("127.0.0.1", 50000)) as client:
            body = client.get("/health").json()
            assert body["model_loaded"] is True
            assert body["active_model_id"] == MODEL_ID
            lp = body["loaded_pipeline"]
            assert lp is not None
            assert lp["strategy"] == "whole-model"
            assert lp["device"] == "cpu"
    finally:
        runtime.unload_model()
        assert not runtime.is_loaded()
        assert runtime.loaded_pipeline() is None
