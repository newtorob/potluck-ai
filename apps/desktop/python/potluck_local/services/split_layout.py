"""Where the pinned engine puts layers in a layer split. Pure arithmetic, no I/O.

Potluck chooses the device order and per-device memory margins (split_memory)
and llama.cpp's --fit chooses the tensor split. What happens after that is a
fixed rule in llama.cpp (src/llama-model.cpp, load_tensors at the pinned
commit): repeating layer il goes to the first device whose cumulative split
fraction exceeds (il - start) / active, the output head is treated as layer
index n_layer, and the token embedding always stays on the CPU. This module
restates that rule so a plan can be checked, and the split-equivalence tool
(bench/scripts/split_equivalence.py) compares it to what the real binary logs.
"""
from __future__ import annotations

import re
import struct

CPU = -1


def _f32(x: float) -> float:
    # The engine does this arithmetic in float32; ties must break the same way.
    return struct.unpack("<f", struct.pack("<f", x))[0]


def predict_layer_assignment(n_layer: int, tensor_split: list[float], n_gpu_layers: int | None = None) -> list[int]:
    """Device index for layers 0..n_layer-1 plus the output head as the last entry.

    CPU (-1) means the layer stays on the host. n_gpu_layers None offloads all.
    """
    if n_layer < 1 or not tensor_split or any(s < 0 for s in tensor_split) or sum(tensor_split) <= 0:
        raise ValueError("invalid layer count or tensor split")
    total = n_layer + 1
    offload = total if n_gpu_layers is None or n_gpu_layers < 0 else min(n_gpu_layers, total)
    start = max(total - offload, 0)
    splits, running = [], 0.0
    for share in tensor_split:
        running = _f32(running + _f32(share))
        splits.append(running)
    splits = [_f32(s / running) for s in splits]
    out = []
    for il in range(total):
        if il < start or offload == 0:
            out.append(CPU)
            continue
        fraction = _f32(_f32(float(il - start)) / _f32(float(offload)))
        out.append(next((i for i, s in enumerate(splits) if s > fraction), len(splits) - 1))
    return out


def summarize(assignment: list[int]) -> dict:
    """Contiguous layer ranges per device and where the output head landed."""
    n_layer = len(assignment) - 1
    ranges: dict[int, list[int]] = {}
    for il, dev in enumerate(assignment[:n_layer]):
        first_last = ranges.setdefault(dev, [il, il])
        first_last[1] = il
    return {"layers": n_layer, "ranges": ranges, "outputDevice": assignment[n_layer],
            "counts": {dev: sum(d == dev for d in assignment[:n_layer]) for dev in ranges}}


def assignment_problems(assignment: list[int], n_devices: int) -> list[str]:
    """Violations of the layout invariants. Empty means the placement is sound."""
    problems = []
    if len(assignment) < 2:
        return ["no layers"]
    if any(not (CPU <= d < n_devices) for d in assignment):
        problems.append("device index out of range")
    # Host layers come first, then devices in order; so the sequence never
    # decreases, which also makes every range contiguous. The output head is
    # the last element, so it cannot sit before the final layer's device.
    if any(b < a for a, b in zip(assignment, assignment[1:])):
        problems.append("layers are not contiguous and in device order")
    return problems


def layer_load_mib(n_layer: int, model_mib: float, kv_mib: float, assignment: list[int]) -> dict[int, float]:
    """Estimated weights plus KV per device. Weights are spread evenly over the
    n_layer + 1 slots (layers and output head); KV exists only on layers. An
    estimate for budget checks, not an allocation."""
    slot, kv_layer = model_mib / (n_layer + 1), kv_mib / n_layer
    load: dict[int, float] = {}
    for il, dev in enumerate(assignment):
        load[dev] = load.get(dev, 0.0) + slot + (kv_layer if il < n_layer else 0.0)
    return load


def budget_split(budgets_mib: list[int]) -> list[float]:
    """Tensor split proportional to per-device budget, the engine's own default
    rule (free memory) once Potluck's margins are subtracted."""
    return [float(max(b, 0)) for b in budgets_mib]


def plan_fits(n_layer: int, model_mib: float, kv_mib: float, budgets_mib: list[int]) -> tuple[bool, list[int], dict[int, float]]:
    """Advisory fit check: split by budget, then compare each device's estimated
    load to its budget. Any device over budget, or no budget, is a refusal."""
    if sum(max(b, 0) for b in budgets_mib) <= 0:
        return False, [], {}
    assignment = predict_layer_assignment(n_layer, budget_split(budgets_mib))
    load = layer_load_mib(n_layer, model_mib, kv_mib, assignment)
    slot = model_mib / (n_layer + 1) + kv_mib / n_layer
    # One layer of rounding slack: a split by fraction cannot cut a layer.
    ok = all(load.get(i, 0.0) <= max(b, 0) + slot for i, b in enumerate(budgets_mib)) \
        and sum(load.values()) <= sum(max(b, 0) for b in budgets_mib) + slot
    return ok, assignment, load


ASSIGNED = re.compile(r"load_tensors: layer +(\d+) assigned to device (\S+?),")


def parse_assignment_log(text: str) -> tuple[list[str], int | None]:
    """Device name per layer, in layer order, from a verbose engine log. The
    last entry is the output head. Returns ([], None) when the log has none."""
    rows = {}
    for match in ASSIGNED.finditer(text):
        rows[int(match[1])] = match[2]
    if not rows or sorted(rows) != list(range(len(rows))):
        return [], None
    return [rows[i] for i in range(len(rows))], len(rows) - 1
