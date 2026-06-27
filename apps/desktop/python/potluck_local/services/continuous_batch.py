"""Continuous batching — Phase 1 (single-node) and Phase 2 (distributed pipeline).

PHASE 1 — ContinuousBatcher
  Wraps a WholeModelEngine. Implements vLLM-style dynamic admit/evict with shared
  batched forward passes so many concurrent callers share ONE model instance rather
  than serialising behind a lock.

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

PHASE 2 — DistributedContinuousBatcher
  Drives a PipelineParallelEngine chain (head block + downstream tail) instead of a
  local whole model. Uses the same scheduler (same _Sequence, same admit/evict loop),
  but each step:

    HEAD SIDE — same as Phase 1's _step() up through building the batched forward:
      * Left-pad per-sequence KV caches -> batched DynamicCache for HEAD layers.
      * Build input_ids [B,1], attention_mask [B, kv_max+1], position_ids [B,1].
      * Run head's embed + layer block -> activations h [B, 1, H].
      * Compute drop list (rows that finished last step).

    WIRE — one round-trip to the tail per step:
      * PREFILL: new row admitted -> send op="cb_prefill" with h [1, q, H] + row_id
        -> tail creates that row's per-row KV for its layers, returns first token.
      * DECODE: send op="cb_step" with h [B,1,H] + row_ids + kv_depths + kv_max +
        attn_mask + pos_ids + drop list -> tail builds its own batched cache,
        runs its layers, slices back per-row, evicts dropped rows, returns B tokens.

    PER-ROW KV ACROSS NODES:
      * HEAD: each sequence holds a DynamicCache for the head's layers (same as
        Phase 1 — the existing _Sequence.cache covers head layers only).
      * TAIL: the tail's serve() handler maintains cb_cache: dict[row_id, DynamicCache]
        for the tail's layers. Admit adds an entry (via cb_prefill), evict removes it.
      * CONSISTENCY: the drop list in cb_step is the tail's eviction signal. The head
        evicts its own cache by removing the sequence from _active. Both happen in the
        same step so they stay in sync: a row that finishes is dropped from the tail's
        cb_cache in the SAME step it is removed from the head's _active list.

    OPT-IN: DistributedContinuousBatcher is a separate class. Pass it a loaded
    PipelineParallelEngine. The single-stream, lockstep-batch, and speculative paths
    on PipelineParallelEngine are byte-for-byte unchanged when it is not used.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from typing import Iterator

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
    # Sampling params — per-request so mixed greedy/creative batches work correctly.
    # temperature <= 0 => greedy argmax for this row; generator is then unused.
    temperature: float = 0.0
    top_k: int = 0
    top_p: float = 1.0
    generator: object = None  # torch.Generator | None (seeded per-request)


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

    def submit(self, messages: list[dict], *, max_new: int = 512,
               temperature: float = 0.0, top_k: int = 0, top_p: float = 1.0,
               seed: int | None = None) -> Iterator[str]:
        """Prefill the prompt, admit the sequence, and return a blocking generator
        of text deltas. The generator is safe to consume from any thread. Same
        contract as WholeModelEngine.stream_tokens().

        Sampling parameters
        -------------------
        temperature <= 0 (default 0.0)  ->  greedy argmax for this request.
        temperature > 0                 ->  stochastic sampling per step.
        top_k / top_p                   ->  same semantics as WholeModelEngine.
        seed                            ->  seed for a per-request torch.Generator.
                                            None = global RNG.  Ignored when greedy.

        Each request carries its OWN generator, so a greedy and a sampled request
        in the same batch step are fully independent (different seeds, different RNG
        state; neither perturbs the other).
        """
        seq = self._prefill(messages, max_new=max_new,
                            temperature=temperature, top_k=top_k, top_p=top_p, seed=seed)
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

    def _prefill(self, messages: list[dict], *, max_new: int,
                 temperature: float = 0.0, top_k: int = 0, top_p: float = 1.0,
                 seed: int | None = None) -> _Sequence:
        """Run the prompt forward to populate the sequence's KV cache.

        Serialised by _prefill_lock so concurrent submitters don't call the
        model simultaneously. The prefill runs OUTSIDE the step loop's step,
        so the step loop keeps running while we prefill (it will pick up the
        new sequence on the next iteration after _admit).

        The first decode token (prefill argmax/sample) is drawn here from the
        prefill logits, matching WholeModelEngine.stream_tokens() exactly.
        """
        import torch
        from .sampling import sample_token
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

        # Build a seeded per-request generator BEFORE the prefill so the generator
        # state is consistent whether or not the prefill forward is fast or slow.
        gen = None
        if temperature > 0.0 and seed is not None:
            gen = torch.Generator(device=self._device)
            gen.manual_seed(seed)

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

        # First decode token: use sample_token so temperature/top_k/top_p apply
        # here too, matching the step loop's per-row sampling.
        nxt = sample_token(
            out.logits[0, -1, :],
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            generator=gen,
        )
        prompt_tokens = input_ids[0].tolist()

        eos = self._tok.eos_token_id
        seq = _Sequence(
            request_id=seq_id,
            token_ids=prompt_tokens + [nxt],
            cache=out.past_key_values,   # use the model's returned cache (has KV data)
            max_new=max_new,
            # The prefill's first token counts against the max_new budget,
            # matching WholeModelEngine.stream_tokens() which runs the decode loop
            # for exactly max_new iterations, first of which processes this token.
            generated=1,
            done=(eos is not None and nxt == eos),
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            generator=gen,
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
        import torch
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
        # _prefill_lock serialises this forward against concurrent _prefill calls.
        # On CPU, PyTorch releases the GIL inside BLAS kernels, so without this
        # lock a concurrent _prefill and _step can genuinely overlap, producing
        # corrupted logit outputs for both.  On CUDA the CUDA stream already
        # serialises ops, so this lock is a no-op in that path but harmless.
        with self._prefill_lock:
            with torch.no_grad():
                out = self._model(
                    input_ids=input_ids,
                    attention_mask=attn_mask,
                    past_key_values=batched,
                    position_ids=position_ids,
                    use_cache=True,
                )
            self._n_forward += 1

        # --- Next tokens [B]: per-row sample (or argmax when temperature <= 0) ---
        # Each sequence carries its own generator so a greedy row and a sampled row
        # in the same batch step are fully independent and neither perturbs the other.
        from .sampling import sample_token as _sample_token
        logits_all = out.logits[:, -1, :]   # [B, vocab]
        next_tokens = [
            _sample_token(
                logits_all[i],
                temperature=seqs[i].temperature,
                top_k=seqs[i].top_k,
                top_p=seqs[i].top_p,
                generator=seqs[i].generator,
            )
            for i in range(B)
        ]

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


# ---------------------------------------------------------------------------
# Phase 2: DistributedContinuousBatcher
# ---------------------------------------------------------------------------

class DistributedContinuousBatcher:
    """Distributed continuous batcher — drives a pipeline chain (head + tail) with the
    same dynamic admit/evict scheduler as ContinuousBatcher, but ONE shared chain
    round-trip per step instead of one local model forward.

    The head runs the same left-pad batching logic (Phase 1's _step structure) over
    its own layer block, then sends batched activations to the tail in a single
    op="cb_step" round-trip. The tail maintains per-row KV for its layers and returns
    B next-tokens. Per-row KV consistency across nodes is managed via:
      - ADMIT: op="cb_prefill" creates the row's tail-side KV before it joins decode.
      - EVICT: a drop list piggybacked on the next cb_step tells the tail which
        row_ids to discard from its cb_cache. The head evicts the row from _active
        in the same step, keeping both sides in sync.

    OPT-IN: only used when explicitly constructed. PipelineParallelEngine's existing
    single-stream, lockstep-batch, and speculative paths are byte-for-byte unchanged.

    PUBLIC API (mirrors ContinuousBatcher):
      batcher = DistributedContinuousBatcher(pipeline_engine)
      gen = batcher.submit(messages, max_new=64)
      stats = batcher.stats()
      batcher.stop()
    """

    def __init__(self, engine) -> None:
        """engine: a loaded PipelineParallelEngine (has .parts, .tokenizer, .device,
        .peer, .port, .timeout). Used for its head block; the tail is contacted over
        the wire for each step."""
        # Lazy import so continuous_batch.py stays importable without torch at module
        # level (create_app torch-free invariant). torch is needed here because we
        # drive a pipeline engine — which already required torch to load — so the
        # import guard is already satisfied by the time this constructor runs.
        from potluck_local.services import pipeline_parallel as pp
        from potluck_local.services.pipeline_wire import send_msg, recv_msg

        self._engine = engine
        self._parts = engine.parts
        self._tok = engine.tokenizer
        self._device = engine.device
        self._peer = engine.peer
        self._port = engine.port
        self._timeout = engine.timeout
        self._pp = pp
        self._send_msg = send_msg
        self._recv_msg = recv_msg

        self._admit_lock = threading.Lock()
        self._prefill_lock = threading.Lock()
        self._step_cond = threading.Condition(self._admit_lock)

        self._active: list[_Sequence] = []
        self._pending: list[_Sequence] = []
        self._seq_id = 0

        # drop_queue: row_ids evicted last step, piggybacked on the next cb_step.
        # Written by _step (step thread only), so no extra lock needed.
        self._drop_queue: list[int] = []

        self._n_round_trips = 0   # chain round-trips (prefill + decode steps)
        self._n_steps = 0         # decode steps only
        self._max_batch = 0
        self._n_generated = 0

        self._stop = False
        self._thread = threading.Thread(
            target=self._loop, name="dcb-step-loop", daemon=True)
        self._thread.start()

        # One persistent TCP socket to the tail, opened lazily and re-opened on error.
        # _wire_lock serializes ALL send+recv round-trips (prefill calls from caller
        # threads + decode steps from the step thread share the same socket; interleaving
        # them would garble the framing). Hold _wire_lock for the full send+recv pair.
        self._sock = None
        self._sock_lock = threading.Lock()
        self._wire_lock = threading.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def submit(self, messages: list[dict], *, max_new: int = 512,
               temperature: float = 0.0, top_k: int = 0, top_p: float = 1.0,
               seed: int | None = None) -> Iterator[str]:
        """Prefill the prompt through the full chain, admit the sequence, and return a
        blocking generator of text deltas. Same contract as ContinuousBatcher.submit.

        Sampling parameters
        -------------------
        temperature <= 0 (default 0.0)  ->  greedy: tail argmaxes, existing wire path.
        temperature > 0                 ->  sampling: tail returns per-row top-K; head
            samples with the per-request generator (built from seed).
        top_k / top_p                   ->  same semantics as ContinuousBatcher.submit.
        seed                            ->  reproducible sampling across calls.
        """
        seq = self._prefill(messages, max_new=max_new,
                            temperature=temperature, top_k=top_k, top_p=top_p, seed=seed)
        self._admit(seq)
        return self._drain(seq)

    def stats(self) -> dict:
        return {
            "n_round_trips": self._n_round_trips,
            "n_steps": self._n_steps,
            "max_batch_size": self._max_batch,
            "n_generated": self._n_generated,
        }

    def stop(self) -> None:
        with self._step_cond:
            self._stop = True
            self._step_cond.notify_all()
        self._thread.join(timeout=10.0)
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # ------------------------------------------------------------------
    # Socket management — one persistent connection to the tail per batcher
    # ------------------------------------------------------------------

    def _get_sock(self):
        """Return the persistent tail socket, connecting if needed."""
        with self._sock_lock:
            if self._sock is None:
                self._sock = self._pp._connect_with_retry(
                    self._peer, self._port, self._timeout)
            return self._sock

    def _reset_sock(self):
        """Close and clear the socket so the next _get_sock() reconnects."""
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.close()
                except OSError:
                    pass
                self._sock = None

    # ------------------------------------------------------------------
    # Prefill: head layers locally + op="cb_prefill" to tail
    # ------------------------------------------------------------------

    def _prefill(self, messages: list[dict], *, max_new: int,
                 temperature: float = 0.0, top_k: int = 0, top_p: float = 1.0,
                 seed: int | None = None) -> _Sequence:
        """Run the full-chain prefill for one new sequence.

        Head side: embed + head layer block -> activations h [1, q, H].
        Wire: op="cb_prefill" carries h + row_id + sample_k (>0 when sampling).
              The tail builds that row's per-row tail-side KV and returns either
              the first decoded token (greedy) or top-K logits (sampling).

        Serialised by _prefill_lock: the head model forward is not safe for concurrent
        calls (same as Phase 1). The step loop continues during a prefill — the new
        sequence joins on the next iteration after _admit().

        _Sequence.cache stores head-layer KV only. The tail's per-row KV is stored
        on the tail side, keyed by row_id (== seq.request_id)."""
        import torch
        from transformers import DynamicCache
        from .sampling import sample_from_topk

        sampling = temperature > 0.0
        # K for top-K return: same sizing rule as PipelineParallelEngine.stream_tokens.
        sample_k = max(top_k if (top_k and top_k > 0) else 0, 64) if sampling else 0

        enc = self._tok.apply_chat_template(
            messages, add_generation_prompt=True,
            return_tensors="pt", return_dict=True,
        )
        input_ids = enc["input_ids"].to(self._device)
        prompt_tokens = input_ids[0].tolist()

        # Build a seeded per-request generator BEFORE the prefill so the generator
        # state is consistent whether or not the prefill forward is fast or slow.
        gen = None
        if sampling and seed is not None:
            gen = torch.Generator(device="cpu")  # top-K arrives on CPU from tail
            gen.manual_seed(seed)

        with self._prefill_lock:
            with self._admit_lock:
                self._seq_id += 1
                seq_id = self._seq_id

            # Head prefill. _head_forward acquires _COMPUTE internally; we must NOT
            # hold _COMPUTE ourselves here (non-reentrant lock).
            head_cache = DynamicCache()
            with torch.no_grad():
                h = self._pp._head_forward(
                    self._parts, input_ids, head_cache, 0, self._device)

            # Tail prefill — one round-trip. h is [1, q, H]; start=0 (absolute
            # position of the prompt start at the tail, which also starts from 0).
            # Hold _wire_lock for the full send+recv to prevent interleaving with
            # concurrent prefill calls or the decode step thread.
            with self._wire_lock:
                sock = self._get_sock()
                try:
                    self._send_msg(sock, {
                        "op": "cb_prefill",
                        "row_id": seq_id,
                        "h": h.cpu(),
                        "start": 0,
                        "sample_k": sample_k,
                    })
                    resp = self._recv_msg(sock)
                    self._n_round_trips += 1
                except Exception as e:
                    self._reset_sock()
                    raise RuntimeError(f"distributed batcher prefill failed: {e}") from e

            if sampling and resp.get("topk_vals") is not None:
                nxt = sample_from_topk(
                    resp["topk_vals"], resp["topk_ids"],
                    temperature=temperature, top_k=top_k, top_p=top_p, generator=gen,
                )
            else:
                nxt = int(resp["tok"])

        eos = self._tok.eos_token_id
        return _Sequence(
            request_id=seq_id,
            token_ids=prompt_tokens + [nxt],
            cache=head_cache,     # head layers' KV only
            max_new=max_new,
            generated=1,
            done=(eos is not None and nxt == eos),
            temperature=temperature,
            top_k=top_k,
            top_p=top_p,
            generator=gen,
        )

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
        while True:
            with self._step_cond:
                while not self._stop and not self._active and not self._pending:
                    self._step_cond.wait(timeout=0.1)
                if self._stop and not self._active and not self._pending:
                    break
                self._active.extend(self._pending)
                self._pending.clear()

            self._active = [s for s in self._active if not s.done]
            if not self._active:
                continue

            try:
                self._step()
            except Exception as exc:
                logger.exception("distributed_batcher step error: %s", exc)
                with self._step_cond:
                    for s in self._active:
                        if not s.done:
                            s.out.put(_ERROR)
                            s.out.put(exc)
                            s.out.put(_DONE)
                    self._active.clear()
                self._drop_queue.clear()
                self._reset_sock()

    def _step(self) -> None:
        """Run ONE distributed decode step.

        HEAD SIDE (mirrors Phase 1 _step):
          1. Snapshot active sequences.
          2. Left-pad per-sequence head-layer KV -> batched DynamicCache.
          3. Build input_ids [B,1], attention_mask [B, kv_max+1], position_ids [B,1].
          4. Run embed + head layer block (under _COMPUTE) -> h [B, 1, H].
          5. Slice output batched cache back to per-sequence head caches.

        WIRE (one cb_step round-trip to the tail):
          6. Send op="cb_step" with h, row_ids, kv_depths, kv_max, attn_mask,
             pos_ids, and the drop list accumulated from the previous step.
          7. Tail rebuilds its batched per-row cache, runs its layers, slices back,
             evicts dropped rows, returns B next-tokens.

        EVICT:
          8. Tokens distributed; finished rows added to _drop_queue for the NEXT
             step's wire message. The head removes them from _active immediately.
             The tail removes them when the next cb_step arrives with them in drop.

        ADMIT:
          9. Pending sequences absorbed at the end (join the batch next step)."""
        import torch
        from transformers import DynamicCache

        seqs = self._active
        B = len(seqs)
        self._max_batch = max(self._max_batch, B)
        eos_id = self._tok.eos_token_id

        # --- Head-layer KV depths and max ---
        kv_depths = [s.cache.get_seq_length() for s in seqs]
        kv_max = max(kv_depths)
        n_head_layers = len(seqs[0].cache.layers) if seqs[0].cache.layers else 0

        # --- Build batched head DynamicCache (left-pad shorter sequences) ---
        batched_head = DynamicCache()
        for li in range(n_head_layers):
            k_list, v_list = [], []
            for i, s in enumerate(seqs):
                k = s.cache.layers[li].keys    # [1, H, depth_i, D]
                v = s.cache.layers[li].values
                pad_len = kv_max - kv_depths[i]
                if pad_len > 0:
                    pad_k = torch.zeros(1, k.shape[1], pad_len, k.shape[3],
                                        dtype=k.dtype, device=k.device)
                    pad_v = torch.zeros(1, v.shape[1], pad_len, v.shape[3],
                                        dtype=v.dtype, device=v.device)
                    k = torch.cat([pad_k, k], dim=2)
                    v = torch.cat([pad_v, v], dim=2)
                k_list.append(k)
                v_list.append(v)
            batched_head.update(
                torch.cat(k_list, dim=0),
                torch.cat(v_list, dim=0),
                layer_idx=li,
            )

        # --- input_ids [B, 1], attn_mask [B, kv_max+1], position_ids [B, 1] ---
        input_ids = torch.tensor(
            [s.token_ids[-1] for s in seqs],
            dtype=torch.long, device=self._device,
        ).unsqueeze(1)

        attn_mask = torch.zeros(B, kv_max + 1, dtype=torch.long, device=self._device)
        for i, depth in enumerate(kv_depths):
            attn_mask[i, kv_max - depth:] = 1

        position_ids = torch.tensor(
            [[kv_depths[i]] for i in range(B)],
            dtype=torch.long, device=self._device,
        )

        row_ids = [s.request_id for s in seqs]

        # --- Head forward: embed + layers -> h [B, 1, H] ---
        # Build 4-D attention mask expected by transformers SDPA layers:
        # shape [B, 1, 1, kv_max+1]. True positions -> 0.0, padding -> -inf.
        with torch.no_grad():
            with self._pp._COMPUTE:
                h = self._parts["embed"](input_ids)  # [B, 1, H]
                pe = self._parts["rotary"](h, position_ids)
                dtype_h = h.dtype
                min_val = torch.finfo(dtype_h).min
                # attn_mask [B, kv_max+1] -> [B, 1, 1, kv_max+1] additive mask
                mask_4d = torch.where(
                    attn_mask.bool().unsqueeze(1).unsqueeze(2),
                    torch.zeros((), dtype=dtype_h, device=self._device),
                    torch.full((), min_val, dtype=dtype_h, device=self._device),
                )
                for layer in self._parts["layers"]:
                    h = layer(
                        h,
                        attention_mask=mask_4d,
                        position_ids=position_ids,
                        past_key_values=batched_head,
                        use_cache=True,
                        position_embeddings=pe,
                    )

        # --- Slice head output cache back to per-sequence caches ---
        # batched_head was mutated in-place by the forward; it now has shape
        # [B, H, kv_max+1, D]. Slice out each sequence's real [1, H, depth+1, D].
        n_out = len(batched_head.layers)
        for i, s in enumerate(seqs):
            real_start = kv_max - kv_depths[i]
            new_cache = DynamicCache()
            for li in range(n_out):
                kf = batched_head.layers[li].keys    # [B, H, kv_max+1, D]
                vf = batched_head.layers[li].values
                new_cache.update(
                    kf[i:i+1, :, real_start:, :].clone(),
                    vf[i:i+1, :, real_start:, :].clone(),
                    layer_idx=li,
                )
            s.cache = new_cache

        # --- Wire: one cb_step round-trip to the tail ---
        # Include the drop list from the previous step (piggybacked eviction).
        # Hold _wire_lock for the full send+recv; prefill round-trips from caller
        # threads also hold this lock, so neither can interleave on the socket.
        drop_now = list(self._drop_queue)
        self._drop_queue.clear()

        # Per-row sample_k: 0 = greedy (tail argmaxes), >0 = tail returns top-K.
        # K sizing: max(top_k, 64) for sampled rows, 0 for greedy rows.
        sample_k_list = []
        any_sampled = False
        for s in seqs:
            if s.temperature > 0.0:
                ki = max(s.top_k if (s.top_k and s.top_k > 0) else 0, 64)
                sample_k_list.append(ki)
                any_sampled = True
            else:
                sample_k_list.append(0)

        with self._wire_lock:
            sock = self._get_sock()
            try:
                msg_out = {
                    "op": "cb_step",
                    "rows": row_ids,
                    "drop": drop_now,
                    "h": h.cpu(),
                    "kv_depths": kv_depths,
                    "kv_max": kv_max,
                    "attn_mask": attn_mask.cpu(),
                    "pos_ids": position_ids.cpu(),
                }
                if any_sampled:
                    msg_out["sample_k"] = sample_k_list
                self._send_msg(sock, msg_out)
                resp = self._recv_msg(sock)
                self._n_round_trips += 1
                self._n_steps += 1
            except Exception as e:
                self._reset_sock()
                raise RuntimeError(f"distributed batcher step failed: {e}") from e

        # Resolve next tokens: greedy rows have int in resp["toks"]; sampled rows
        # have None in resp["toks"] and top-K in resp["topk_rows"][i].
        from .sampling import sample_from_topk
        raw_toks = resp["toks"]
        topk_rows = resp.get("topk_rows")  # list[dict|None] or None
        next_tokens = []
        for i, s in enumerate(seqs):
            if any_sampled and topk_rows is not None and topk_rows[i] is not None:
                # Sampled row: head samples from tail's top-K.
                entry = topk_rows[i]
                tok = sample_from_topk(
                    entry["vals"], entry["ids"],
                    temperature=s.temperature, top_k=s.top_k, top_p=s.top_p,
                    generator=s.generator,
                )
                next_tokens.append(tok)
            else:
                # Greedy row (or all-greedy batch): tail already argmaxed.
                next_tokens.append(int(raw_toks[i]))

        # --- Distribute tokens; collect evictions for next step's drop list ---
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
                self._drop_queue.append(s.request_id)

        for i in reversed(finished_indices):
            self._active.pop(i)

        # --- Absorb newly-admitted sequences (join next step) ---
        with self._step_cond:
            if self._pending:
                self._active.extend(self._pending)
                self._pending.clear()

    # ------------------------------------------------------------------
    # Output drain (identical structure to ContinuousBatcher._drain)
    # ------------------------------------------------------------------

    def _drain(self, seq: _Sequence) -> Iterator[str]:
        """Decode token ids from the sequence queue and yield text deltas.
        Emits the prefill's first token immediately (same as Phase 1)."""
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

        if not seq.done:
            first_token = seq.token_ids[-1]
            if eos_id is None or first_token != eos_id:
                yield from _maybe_yield(first_token)

        if seq.done:
            return

        while True:
            item = seq.out.get()
            if item is _ERROR:
                exc = seq.out.get()
                seq.out.get()
                raise RuntimeError(
                    f"distributed batcher step error: {exc}"
                ) from exc
            if item is _DONE:
                break
            yield from _maybe_yield(item)
