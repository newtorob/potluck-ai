"""Automatic context-window (n_ctx) sizing.

Picks n_ctx per model instead of one fixed default for everything, so a small
model on a roomy machine gets to use its long context, and a big model on a
tight machine doesn't try to allocate a KV cache that won't fit. Uses only
catalog data (context window, layer count, on-disk size) plus total RAM. No
GGUF parsing.

The KV-cost estimate is deliberately CONSERVATIVE (it over-estimates per-token
cost): over-allocating the KV cache risks an OOM at load, while under-allocating
only shortens the usable context. When the math is uncertain we pick the
smaller, safe context.
"""
from __future__ import annotations

# Standard context sizes we round down to (descending). Keeps n_ctx on familiar
# boundaries instead of an odd number from the RAM division.
_CTX_LADDER = (131072, 65536, 32768, 16384, 8192, 4096, 2048)
_MIN_CTX = 2048
_DEFAULT_MAX_CTX = 8192  # used when a model's max context is unknown

# Conservative KV cache cost per layer per token (bytes): fp16 K+V with an
# upper-bound n_embd_kv ~2048 => 2 (K,V) * 2048 * 2 bytes = 8 KiB. Real GQA
# models are smaller, so this errs toward a smaller, safe context.
_KV_BYTES_PER_LAYER_TOKEN = 8 * 1024


def _round_down_to_ladder(n: int) -> int:
    for step in _CTX_LADDER:
        if n >= step:
            return step
    return _MIN_CTX


def auto_context_size(
    model_max_ctx: int,
    size_bytes: int,
    num_layers: int,
    ram_total_gb: float,
) -> int:
    """Largest sensible n_ctx that the model supports AND fits in RAM alongside
    its weights, rounded down to a standard size. Never below _MIN_CTX."""
    model_max = model_max_ctx if model_max_ctx and model_max_ctx > 0 else _DEFAULT_MAX_CTX
    weights_gb = max(0.0, size_bytes / 1e9)
    # Headroom for the OS, the app, and activation/compute scratch.
    reserve_gb = max(3.0, ram_total_gb * 0.25)
    kv_budget_gb = ram_total_gb - reserve_gb - weights_gb
    if kv_budget_gb <= 0.25:
        # Weights barely fit (or don't); keep the KV cache minimal.
        return min(_MIN_CTX, model_max)

    layers = max(1, num_layers)
    max_tokens_by_ram = int(kv_budget_gb * 1e9 / (layers * _KV_BYTES_PER_LAYER_TOKEN))
    target = min(model_max, max_tokens_by_ram)
    if target < _MIN_CTX:
        return min(_MIN_CTX, model_max)
    return _round_down_to_ladder(target)


def resolve_context_size(
    *,
    configured: int,
    model_max_ctx: int,
    size_bytes: int,
    num_layers: int,
    ram_total_gb: float,
) -> int:
    """Resolve the n_ctx to load a model with.

    configured > 0  -> honor the user's pin, capped at the model's max context.
    configured == 0 -> auto-tune from the model + available RAM.
    """
    if configured and configured > 0:
        cap = model_max_ctx if model_max_ctx and model_max_ctx > 0 else configured
        return max(_MIN_CTX, min(configured, cap))
    return auto_context_size(model_max_ctx, size_bytes, num_layers, ram_total_gb)
