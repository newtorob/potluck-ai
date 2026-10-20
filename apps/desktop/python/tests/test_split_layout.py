"""L1 layout checks: where layers and the output head land, from metadata only.

Runs the engine's documented layer rule (split_layout) over a matrix of synthetic
models and device budgets. The same rule is compared against the real binary's
log by `bench/scripts/split_equivalence.py l2`.
"""
import itertools

import pytest

from potluck_local.services.split_layout import (
    CPU, assignment_problems, layer_load_mib, parse_assignment_log, plan_fits,
    predict_layer_assignment, summarize)

LAYERS = (24, 28, 32, 36, 64, 80)
SPLITS = ([1], [1, 1], [10.8, 8.0], [1, 3], [3, 1], [1, 1, 1], [24, 10, 8], [1, 0, 1], [5, 0.01])


@pytest.mark.parametrize("n,split,ngl", [(n, s, g) for n in LAYERS for s in SPLITS for g in (None, 0, 10, 1000)])
def test_every_layer_assigned_once_contiguous_and_deterministic(n, split, ngl):
    a = predict_layer_assignment(n, split, ngl)
    assert a == predict_layer_assignment(n, split, ngl)
    assert len(a) == n + 1  # n layers plus the output head, each exactly once
    assert assignment_problems(a, len(split)) == []
    s = summarize(a)
    assert sum(s["counts"].values()) == n
    for dev, (first, last) in s["ranges"].items():
        assert a[first:last + 1] == [dev] * (last - first + 1)
    for i, share in enumerate(split):
        if share == 0:
            assert i not in a  # a zero share gets nothing, not even the head


@pytest.mark.parametrize("n", LAYERS)
def test_ngl_zero_keeps_everything_on_host(n):
    assert set(predict_layer_assignment(n, [1, 1], 0)) == {CPU}


@pytest.mark.parametrize("n", LAYERS)
def test_partial_offload_puts_early_layers_on_host_and_head_on_a_device(n):
    a = predict_layer_assignment(n, [1, 1], 5)
    assert a[:n - 4] == [CPU] * (n - 4) and CPU not in a[n - 4:]
    assert a[n] != CPU


def test_known_layout_matches_the_real_engine_log():
    # Observed with llama-perplexity at the pinned commit on qwen2.5-0.5b
    # (24 layers), --device MTL0,RPC0 -ts 1,1 -ngl 99: layers 0-12 on MTL0,
    # 13-23 on RPC0, output head on RPC0.
    a = predict_layer_assignment(24, [1, 1], 99)
    assert summarize(a) == {"layers": 24, "ranges": {0: [0, 12], 1: [13, 23]}, "outputDevice": 1,
                            "counts": {0: 13, 1: 11}}


@pytest.mark.parametrize("n", LAYERS)
def test_output_head_lands_on_the_last_device_in_device_order(n):
    # The head is layer index n, the last slot, so the last device in the order
    # receives it when it holds at least one slot's worth of the split. Potluck
    # lists workers first and its own devices last (split_memory.fit_arguments),
    # which keeps the output projection and the logits on the head machine.
    for worker, local in ((8.0, 10.8), (1, 1), (8, 24), (5, 1)):
        a = predict_layer_assignment(n, [worker, local])
        assert a[n] == 1
    assert predict_layer_assignment(n, [0.5, 100])[n] == 1
    assert predict_layer_assignment(n, [100, 0.5])[n] == 0  # a sliver of the last device does not get it
    assert predict_layer_assignment(n, [1, 1, 1])[n] == 2


def test_parse_assignment_log_roundtrip_and_rejects_gaps():
    log = "\n".join(f"0.01.1 D load_tensors: layer {i:3d} assigned to device {'MTL0' if i < 2 else 'RPC0'}, is_swa = 0"
                    for i in range(4))
    assert parse_assignment_log(log) == (["MTL0", "MTL0", "RPC0", "RPC0"], 3)
    assert parse_assignment_log(log.replace("layer   1", "layer   7")) == ([], None)
    assert parse_assignment_log("") == ([], None)


MODELS = [(24, 400), (28, 1000), (36, 2400), (64, 18000), (80, 40000)]
BUDGETS = [[16000], [10000, 8000], [12000, 8000, 6000], [2000, 2000], [500, 400]]


@pytest.mark.parametrize("model,budgets", list(itertools.product(MODELS, BUDGETS)))
def test_devices_stay_within_budget_or_the_plan_is_refused(model, budgets):
    n, model_mib = model
    kv = n * 2.0
    ok, assignment, load = plan_fits(n, model_mib, kv, budgets)
    assert (ok, assignment, load) == plan_fits(n, model_mib, kv, budgets)
    total = model_mib + kv
    if total > sum(budgets) * 1.05:
        assert not ok  # nothing fits: refuse rather than spill
    if ok:
        slot = model_mib / (n + 1) + kv / n
        assert all(load.get(i, 0) <= b + slot for i, b in enumerate(budgets))
        assert sum(load.values()) == pytest.approx(total)


def test_zero_or_negative_budgets_refuse():
    assert plan_fits(32, 4500, 100, [0, 0])[0] is False
    assert plan_fits(32, 4500, 100, [-5, 0])[0] is False
    assert layer_load_mib(2, 30, 4, [0, 0, 1]) == {0: 24.0, 1: 10.0}  # two layers (10 + 2 KV each) and the head (10)
