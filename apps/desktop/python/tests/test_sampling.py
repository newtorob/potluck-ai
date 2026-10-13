"""Sampling tests - services/sampling.py, WholeModelEngine, and ContinuousBatcher.

All model-level tests are gated on torch + transformers and use
Qwen/Qwen2.5-0.5B-Instruct on cpu/fp32 (same fixture as the whole-model tests).
Pure sampling-util tests run without a model but still need torch.

Test groups
-----------
1. PURE UTIL - sample_token unit tests (greedy fallback, top_k, top_p, seed
   reproducibility). No model required; just a synthetic logit tensor.

2. GREEDY UNCHANGED - temperature=0 (and explicit default) produces the same
   tokens as the current argmax path, for both WholeModelEngine and
   ContinuousBatcher. This is the regression gate for the default behavior.

3. DETERMINISM - a fixed seed + temperature>0 reproduces the same sampled
   sequence across two independent calls; a different seed (or no seed) can differ.

4. FILTERING SANITY - top_k=1 == greedy; top_p with tiny value keeps only the
   top token; very low temperature is near-greedy.

5. PER-ROW INDEPENDENCE (ContinuousBatcher) - a greedy request (temp=0) and a
   sampled request (temp>0, seeded) submitted to the same batcher each match
   their standalone single-stream counterpart. Neighbors do not perturb each other.

create_app torch-free invariant
--------------------------------
Importing create_app must not trigger a torch import.  Verified by a test that
imports sampling in a subprocess without torch present -- but since we run inside
the venv (torch present), we verify the invariant structurally: sampling.py does
not import torch at module top (lazy import only), so importing the module itself
is safe in a torch-free environment.
"""

from __future__ import annotations

import threading

import pytest

# Gate the whole module on torch (sampling.py uses torch internally).
torch = pytest.importorskip("torch")
pytest.importorskip("transformers")

from potluck_local.services.pipeline_parallel import WholeModelEngine  # noqa: E402
from potluck_local.services.continuous_batch import ContinuousBatcher  # noqa: E402
from potluck_local.services.sampling import sample_token  # noqa: E402

MODEL_ID = "Qwen/Qwen2.5-0.5B-Instruct"

# ---------------------------------------------------------------------------
# Module-level fixture: one engine load for all model tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def engine():
    return WholeModelEngine(MODEL_ID, device="cpu", dtype=torch.float32)


# ---------------------------------------------------------------------------
# 1. PURE UTIL - sample_token unit tests
# ---------------------------------------------------------------------------

def _make_logits(n=100, *, hot_idx=3, hot_val=10.0, seed=42):
    """Synthetic logits: one very high value at hot_idx; rest small + random."""
    g = torch.Generator()
    g.manual_seed(seed)
    logits = torch.randn(n, generator=g)
    logits[hot_idx] = hot_val   # overwhelmingly dominant token
    return logits


def test_sample_token_greedy_temperature_zero():
    """temperature=0 -> argmax. Must match torch.argmax exactly."""
    logits = _make_logits()
    result = sample_token(logits, temperature=0.0)
    assert result == int(torch.argmax(logits).item())


def test_sample_token_greedy_negative_temperature():
    """Negative temperature also triggers greedy."""
    logits = _make_logits()
    result = sample_token(logits, temperature=-1.0)
    assert result == int(torch.argmax(logits).item())


def test_sample_token_seed_reproducibility():
    """Same seed -> identical token sequence on repeated calls."""
    logits = _make_logits(hot_val=2.0)  # less dominant so sampling matters
    g1 = torch.Generator()
    g1.manual_seed(7)
    g2 = torch.Generator()
    g2.manual_seed(7)
    t1 = sample_token(logits, temperature=1.0, generator=g1)
    t2 = sample_token(logits, temperature=1.0, generator=g2)
    assert t1 == t2, "same seed must produce the same token"


def test_sample_token_different_seeds_can_differ():
    """Different seeds can (and usually do) produce different tokens on a
    flat-ish distribution.  We run many draws to make the probability of
    all matching astronomically low."""
    logits = torch.zeros(50)   # uniform: every token equally likely
    seen = set()
    for s in range(200):
        g = torch.Generator()
        g.manual_seed(s)
        seen.add(sample_token(logits, temperature=1.0, generator=g))
    assert len(seen) > 1, "different seeds should produce different tokens on a uniform distribution"


