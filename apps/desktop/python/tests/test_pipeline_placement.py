"""Unit tests for the topology-aware pipeline planner (pure logic, no mesh).

These pin the placement DECISION RULES the AUTO orchestrator depends on:
  - fits-on-one-box -> whole-model (skip the pipeline entirely)
  - a too-big model -> a split across the fewest, nearest peers
  - the higher-VRAM node holds more layers
  - the lower-RTT peer is ordered first (adjacent on the critical path)
  - the max_nodes cap is respected
  - contiguous per-node blocks always sum to n_layers

The planner is pure + deterministic so we can assert exact layer counts without a
running mesh, peers, or torch device. The module top-imports torch, but the planner
functions themselves are torch-free; importing the module here is fine in the sidecar
venv (torch is installed) and exercises nothing GPU-bound.
"""
from __future__ import annotations

import pytest

from potluck_local.services.pipeline_parallel import (
    _proportional_contiguous_split,
    estimate_model_vram_bytes,
    estimate_params_from_config,
    plan_pipeline_placement,
)

GB = 1e9


def _node(nid, vram_gb=None, tier=None, rtt_ms=0.0):
    return {"id": nid, "vram_gb": vram_gb, "tier": tier, "rtt_ms": rtt_ms}


# ---------------------------------------------------------------------------
# Rule 1: fits-on-one-box -> whole-model
# ---------------------------------------------------------------------------


def test_fits_on_one_box_returns_whole_model():
    # 24GB head, ~10GB model -> the head alone fits; no pipeline.
    head = _node("this", vram_gb=24.0, tier="large")
    peer = _node("10.0.0.2", vram_gb=24.0, tier="large", rtt_ms=5.0)
    placement = plan_pipeline_placement(48, model_bytes=10 * GB, nodes=[head, peer])
    assert placement["mode"] == "whole"
    assert placement["nodes"] == [head]


def test_whole_model_short_circuits_even_with_peers_available():
    # Even if cheaper/closer peers exist, no hop beats no hop when the head fits.
    head = _node("this", vram_gb=48.0, tier="large")
    peers = [_node("a", vram_gb=8.0, rtt_ms=1.0), _node("b", vram_gb=8.0, rtt_ms=2.0)]
    placement = plan_pipeline_placement(60, model_bytes=20 * GB, nodes=[head, *peers])
    assert placement["mode"] == "whole"


# ---------------------------------------------------------------------------
# Rule: a too-big model splits across peers
# ---------------------------------------------------------------------------


def test_too_big_model_splits():
    # 8GB head + 8GB peer, ~12GB model -> must split (head alone can't hold it).
    head = _node("this", vram_gb=8.0, tier="mid")
    peer = _node("10.0.0.2", vram_gb=8.0, tier="mid", rtt_ms=5.0)
    placement = plan_pipeline_placement(40, model_bytes=12 * GB, nodes=[head, peer])
    assert placement["mode"] == "pipeline"
    assert len(placement["order"]) == 2
    assert sum(placement["layers"]) == 40
    assert placement["split2"] == 0  # 2-node chain


def test_split_raises_path_when_no_peers_but_too_big_returns_pipeline_with_no_extra():
    # No peers at all + model too big for head: planner still returns a pipeline plan
    # (it can only place on the head); the orchestrator is what raises "no peers".
    head = _node("this", vram_gb=4.0, tier="low")
    placement = plan_pipeline_placement(32, model_bytes=20 * GB, nodes=[head])
    assert placement["mode"] == "pipeline"
    assert placement["order"] == [head]
    assert placement["layers"] == [32]


# ---------------------------------------------------------------------------
# Rule: higher-VRAM node gets more layers
# ---------------------------------------------------------------------------


def test_higher_vram_node_gets_more_layers():
    # Head 24GB, peer 8GB -> head should hold strictly more layers than the peer.
    head = _node("this", vram_gb=24.0, tier="large")
    peer = _node("10.0.0.2", vram_gb=8.0, tier="mid", rtt_ms=5.0)
    placement = plan_pipeline_placement(32, model_bytes=40 * GB, nodes=[head, peer])
    assert placement["mode"] == "pipeline"
    head_layers, peer_layers = placement["layers"]
    assert head_layers > peer_layers
    assert head_layers + peer_layers == 32
    # ~3:1 capacity ratio -> roughly 3:1 layers (24 vs 8).
    assert head_layers == 24 and peer_layers == 8


def test_proportional_split_direct():
    # 3 nodes, 2:1:1 capacity over 40 layers -> 20/10/10.
    counts = _proportional_contiguous_split(40, [16.0, 8.0, 8.0])
    assert counts == [20, 10, 10]
    assert sum(counts) == 40


# ---------------------------------------------------------------------------
# Rule: lower-RTT peer is ordered first
# ---------------------------------------------------------------------------


def test_lower_rtt_peer_ordered_first():
    head = _node("this", vram_gb=4.0, tier="low")
    far = _node("far", vram_gb=8.0, tier="mid", rtt_ms=120.0)
    near = _node("near", vram_gb=8.0, tier="mid", rtt_ms=3.0)
    # model needs all three nodes; check the chosen ORDER puts near before far.
    placement = plan_pipeline_placement(48, model_bytes=18 * GB, nodes=[head, far, near])
    assert placement["mode"] == "pipeline"
    order_ids = [n["id"] for n in placement["order"]]
    assert order_ids[0] == "this"  # head always first
    assert order_ids.index("near") < order_ids.index("far")


