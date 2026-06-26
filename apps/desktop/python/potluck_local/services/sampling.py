"""Token sampling utilities for local transformers inference.

Provides a single public function, ``sample_token``, used by WholeModelEngine
and ContinuousBatcher when temperature > 0.

Semantics
---------
temperature <= 0  ->  greedy argmax (identical to the original argmax path;
                       no torch.Generator is created, no randomness).

temperature > 0   ->  stochastic sampling:
    1. Scale logits by 1 / temperature.
    2. Apply top-k filtering: keep only the k highest-logit tokens; set all
       others to -inf.  top_k <= 0 means no top-k filter.
    3. Apply top-p (nucleus) filtering: sort descending by probability;
       accumulate until cumulative probability >= top_p; mask everything
       outside the nucleus to -inf.  top_p >= 1.0 means no nucleus filter.
       Safety: always keep at least the single highest-probability token so
       the distribution is never fully masked.
    4. Softmax over the remaining logits.
    5. torch.multinomial sample using ``generator`` (a seeded
       torch.Generator for reproducibility, or None for the default RNG).

Parameters
----------
logits_row : torch.Tensor
    1-D float tensor of shape [vocab_size].  Must be on the same device as
    ``generator`` when one is provided.
temperature : float
    > 0 for sampling; <= 0 for greedy argmax.
top_k : int
    Number of top tokens to keep.  0 or negative = disabled.
top_p : float
    Nucleus probability threshold.  1.0 = disabled.
generator : torch.Generator | None
    A seeded generator for deterministic sampling.  None = default RNG.

Returns
-------
int
    The selected token id.

torch import
------------
torch is imported lazily inside the function body so that this module can be
imported in torch-free environments (e.g. ``create_app`` without a GPU
install) without raising ImportError.  The import is a no-op after the first
call because Python caches it in sys.modules.
"""

from __future__ import annotations


def sample_from_topk(
    values,
    indices,
    *,
    temperature: float,
    top_k: int = 0,
    top_p: float = 1.0,
    generator=None,
) -> int:
    """Sample one token from a PRE-REDUCED top-K set (values, indices).

    Called on the HEAD after the tail returns the top-K logits for a sampling
    step.  The tail returns the K largest logit values with their real vocab
    positions (indices); this function reapplies temperature/top_k/top_p
    filtering over those K entries and maps the sampled position back to the
    real token id.

    Parameters
    ----------
    values : torch.Tensor
        1-D float tensor of shape [K]: the K largest raw logit values.
    indices : torch.Tensor
        1-D int64 tensor of shape [K]: the corresponding real vocab token ids.
    temperature, top_k, top_p, generator
        Same semantics as sample_token.

    Returns
    -------
    int
        The selected real token id.

    Greedy path
    -----------
    temperature <= 0 returns indices[argmax(values)] -- the top-1 token from
    the returned set.  Callers should never reach this branch (greedy uses the
    tail-side argmax wire unchanged), but it is a safe fallback.

    Exactness bound
    ---------------
    Sampling over the returned top-K is identical to sampling over the full
    vocab when:
      * top_k <= K  (the filter keeps at most K anyway), and
      * the top-p nucleus fits within the K largest logits (i.e. the cumulative
        probability of the K largest tokens >= top_p).
    For normal chat parameters (top_k <= 64, top_p <= 0.95, K=64) this holds
    with overwhelming probability.  Very large top_k > K or near-1.0 top_p
    over a very flat distribution may truncate the nucleus to K entries;
    document this at the call site.
    """
    import torch  # lazy: keeps the module importable without torch

    # --- greedy fallback (temperature <= 0) ----------------------------------
    if temperature <= 0.0:
        return int(indices[int(torch.argmax(values).item())].item())

    # Work in fp32 for numerical stability.
    logits = values.float()

    # --- temperature scaling -------------------------------------------------
    logits = logits / temperature

    # --- top-k filtering over the reduced set --------------------------------
    # The reduced set already has at most K entries; top_k <= 0 means no filter.
    if top_k is not None and top_k > 0:
        k = min(top_k, logits.size(-1))
        if k < logits.size(-1):
            topk_vals, _ = torch.topk(logits, k)
            threshold = topk_vals[..., -1]
            logits = logits.masked_fill(logits < threshold, float("-inf"))

    # --- top-p (nucleus) filtering -------------------------------------------
    if top_p is not None and top_p < 1.0:
        sorted_logits, sorted_positions = torch.sort(logits, descending=True)
        probs_sorted = torch.softmax(sorted_logits, dim=-1)
        cumprobs = torch.cumsum(probs_sorted, dim=-1)
        beyond_nucleus = cumprobs - probs_sorted >= top_p
        beyond_nucleus[0] = False  # always keep at least top-1
        remove_mask = beyond_nucleus.scatter(0, sorted_positions, beyond_nucleus)
        logits = logits.masked_fill(remove_mask, float("-inf"))

    # --- softmax + multinomial sample ----------------------------------------
    probs = torch.softmax(logits, dim=-1)
    # Sample a LOCAL position (0..K-1) within the reduced set.
    local_pos = int(torch.multinomial(probs, num_samples=1, generator=generator).item())
    # Map back to the real vocab token id.
    return int(indices[local_pos].item())


def sample_token(
    logits_row,
    *,
    temperature: float,
    top_k: int = 0,
    top_p: float = 1.0,
    generator=None,
) -> int:
    """Sample (or argmax) one token from a 1-D logit tensor.

    See module docstring for full semantics.
    """
    import torch  # lazy: keeps the module importable without torch

    # --- greedy path (temperature <= 0) -----------------------------------
    if temperature <= 0.0:
        return int(torch.argmax(logits_row).item())

    # --- temperature scaling ----------------------------------------------
    logits = logits_row.float()  # work in fp32 for numerical stability
    logits = logits / temperature

    # --- top-k filtering --------------------------------------------------
    if top_k is not None and top_k > 0:
        # Keep only the top_k largest; set the rest to -inf.
        k = min(top_k, logits.size(-1))
        topk_vals, _ = torch.topk(logits, k)
        # threshold: the k-th largest value
        threshold = topk_vals[..., -1]
        logits = logits.masked_fill(logits < threshold, float("-inf"))

    # --- top-p (nucleus) filtering ----------------------------------------
    if top_p is not None and top_p < 1.0:
        # Sort descending by logit, then cumsum of softmax probabilities.
        sorted_logits, sorted_indices = torch.sort(logits, descending=True)
        probs_sorted = torch.softmax(sorted_logits, dim=-1)
        cumprobs = torch.cumsum(probs_sorted, dim=-1)

        # Tokens BEYOND the nucleus: cumprob of the token BEFORE this one
        # already >= top_p.  We shift cumprobs right by one so the token
        # that pushes cumprob over the threshold is still included.
        beyond_nucleus = cumprobs - probs_sorted >= top_p

        # Safety: always keep at least the top-1 token.
        beyond_nucleus[0] = False

        # Map the mask back to the original (unsorted) logit positions.
        remove_mask = beyond_nucleus.scatter(0, sorted_indices, beyond_nucleus)
        logits = logits.masked_fill(remove_mask, float("-inf"))

    # --- softmax + multinomial sample -------------------------------------
    probs = torch.softmax(logits, dim=-1)
    token = torch.multinomial(probs, num_samples=1, generator=generator)
    return int(token.item())