def test_sample_token_top_k_one_equals_greedy():
    """top_k=1 keeps only the argmax token -> sample == greedy."""
    logits = _make_logits(hot_val=2.0)
    g = torch.Generator()
    g.manual_seed(0)
    result = sample_token(logits, temperature=1.0, top_k=1, generator=g)
    assert result == int(torch.argmax(logits).item())


def test_sample_token_top_p_tiny_keeps_top():
    """top_p near 0 forces nucleus to contain only the highest-probability token."""
    logits = _make_logits(hot_val=10.0)   # dominant token has ~1.0 probability
    g = torch.Generator()
    g.manual_seed(0)
    # top_p=1e-9: only the top token should remain after nucleus filter
    result = sample_token(logits, temperature=1.0, top_p=1e-9, generator=g)
    assert result == int(torch.argmax(logits).item())


def test_sample_token_very_low_temperature_near_greedy():
    """temperature=0.01 is near-deterministic: must pick the dominant token."""
    logits = _make_logits(hot_val=5.0)
    for seed in range(10):
        g = torch.Generator()
        g.manual_seed(seed)
        result = sample_token(logits, temperature=0.01, generator=g)
        assert result == int(torch.argmax(logits).item()), (
            f"seed={seed}: expected greedy-like behavior at temp=0.01"
        )


def test_sample_token_no_generator_does_not_crash():
    """Passing generator=None (default RNG) must not crash."""
    logits = _make_logits()
    result = sample_token(logits, temperature=1.0, generator=None)
    assert isinstance(result, int)
    assert 0 <= result < len(logits)


def test_sample_token_top_k_disabled_zero():
    """top_k=0 means no top-k filter -- must not crash or restrict tokens."""
    logits = torch.ones(50)   # uniform
    seen = set()
    for s in range(50):
        g = torch.Generator()
        g.manual_seed(s)
        seen.add(sample_token(logits, temperature=1.0, top_k=0, generator=g))
    assert len(seen) > 1, "top_k=0 should allow sampling from all tokens"


def test_sample_token_top_p_one_no_restriction():
    """top_p=1.0 means no nucleus filter."""
    logits = torch.ones(50)
    seen = set()
    for s in range(50):
        g = torch.Generator()
        g.manual_seed(s)
        seen.add(sample_token(logits, temperature=1.0, top_p=1.0, generator=g))
    assert len(seen) > 1


# ---------------------------------------------------------------------------
# 2. GREEDY UNCHANGED - temperature=0 default must match original argmax
# ---------------------------------------------------------------------------