def test_fewest_hops_picks_the_nearest_sufficient_peer():
    # Head can't fit; ONE 16GB peer is enough. Two peers offered; pick the nearer one
    # and DON'T add the second (fewest hops).
    head = _node("this", vram_gb=4.0, tier="low")
    near_big = _node("near", vram_gb=16.0, tier="large", rtt_ms=2.0)
    far_big = _node("far", vram_gb=16.0, tier="large", rtt_ms=90.0)
    placement = plan_pipeline_placement(40, model_bytes=12 * GB, nodes=[head, near_big, far_big])
    assert placement["mode"] == "pipeline"
    order_ids = [n["id"] for n in placement["order"]]
    assert order_ids == ["this", "near"]  # second peer not needed


# ---------------------------------------------------------------------------
# Rule: max_nodes cap respected
# ---------------------------------------------------------------------------


def test_max_nodes_cap_respected():
    head = _node("this", vram_gb=2.0, tier="low")
    peers = [_node(f"p{i}", vram_gb=4.0, tier="low", rtt_ms=float(i)) for i in range(5)]
    placement = plan_pipeline_placement(60, model_bytes=200 * GB, nodes=[head, *peers], max_nodes=3)
    assert placement["mode"] == "pipeline"
    assert len(placement["order"]) <= 3
    assert sum(placement["layers"]) == 60


def test_max_nodes_two_caps_to_single_peer():
    head = _node("this", vram_gb=2.0, tier="low")
    peers = [_node(f"p{i}", vram_gb=4.0, tier="low", rtt_ms=float(i)) for i in range(3)]
    placement = plan_pipeline_placement(48, model_bytes=200 * GB, nodes=[head, *peers], max_nodes=2)
    assert len(placement["order"]) == 2
    assert placement["split2"] == 0


# ---------------------------------------------------------------------------
# Rule: contiguous blocks sum to n_layers + split/split2 mapping
# ---------------------------------------------------------------------------


def test_contiguous_blocks_sum_and_map_to_split_split2():
    head = _node("this", vram_gb=8.0, tier="mid")
    mid = _node("mid", vram_gb=8.0, tier="mid", rtt_ms=4.0)
    tail = _node("tail", vram_gb=8.0, tier="mid", rtt_ms=8.0)
    placement = plan_pipeline_placement(33, model_bytes=200 * GB, nodes=[head, mid, tail])
    assert placement["mode"] == "pipeline"
    a, b, c = placement["layers"]
    assert a + b + c == 33
    # split = head count; split2 = head + mid count; tail = [split2:n]
    assert placement["split"] == a
    assert placement["split2"] == a + b


def test_every_node_gets_at_least_one_layer():
    # Tiny model, three nodes: each stage must hold >=1 layer (a 0-layer stage is broken).
    counts = _proportional_contiguous_split(3, [100.0, 1.0, 1.0])
    assert all(c >= 1 for c in counts)
    assert sum(counts) == 3


def test_more_nodes_than_layers_is_clamped():
    # 2 layers, 4 nodes: can't give everyone a layer; n_layers floored up to node count
    # so the sum stays >= node count and never produces a 0-count block.
    counts = _proportional_contiguous_split(2, [1.0, 1.0, 1.0, 1.0])
    assert sum(counts) == len(counts)
    assert all(c >= 1 for c in counts)


# ---------------------------------------------------------------------------
# VRAM / param estimation heuristics
# ---------------------------------------------------------------------------


def test_estimate_model_vram_bytes_applies_overhead():
    # 7B params fp16 -> 14GB weights; with the conservative overhead factor it must be
    # strictly larger than raw weights (we never under-claim the footprint).
    raw = 7_000_000_000 * 2
    est = estimate_model_vram_bytes(7_000_000_000, dtype_bytes=2)
    assert est > raw
    assert est == pytest.approx(raw * 1.3, rel=1e-6)


def test_estimate_model_vram_bytes_falls_back_to_tier():
    # No param count -> tier-based guess; a 'large' tier guess must exceed a 'low' one.
    big = estimate_model_vram_bytes(None, fallback_tier="large")
    small = estimate_model_vram_bytes(None, fallback_tier="low")
    assert big > small > 0


class _FakeConfig:
    hidden_size = 4096
    num_hidden_layers = 32
    vocab_size = 128256
    intermediate_size = 14336
    num_attention_heads = 32
    num_key_value_heads = 8
    tie_word_embeddings = False


def test_estimate_params_from_config_is_in_the_right_ballpark():
    # Llama-3-8B-ish config -> roughly 8B params (within a wide tolerance; the estimate
    # is approximate by design and only needs to land the right VRAM bucket).
    n = estimate_params_from_config(_FakeConfig())
    assert n is not None
    assert 6.5e9 < n < 9.5e9


def test_estimate_params_returns_none_on_sparse_config():
    class Sparse:
        pass

    assert estimate_params_from_config(Sparse()) is None


def test_empty_nodes_raises():
    with pytest.raises(ValueError):
        plan_pipeline_placement(32, model_bytes=10 * GB, nodes=[])
