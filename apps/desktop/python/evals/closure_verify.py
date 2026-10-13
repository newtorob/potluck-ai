"""Closure-quantity verification: scoring-challenge foundation test (Mac, CPU).

Tests the quantity the scoring-challenge mechanism depends on:

    For the same fixed sequence O, do a genuine model X and a cheaper model Y
    assign measurably different log-probability? If yes, a verifier can send a
    peer a scoring challenge ("score O under your loaded model") and catch a peer
    that claims X but runs Y, because logprob_Y(O) won't match an anchor's
    logprob_X(O). The margin must beat the honest cross-backend band.
    TODO: measure that band across machines.

Scoring is done low-level (log-softmax over per-position logits, logits_all=True).
The high-level create_completion(logprobs=...) token_logprobs are wrong in the
llama-cpp-python build tested (they returned ~-10 nats for common words like
"of"/"is"); the low-level path gives sane values (bpb ~0.5-1 for English, correct
argmax next-token predictions), so it is used exclusively.

Run (from apps/desktop/python):
    ./.venv/bin/python -m evals.closure_verify
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

from potluck_local.utils.paths import models_dir

LN2 = math.log(2.0)

PROMPTS = [
    "Explain in two sentences why the sky is blue.",
    "Write a haiku about a thunderstorm.",
    "What is the capital of Australia, and why is it not Sydney?",
    "Give three tips for writing clear commit messages.",
    "Summarize what a hash function is for a beginner.",
    "List the planets of the solar system in order from the sun.",
    "What is the difference between TCP and UDP? Answer briefly.",
    "Describe the taste of coffee to someone who has never had it.",
    "Why do leaves change color in autumn? Two sentences.",
    "Name a good reason to use a local LLM instead of a cloud one.",
]


def gguf_path(slug: str) -> str:
    p = Path(models_dir()) / slug / "model.gguf"
    if not p.exists():
        raise SystemExit(f"model not found: {p}")
    return str(p)


def load_model(slug: str, *, n_ctx: int, n_gpu_layers: int):
    from llama_cpp import Llama  # lazy: avoids Metal init at import

    return Llama(
        model_path=gguf_path(slug),
        n_ctx=n_ctx,
        n_gpu_layers=n_gpu_layers,
        logits_all=True,  # required: we read per-position logits
        verbose=False,
    )


def generate(llm, user_prompt: str, *, max_tokens: int) -> str:
    res = llm.create_chat_completion(
        messages=[{"role": "user", "content": user_prompt}],
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return res["choices"][0]["message"]["content"].strip()


def score_text(llm, text: str) -> dict:
    """Unconditional log-prob of `text` under llm, via low-level log-softmax over
    per-position logits. Verified sane (see module docstring). Returns sum_logprob
    (nats), n_tokens, n_bytes, bits_per_byte, avg_token_logprob."""
    toks = llm.tokenize(text.encode("utf-8"), add_bos=True, special=False)
    llm.reset()
    llm.eval(toks)
    scores = llm.scores  # numpy (n_ctx, n_vocab); row i predicts token i+1
    sum_lp = 0.0
    n = 0
    for i in range(1, len(toks)):  # skip BOS (token 0 has no predictor)
        row = scores[i - 1]
        m = row.max()
        logZ = m + np.log(np.exp(row - m).sum())
        sum_lp += float(row[toks[i]] - logZ)
        n += 1
    nbytes = max(1, len(text.encode("utf-8")))
    return {
        "sum_logprob": sum_lp,
        "n_tokens": n,
        "n_bytes": nbytes,
        "bits_per_byte": -sum_lp / (LN2 * nbytes),
        "avg_token_logprob": sum_lp / n if n else float("nan"),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Scoring-challenge foundation test")
    ap.add_argument("--genuine", default="qwen-2.5-3b-instruct-q4",
                    help="X: the genuine / advertised model")
    ap.add_argument("--cheaper", default="qwen-2.5-0.5b-instruct-q4",
                    help="Y: the cheaper model a peer might secretly run")
    ap.add_argument("--n-ctx", type=int, default=2048)
    ap.add_argument("--max-tokens", type=int, default=64)
    ap.add_argument("--n-gpu-layers", type=int, default=0,
                    help="0 = CPU (safe alongside the running app); -1 = full GPU offload")
    args = ap.parse_args()

    print(f"genuine X = {args.genuine}")
    print(f"cheaper Y = {args.cheaper}")
    print("test: fix O = X's own output; score the SAME O under both models (low-level logprob).")
    print("we want |logprob_X(O) - logprob_Y(O)| to be LARGE (margin to beat the fleet band).\n")

    # --- 1. X generates the outputs O and self-scores them. ------------------
    print(f"loading X (n_gpu_layers={args.n_gpu_layers})...")
    x = load_model(args.genuine, n_ctx=args.n_ctx, n_gpu_layers=args.n_gpu_layers)
    data = []
    for p in PROMPTS:
        o = generate(x, p, max_tokens=args.max_tokens)
        data.append({"p": p, "o": o, "x": score_text(x, o)})
    del x
    print("outputs generated + self-scored under X; X unloaded.\n")

    # --- 2. Y scores the same outputs O. ------------------------------------
    print("loading Y to score the same outputs...")
    y = load_model(args.cheaper, n_ctx=args.n_ctx, n_gpu_layers=args.n_gpu_layers)
    for d in data:
        d["y"] = score_text(y, d["o"])
    del y

    # --- 3. Report. ---------------------------------------------------------
    print("\n" + "=" * 86)
    print(f"{'prompt':<34} {'bpb X':>8} {'bpb Y':>8} {'avglp X':>9} {'avglp Y':>9} {'d nats/tok':>11}")
    print("-" * 86)
    tok_deltas = []
    for d in data:
        sx, sy = d["x"], d["y"]
        dtok = sx["avg_token_logprob"] - sy["avg_token_logprob"]  # >0: X more confident
        tok_deltas.append(dtok)
        label = (d["p"][:31] + "...") if len(d["p"]) > 34 else d["p"]
        print(f"{label:<34} {sx['bits_per_byte']:>8.3f} {sy['bits_per_byte']:>8.3f} "
              f"{sx['avg_token_logprob']:>9.3f} {sy['avg_token_logprob']:>9.3f} {dtok:>+11.3f}")
    print("-" * 86)

    mean_bpb_x = sum(d["x"]["bits_per_byte"] for d in data) / len(data)
    mean_bpb_y = sum(d["y"]["bits_per_byte"] for d in data) / len(data)
    mean_dtok = sum(tok_deltas) / len(tok_deltas)
    min_dtok = min(tok_deltas)
    n_xbetter = sum(1 for dt in tok_deltas if dt > 0)

    print(f"mean bpb under X (genuine)        : {mean_bpb_x:.3f}   (sanity: ~0.5-1.5 for English)")
    print(f"mean bpb under Y (cheaper)        : {mean_bpb_y:.3f}")
    print(f"mean |avglp gap| X vs Y           : {mean_dtok:+.3f} nats/token")
    print(f"min gap (worst case)              : {min_dtok:+.3f} nats/token")
    print(f"prompts where X more confident    : {n_xbetter}/{len(data)}")
    print("within-model band is ~0 on one machine (deterministic); real band = cross-backend (fleet, TODO).")
    print("=" * 86)
    if mean_bpb_x > 3.0:
        print("WARN: bpb under X is implausibly high -- scorer sanity failed, do not trust the rest.")
    elif min_dtok > 0.2:
        print("RESULT: the genuine model is consistently + clearly more confident in O than the")
        print("        cheaper one. logprob(O) is model-characteristic, so a scoring challenge")
        print("        separates them with margin. NEXT: measure the cross-backend band on the")
        print("        fleet to confirm the margin survives quant/backend noise.")
    else:
        print("RESULT: the per-sequence logprob gap between models is small/inconsistent. The")
        print("        scoring challenge would have little margin over the fleet band; lean on")
        print("        anchored redundant execution instead, or score harder/longer sequences.")
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