def _whole_model_tokens(eng, messages, max_new, *, temperature=0.0, seed=None):
    """Collect raw token ids from WholeModelEngine.stream_tokens (text deltas ->
    we recover ids by re-encoding; simpler: run engine directly for token ids)."""
    from transformers import DynamicCache
    enc = eng.tokenizer.apply_chat_template(
        messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
    ids = enc["input_ids"].to(eng.device)
    attn = enc.get("attention_mask")
    if attn is not None:
        attn = attn.to(eng.device)
    cache = DynamicCache()
    eos_id = eng.tokenizer.eos_token_id
    produced = []
    with torch.no_grad():
        out = eng.model(input_ids=ids, attention_mask=attn,
                        past_key_values=cache, use_cache=True)
        nxt = int(torch.argmax(out.logits[:, -1, :], dim=-1).item())
        for _ in range(max_new):
            if eos_id is not None and nxt == eos_id:
                break
            produced.append(nxt)
            step_ids = torch.tensor([[nxt]], device=eng.device)
            out = eng.model(input_ids=step_ids, past_key_values=cache, use_cache=True)
            nxt = int(torch.argmax(out.logits[:, -1, :], dim=-1).item())
    return produced


def test_whole_model_greedy_unchanged_default(engine):
    """Default temperature=0.0 produces the same tokens as the original argmax path."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    max_new = 16

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    # stream_tokens with default (greedy) params
    deltas = list(engine.stream_tokens(messages, max_new=max_new))
    sampled_text = "".join(deltas)

    assert sampled_text == expected_text, (
        f"greedy unchanged FAIL:\n  got : {sampled_text!r}\n  want: {expected_text!r}"
    )


def test_whole_model_greedy_explicit_temperature_zero(engine):
    """Explicit temperature=0.0 also matches argmax exactly."""
    messages = [{"role": "user", "content": "Reply with: cat"}]
    max_new = 12

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    deltas = list(engine.stream_tokens(messages, max_new=max_new, temperature=0.0))
    assert "".join(deltas) == expected_text


def test_cb_greedy_unchanged_default(engine):
    """ContinuousBatcher with default params (temperature=0) matches argmax."""
    messages = [{"role": "user", "content": "Reply with: dog"}]
    max_new = 12

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    batcher = ContinuousBatcher(engine)
    result = "".join(batcher.submit(messages, max_new=max_new))
    batcher.stop()

    assert result == expected_text, (
        f"cb greedy unchanged FAIL:\n  got : {result!r}\n  want: {expected_text!r}"
    )


def test_cb_greedy_explicit_temperature_zero(engine):
    """Explicit temperature=0.0 in ContinuousBatcher matches argmax."""
    messages = [{"role": "user", "content": "Reply with: fish"}]
    max_new = 12

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    batcher = ContinuousBatcher(engine)
    result = "".join(batcher.submit(messages, max_new=max_new, temperature=0.0))
    batcher.stop()

    assert result == expected_text, (
        f"cb greedy explicit FAIL:\n  got : {result!r}\n  want: {expected_text!r}"
    )


# ---------------------------------------------------------------------------
# 3. DETERMINISM - fixed seed + temperature > 0 is reproducible
# ---------------------------------------------------------------------------

def test_whole_model_deterministic_with_seed(engine):
    """Same seed + temperature produces the same text on two independent calls."""
    messages = [{"role": "user", "content": "Tell me about the ocean."}]
    max_new = 20

    run1 = "".join(engine.stream_tokens(messages, max_new=max_new,
                                        temperature=1.0, seed=42))
    run2 = "".join(engine.stream_tokens(messages, max_new=max_new,
                                        temperature=1.0, seed=42))

    assert run1 == run2, (
        f"determinism FAIL: same seed produced different text:\n  run1: {run1!r}\n  run2: {run2!r}"
    )


def test_whole_model_different_seeds_can_differ(engine):
    """Different seeds with temperature>0 can produce different outputs."""
    messages = [{"role": "user", "content": "Tell me a short story."}]
    max_new = 20

    outputs = set()
    for seed in range(5):
        text = "".join(engine.stream_tokens(messages, max_new=max_new,
                                            temperature=1.5, seed=seed))
        outputs.add(text)

    # With temperature=1.5 and 5 different seeds, at least 2 distinct outputs expected.
    assert len(outputs) >= 2, (
        "expected at least 2 distinct outputs across 5 seeds; "
        f"got: {outputs}"
    )


def test_cb_deterministic_with_seed(engine):
    """ContinuousBatcher: same seed + temperature produces the same text."""
    messages = [{"role": "user", "content": "Tell me about the ocean."}]
    max_new = 20

    batcher1 = ContinuousBatcher(engine)
    run1 = "".join(batcher1.submit(messages, max_new=max_new, temperature=1.0, seed=99))
    batcher1.stop()

    batcher2 = ContinuousBatcher(engine)
    run2 = "".join(batcher2.submit(messages, max_new=max_new, temperature=1.0, seed=99))
    batcher2.stop()

    assert run1 == run2, (
        f"cb determinism FAIL:\n  run1: {run1!r}\n  run2: {run2!r}"
    )


# ---------------------------------------------------------------------------
# 4. FILTERING SANITY
# ---------------------------------------------------------------------------

def test_whole_model_top_k_one_equals_greedy(engine):
    """top_k=1, temperature=1.0 should produce the greedy output (only 1 token kept)."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    max_new = 10

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    result = "".join(engine.stream_tokens(messages, max_new=max_new,
                                          temperature=1.0, top_k=1, seed=0))
    assert result == expected_text, (
        f"top_k=1 FAIL:\n  got : {result!r}\n  want: {expected_text!r}"
    )


def test_whole_model_top_p_tiny_equals_greedy(engine):
    """top_p near 0 forces the nucleus to the single top token -> same as greedy."""
    messages = [{"role": "user", "content": "Reply with the single word: hello"}]
    max_new = 10

    expected_ids = _whole_model_tokens(engine, messages, max_new)
    expected_text = engine.tokenizer.decode(expected_ids, skip_special_tokens=True)

    result = "".join(engine.stream_tokens(messages, max_new=max_new,
                                          temperature=1.0, top_p=1e-6, seed=0))
    assert result == expected_text, (
        f"top_p near-0 FAIL:\n  got : {result!r}\n  want: {expected_text!r}"
    )


# ---------------------------------------------------------------------------
# 5. PER-ROW INDEPENDENCE (ContinuousBatcher)
# ---------------------------------------------------------------------------

def _cb_single_stream_text(eng, messages, max_new, *, temperature=0.0, seed=None):
    """Run a single-request ContinuousBatcher submission and collect text."""
    batcher = ContinuousBatcher(eng)
    text = "".join(batcher.submit(messages, max_new=max_new,
                                  temperature=temperature, seed=seed))
    batcher.stop()
    return text


def test_per_row_independence(engine):
    """A greedy request (temp=0) and a seeded sampled request (temp>0) submitted to
    the same batcher each match their standalone single-request output.  Neighbors
    must not perturb each other.

    Strategy:
      - Compute ground truth for each request in isolation first.
      - Then submit both to the same batcher concurrently (staggered so they overlap).
      - Assert both match.
    """
    greedy_prompt = [{"role": "user", "content": "Reply with: cat"}]
    sampled_prompt = [{"role": "user", "content": "Tell me about stars."}]
    max_new = 15

    # Ground truth: each request alone
    expected_greedy = _cb_single_stream_text(engine, greedy_prompt, max_new,
                                             temperature=0.0)
    expected_sampled = _cb_single_stream_text(engine, sampled_prompt, max_new,
                                              temperature=1.0, seed=7)

    # Now run both in the same batcher concurrently
    batcher = ContinuousBatcher(engine)
    results = {}

    def run(key, prompt, temp, seed_val):
        results[key] = "".join(
            batcher.submit(prompt, max_new=max_new, temperature=temp, seed=seed_val)
        )

    import time
    t_greedy  = threading.Thread(target=run, args=("greedy",  greedy_prompt,  0.0, None))
    t_sampled = threading.Thread(target=run, args=("sampled", sampled_prompt, 1.0, 7))

    t_greedy.start()
    time.sleep(0.02)   # small stagger so they overlap in the step loop
    t_sampled.start()

    t_greedy.join(timeout=120)
    t_sampled.join(timeout=120)
    batcher.stop()

    assert results.get("greedy") == expected_greedy, (
        f"per-row greedy FAIL:\n  batched : {results.get('greedy')!r}\n"
        f"  expected: {expected_greedy!r}"
    )
    assert results.get("sampled") == expected_sampled, (
        f"per-row sampled FAIL:\n  batched : {results.get('sampled')!r}\n"
        f"  expected: {expected_sampled!r}"
    )


# ---------------------------------------------------------------------------
# Torch-free invariant: sampling.py must not import torch at module level
# ---------------------------------------------------------------------------

def test_sampling_module_no_top_level_torch_import():
    """Verify that services/sampling.py can be parsed without torch at module top.

    We check this structurally: read the source and confirm 'import torch' only
    appears inside function bodies (indented), not at the top level.  This ensures
    create_app stays torch-free even after importing sampling.py.
    """
    import ast
    import pathlib

    src_path = (
        pathlib.Path(__file__).parent.parent
        / "potluck_local" / "services" / "sampling.py"
    )
    source = src_path.read_text()
    tree = ast.parse(source)

    # Walk top-level statements only (depth=1 from the module root).
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            assert "torch" not in names, (
                "sampling.py imports torch at module top level: "
                "this breaks the create_app torch-free invariant"
            )
