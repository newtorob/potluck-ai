"""Continuous batching for WholeModelEngine — Phase 1 (single-node).

Implements vLLM-style dynamic admit/evict with shared batched forward passes so many
concurrent callers share ONE model instance rather than serialising behind a lock.

Design overview
---------------

ACTIVE SEQUENCES
  Each in-flight request is a _Sequence: its own DynamicCache (per-request KV),
  current token ids, position counter, done flag, and an output Queue for deltas.

STEP LOOP
  A single background thread drives _step() repeatedly while any sequence is active.
  Each _step():
    1. Snapshot active sequences under the admit lock.
    2. Build ONE batched forward:
       - input_ids [B, 1] — one decode token per sequence.
       - A batched DynamicCache built by left-padding each sequence's per-layer
         KV tensors (shape [1, heads, depth_i, head_dim]) to the same max depth,
         then stacking on the batch axis -> [B, heads, kv_max, head_dim].
       - attention_mask [B, kv_max+1]: each row is 1 for real tokens (right-
         aligned) and 0 for left-padding.
       - position_ids [B, 1]: each sequence's own absolute position (depth_i).
    3. Run model() -> logits [B, 1, vocab], updated cache [B, heads, kv_max+1, hd].
    4. Argmax -> next token per row.
    5. Slice the output cache back into per-sequence DynamicCaches (strip the left-
       pad prefix so each sequence's cache depth grows by exactly 1 per step).
    6. Enqueue tokens; mark finished sequences (EOS / max_new); evict them.

ADMIT (dynamic)
  submit() prefills the prompt (own prefill forward, builds the sequence's
  DynamicCache), then queues the sequence. The step loop picks it up at the next
  iteration boundary under the same admit lock. No step is interrupted; the new
  sequence joins on the next batch.

EVICT (dynamic)
  When a sequence hits EOS or max_new, it is removed from _active without stopping
  the step loop. Other sequences are unaffected. The sequence's KV memory is freed
  when its DynamicCache is garbage-collected.

KV APPROACH AND COST
  Phase 1 rebuilds the batched DynamicCache from individual per-sequence caches on
  every step. Cost: O(B * n_layers * kv_max_depth) tensor-stack operations per step,
  where kv_max_depth grows as decoding progresses. For small B and short sequences
  (tests: B=3, max_new=30) this is fast. Phase 2 upgrade path: paged attention (a
  shared physical KV pool with per-sequence logical mappings) eliminates the per-step
  copy entirely and makes eviction O(1).

CORRECTNESS INVARIANT
  Each sequence's decode is equivalent to running it alone because:
  - Its KV cache accumulates only its own prefix (left-padding zeros don't propagate
    via attention_mask=0 at those positions).
  - Its position_ids are its own absolute position, not shared or relative.
  - The batched forward is identical to N independent forwards for a causally-masked
    decoder when each row's attention_mask correctly masks out the padding.
  Verified by the correctness gate test (token-for-token match vs single-stream).

THREAD SAFETY
  _admit_lock: guards _active and _pending. Held only for list ops (< 1µs).
  _prefill_lock: serialises prefill forwards (each is a full model forward;
    concurrent prefills on the same CPU model would just contend on the GIL and
    produce nonsense cache state — serialise instead).
  _step runs in one background thread; no concurrent step calls.
  submit() blocks until its own prefill is done, then returns a generator that
  pulls from the sequence's queue.

PUBLIC API
  batcher = ContinuousBatcher(whole_model_engine)
  gen = batcher.submit(messages, max_new=64)   # blocking generator of text deltas
  stats = batcher.stats()                       # forward-pass count, max batch, tokens
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

import torch

logger = logging.getLogger(__name__)

# Sentinel values pushed into a sequence's output queue.
_DONE = object()       # normal finish (EOS or max_new)
_ERROR = object()      # step-loop exception; next item is the exception object


@dataclass
class _Sequence:
    """One active or just-admitted generation request."""
    request_id: int
    token_ids: list[int]      # full token history (prompt + generated so far)
    cache: object             # DynamicCache for this sequence's KV
    max_new: int
    generated: int = 0        # decode tokens produced so far
    done: bool = False
    out: queue.Queue = field(default_factory=queue.Queue)


class ContinuousBatcher:
    """Wraps a loaded WholeModelEngine and multiplexes concurrent submit() callers
    through shared batched forward passes. Thread-safe. Drop-in for single-stream
    use: each submit() returns a blocking generator identical to stream_tokens()."""

    def __init__(self, engine) -> None:
        """engine: a loaded WholeModelEngine (has .model, .tokenizer, .device, .n_layers)."""
        self._engine = engine
        self._model = engine.model
        self._tok = engine.tokenizer
        self._device = engine.device
        self._n_layers = engine.n_layers

        # Guards the _active and _pending lists.
        self._admit_lock = threading.Lock()
        # Serialises prefill forwards (model is not safe for concurrent use on CPU).
        self._prefill_lock = threading.Lock()

        self._active: list[_Sequence] = []
        self._pending: list[_Sequence] = []

        self._seq_id = 0

        # Stats counters (written from step thread only; reads are approximate).
        self._n_forward = 0
        self._max_batch = 0
        self._n_generated = 0

        # Step loop wakeup condition.
        self._step_cond = threading.Condition(self._admit_lock)
        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, name="cb-step-loop", daemon=True)
        self._thread.start()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, messages: list[dict], *, max_new: int = 512) -> Iterator[str]:
        """Prefill the prompt, admit the sequence, and return a blocking generator
        of text deltas. The generator is safe to consume from any thread. Same
        contract as WholeModelEngine.stream_tokens()."""
        seq = self._prefill(messages, max_new=max_new)
        self._admit(seq)
        return self._drain(seq)

    def stats(self) -> dict:
        """Counters for tests and monitoring. Reads are not locked; values are
        approximately consistent (step thread is the only writer)."""
        return {
            "n_forward": self._n_forward,
            "max_batch_size": self._max_batch,
            "n_generated": self._n_generated,
        }

    def stop(self) -> None:
        """Shut down the background step loop gracefully."""
        with self._step_cond:
            self._stop = True
            self._step_cond.notify_all()
        self._thread.join(timeout=10.0)

    # ------------------------------------------------------------------
    # Prefill
    # ------------------------------------------------------------------

    def _prefill(self, messages: list[dict], *, max_new: int) -> _Sequence:
        """Run the prompt forward to populate the sequence's KV cache.

        Serialised by _prefill_lock so concurrent submitters don't call the
        model simultaneously. The prefill runs OUTSIDE the step loop's step,
        so the step loop keeps running while we prefill (it will pick up the
        new sequence on the next iteration after _admit)."""
        from transformers import DynamicCache

        enc = self._tok.apply_chat_template(
            messages, add_generation_prompt=True,
            return_tensors="pt", return_dict=True,
        )
        input_ids = enc["input_ids"].to(self._device)
        attn_mask = enc.get("attention_mask")
        if attn_mask is not None:
            attn_mask = attn_mask.to(self._device)

        cache = DynamicCache()

        with self._prefill_lock:
            with self._admit_lock:
                self._seq_id += 1
                seq_id = self._seq_id

            with torch.no_grad():
                out = self._model(
                    input_ids=input_ids,
                    attention_mask=attn_mask,
                    past_key_values=cache,
                    use_cache=True,
                )
            self._n_forward += 1

        nxt = int(torch.argmax(out.logits[:, -1, :], dim=-1).item())
        prompt_tokens = input_ids[0].tolist()

        eos = self._tok.eos_token_id
        seq = _Sequence(
            request_id=seq_id,
            token_ids=prompt_tokens + [nxt],
            cache=out.past_key_values,   # use the model's returned cache (has KV data)
            max_new=max_new,
            # The prefill's first argmax token counts against the max_new budget,
            # matching WholeModelEngine.stream_tokens() which runs the decode loop
            # for exactly max_new iterations, first of which processes this token.
            generated=1,
            done=(eos is not None and nxt == eos),
        )
        return seq

    # ------------------------------------------------------------------
    # Admit
    # ------------------------------------------------------------------

    def _admit(self, seq: _Sequence) -> None:
        with self._step_cond:
            self._pending.append(seq)
            self._step_cond.notify_all()

    # ------------------------------------------------------------------
    # Step loop
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        """Background thread: drive decode steps until stopped."""
        while True:
            with self._step_cond:
                while not self._stop and not self._active and not self._pending:
                    self._step_cond.wait(timeout=0.1)
                if self._stop and not self._active and not self._pending:
                    break
                self._active.extend(self._pending)
                self._pending.clear()

            # Remove already-done sequences (e.g. prefill hit EOS immediately).
            self._active = [s for s in self._active if not s.done]
            if not self._active:
                continue

            try:
                self._step()
            except Exception as exc:
                logger.exception("continuous_batcher step error: %s", exc)
                with self._step_cond:
                    for s in self._active:
                        if not s.done:
                            s.out.put(_ERROR)
                            s.out.put(exc)
                            s.out.put(_DONE)
                    self._active.clear()

    def _step(self) -> None:
        """Run ONE batched decode step over all active sequences."""
        from transformers import DynamicCache

        seqs = self._active
        B = len(seqs)
        self._max_batch = max(self._max_batch, B)

        eos_id = self._tok.eos_token_id

        # --- Gather per-sequence KV depths ---------------------------------
        # DynamicCache.get_seq_length() returns the number of tokens in the KV.
        kv_depths = [s.cache.get_seq_length() for s in seqs]
        kv_max = max(kv_depths) if kv_depths else 0

        n_layers = len(seqs[0].cache.layers) if seqs[0].cache.layers else self._n_layers

        # --- Build batched DynamicCache (left-pad shorter sequences) --------
        #
        # Each sequence's cache has layers[li].keys of shape [1, H, depth_i, D].
        # We left-pad each to kv_max, then stack -> [B, H, kv_max, D].
        batched = DynamicCache()
        for li in range(n_layers):
            k_list, v_list = [], []
            for i, s in enumerate(seqs):
                depth = kv_depths[i]
                k = s.cache.layers[li].keys    # [1, H, depth_i, D]
                v = s.cache.layers[li].values
                pad_len = kv_max - depth
                if pad_len > 0:
                    pad_k = torch.zeros(1, k.shape[1], pad_len, k.shape[3],
                                        dtype=k.dtype, device=k.device)
                    pad_v = torch.zeros(1, v.shape[1], pad_len, v.shape[3],
                                        dtype=v.dtype, device=v.device)
                    k = torch.cat([pad_k, k], dim=2)
                    v = torch.cat([pad_v, v], dim=2)
                k_list.append(k)
                v_list.append(v)

            kb = torch.cat(k_list, dim=0)   # [B, H, kv_max, D]
            vb = torch.cat(v_list, dim=0)
            batched.update(kb, vb, layer_idx=li)

        # --- input_ids [B, 1]: last token of each sequence ----------------
        decode_tokens = [s.token_ids[-1] for s in seqs]
        input_ids = torch.tensor(decode_tokens, dtype=torch.long,
                                 device=self._device).unsqueeze(1)

        # --- attention_mask [B, kv_max + 1] --------------------------------
        # Row i: 0 for left-padding, 1 for real past tokens + the new token.
        attn_mask = torch.zeros(B, kv_max + 1, dtype=torch.long, device=self._device)
        for i, depth in enumerate(kv_depths):
            real_start = kv_max - depth
            attn_mask[i, real_start:] = 1

        # --- position_ids [B, 1]: each sequence's own absolute position ----
        position_ids = torch.tensor(
            [[kv_depths[i]] for i in range(B)],
            dtype=torch.long, device=self._device,
        )

        # --- Forward pass --------------------------------------------------
        with torch.no_grad():
            out = self._model(
                input_ids=input_ids,
                attention_mask=attn_mask,
                past_key_values=batched,
                position_ids=position_ids,
                use_cache=True,
            )
        self._n_forward += 1

        # --- Next tokens [B] -----------------------------------------------
        next_tokens = torch.argmax(out.logits[:, -1, :], dim=-1).tolist()

        # --- Slice output cache back to per-sequence caches ----------------
        #
        # out.past_key_values.layers[li].keys has shape [B, H, kv_max+1, D].
        # For sequence i:
        #   - real tokens start at index (kv_max - kv_depths[i]) in the padded axis.
        #   - after this step the real length is kv_depths[i] + 1.
        #   - slice [real_start : ] to strip the left-padding.
        out_cache = out.past_key_values
        n_out_layers = len(out_cache.layers)
        for i, s in enumerate(seqs):
            real_start = kv_max - kv_depths[i]
            new_cache = DynamicCache()
            for li in range(n_out_layers):
                k_full = out_cache.layers[li].keys     # [B, H, kv_max+1, D]
                v_full = out_cache.layers[li].values
                new_cache.update(
                    k_full[i:i+1, :, real_start:, :].clone(),
                    v_full[i:i+1, :, real_start:, :].clone(),
                    layer_idx=li,
                )
            s.cache = new_cache

        # --- Distribute tokens and handle finish ---------------------------
        finished_indices = []
        for i, s in enumerate(seqs):
            nxt = next_tokens[i]
            is_eos = eos_id is not None and nxt == eos_id
            s.token_ids.append(nxt)
            s.generated += 1
            self._n_generated += 1

            if not is_eos:
                s.out.put(nxt)

            if is_eos or s.generated >= s.max_new:
                s.done = True
                s.out.put(_DONE)
                finished_indices.append(i)

        for i in reversed(finished_indices):
            self._active.pop(i)

        # Absorb newly-admitted sequences for the next step.
        with self._step_cond:
            if self._pending:
                self._active.extend(self._pending)
                self._pending.clear()

    # ------------------------------------------------------------------
    # Output drain: convert token ids to text deltas
    # ------------------------------------------------------------------

    def _drain(self, seq: _Sequence) -> Iterator[str]:
        """Pull token ids from the sequence's queue and yield text deltas.

        The first decode token was produced in _prefill (the argmax of the
        prefill's last logit position). It is stored as the last element of
        seq.token_ids but NOT put in the queue — we emit it here directly so
        callers see the first token without waiting for the first step to complete.

        Text delta strategy: incrementally decode the growing produced[] list
        and yield only the new suffix each time. This handles multi-byte /
        multi-token characters (e.g. unicode, BPE merges) correctly, matching
        WholeModelEngine.stream_tokens() exactly."""
        eos_id = self._tok.eos_token_id
        produced: list[int] = []
        emitted = 0

        def _maybe_yield(token_id: int) -> Iterator[str]:
            nonlocal emitted
            produced.append(token_id)
            text = self._tok.decode(produced, skip_special_tokens=True)
            if len(text) > emitted:
                delta = text[emitted:]
                emitted = len(text)
                yield delta

        # Emit the prefill's first decode token (if not EOS and not already done).
        if not seq.done:
            first_token = seq.token_ids[-1]
            if eos_id is None or first_token != eos_id:
                yield from _maybe_yield(first_token)

        if seq.done:
            return

        # Drain the step loop's queue.
        while True:
            item = seq.out.get()
            if item is _ERROR:
                exc = seq.out.get()
                seq.out.get()   # consume trailing _DONE
                raise RuntimeError(
                    f"continuous batcher step error: {exc}"
                ) from exc
            if item is _DONE:
                break
            # item is an int (token id from the step loop)
            yield from _maybe_yield(item)
