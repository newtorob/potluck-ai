"""Pipeline-parallel distributed inference — Potluck's WAN-viable "big model" engine.

Each node holds a RESIDENT contiguous block of transformer layers (meta-loaded, so a
node never materializes the whole model) and only small activation tensors cross the
wire per token. This is the sibling of the shipped llama.cpp "Big Model Mode" (RPC
weight-streaming, LAN-only): pipeline-parallel moves activations not weights, so it
survives real networks and runs a model too big for any single peer.

Topology is a chain, head -> [mid ...] -> tail, with the token relayed back upstream:
  head = embed + layers[0:split]            (drives generation)
  mid  = layers[split:split2]               (transparent relay: runs its block,
                                             forwards downstream, relays the token up)
  tail = layers[split2:] + norm + lm_head   (computes the next token)
A mid speaks the tail's wire protocol upstream and the head's downstream, so the head
and tail are identical whether or not a mid is present.

Backends: cuda | mps | cpu | dml (DirectML, AMD/any DX12 GPU). Modes: single-stream,
--concurrency N (independent streams, GPU serialized + I/O overlapped), and --batch N
(one batched forward per step, uniform/lockstep).

Scope: my-machines / trusted-circle only. Activation privacy is unsolved, so this must
stay OUT of the open pool (intermediate activations leak a fraction of a user's tokens
to a malicious mid/tail node).

torch + transformers are imported at module top, so callers that must stay light
(the base sidecar) should import THIS module lazily — only when a pipeline run is
actually requested.

Run standalone for testing (same swarm secret on every node first):
    export SHARD_PSK=$(openssl rand -hex 32)
    python -m potluck_local.services.pipeline_parallel --role tail --split 24 --port 29501
    python -m potluck_local.services.pipeline_parallel --role head --split 24 --peer <ip> --port 29501 --prompt "..."
"""

import argparse
import json
import logging
import os
import socket
import sys
import threading
import time

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer, DynamicCache

# Authenticated + encrypted + pickle-free wire framing (ChaCha20-Poly1305 keyed by
# SHARD_PSK). Works both as a package module and as a standalone script.
try:
    from . import pipeline_wire as wire
    from .pipeline_wire import send_msg, recv_msg
    from .sampling import sample_token, sample_from_topk
except ImportError:  # running this file directly rather than as a package module
    import pipeline_wire as wire
    from pipeline_wire import send_msg, recv_msg
    from sampling import sample_token, sample_from_topk

logger = logging.getLogger(__name__)


def _ensure_wire_key():
    """Load the swarm key (SHARD_PSK) for the wire codec if not already set. The
    programmatic entry points (runtime.load_pipeline -> PipelineParallelEngine, the /peer
    stage endpoint -> PipelineStageWorker) reach the engine WITHOUT going through the CLI
    main(), which is the only place that previously loaded the key -- so without this the
    first send/recv raises 'wire key not set'. Converts the CLI's fail-fast SystemExit
    into a RuntimeError so the HTTP layer returns a readable error instead of dying."""
    try:
        wire.key_from_env()
    except SystemExit as e:
        raise RuntimeError(str(e)) from None

# One GPU per process is the serialized resource. Under concurrency we hold this lock
# only for the forward pass (compute), never across the socket round-trip, so stream A's
# network wait overlaps stream B's compute -- that overlap is what lets aggregate
# throughput beat single-stream when the wire leg is non-trivial (e.g. DERP-relayed).
# Uncontended (concurrency=1) it is a no-op.
_COMPUTE = threading.Lock()

# Transport-layer errors we treat as "edge is dead/frozen" (socket.timeout and every
# ConnectionError subclass are OSError, so this one tuple covers them all). recv_msg
# raises ConnectionError on an auth failure, so a tampered/forged frame lands here too
# and resets the edge instead of being trusted.
EDGE_ERRORS = (OSError, EOFError)


class TransportError(Exception):
    """A pipeline edge died or stalled; carries context, never a bare hang."""


def mesh_socket_path():
    """Local IPC socket of the Go mesh sidecar (same path gateway_routing uses)."""
    if sys.platform == "darwin":
        return "/tmp/potluck-mesh.sock"
    xdg = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return os.path.join(xdg, "potluck-mesh.sock")


def resolve_mesh_peer(name=None):
    """Resolve a household peer's Potluck mesh IP via the Go mesh /peers IPC.

    Rides the existing wg-go mesh (potluck0, 100.64.0.0/10): no SSH tunnel and no
    firewall change, because WireGuard provides encryption + NAT traversal + DERP
    fallback and the nft rule accepts the whole mesh interface. Returns
    (mesh_ip, node_name); picks `name` if given, else the first peer with a mesh IP,
    preferring reachable peers. Mirrors gateway_routing.household_peers()."""
    import http.client

    class _UnixHTTP(http.client.HTTPConnection):
        def __init__(self, path):
            super().__init__("localhost")
            self._path = path

        def connect(self):
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(5.0)
            s.connect(self._path)
            self.sock = s

    conn = _UnixHTTP(mesh_socket_path())
    conn.request("GET", "/peers")
    peers = json.loads(conn.getresponse().read()).get("peers", [])
    conn.close()
    cands = [p for p in peers if p.get("mesh_ip")]
    if name:
        cands = [p for p in cands if p.get("node_name") == name] or cands
    cands.sort(key=lambda p: not p.get("reachable", False))  # reachable first
    if not cands:
        return None, None
    return cands[0]["mesh_ip"], cands[0].get("node_name")


def pick_device():
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def resolve_device(name=""):
    """Map a --device string to a torch device. "dml" lazily creates a DirectML
    device (AMD / any DX12 GPU); empty autodetects cuda|mps|cpu."""
    if name == "dml":
        import torch_directml  # only this node needs it; not a base dependency
        return torch_directml.device()
    return name or pick_device()


def default_dtype(dev):
    """fp16 on Metal (bf16 is slower there) and on DirectML (no bf16 support);
    bf16 elsewhere. NOTE: every node in a chain MUST use the same dtype -- the
    activation tensor on the wire has to match each node's weight dtype."""
    if dev == "mps" or "privateuseone" in str(dev):
        return torch.float16
    return torch.bfloat16


def _empty_cache(device):
    if device == "cuda":
        torch.cuda.empty_cache()
    elif device == "mps":
        torch.mps.empty_cache()


def _sync(device):
    """Block until this device's queued work is done, so a timer around compute is
    accurate (GPU kernels are async). Needed to separate compute from wire time."""
    if device == "cuda":
        torch.cuda.synchronize()
    elif device == "mps":
        torch.mps.synchronize()


def _mem_gb(device):
    if device == "cuda":
        return torch.cuda.memory_allocated(device) / 1e9
    if device == "mps":
        return torch.mps.current_allocated_memory() / 1e9
    return 0.0


def load_parts(model_id, split, role, device="cuda", dtype=torch.bfloat16, split2=0):
    """META-LOAD: place ONLY this node's layer block on `device`; everything else is
    mapped to "meta" so it is never materialized. This is what lets a node hold a slice
    of a model far too big for it -- no node ever loads the full model.

    Two-node (split2=0):  head = embed + layers[0:split];  tail = layers[split:] + norm + lm_head.
    Three-node (split2>0): head = embed + layers[0:split];  mid = layers[split:split2];
                           tail = layers[split2:] + norm + lm_head.
    (embed also lives on the tail when weights are tied, since lm_head then shares it.)"""
    from transformers import AutoConfig
    cfg = AutoConfig.from_pretrained(model_id)
    n_layers = cfg.num_hidden_layers
    tied = bool(getattr(cfg, "tie_word_embeddings", False))
    is_head, is_mid, is_tail = role == "head", role == "mid", role == "tail"
    # this node's contiguous layer range [lo:hi]
    if is_head:
        lo, hi = 0, split
    elif is_mid:
        lo, hi = split, split2
    else:  # tail
        lo, hi = (split2 if split2 else split), n_layers
    # Off-block placement: "meta" (never materialized) on GPU backends. On CPU,
    # accelerate treats a cpu+meta mix as an OFFLOAD scenario and parks even the
    # on-block "cpu" weights on meta (offload hooks our manual forward never calls),
    # so use "cpu" for off-block too -- loads the whole model then we keep our slice.
    # Fine because CPU nodes are RAM-rich; the meta memory win matters on small GPUs.
    is_dml = "privateuseone" in str(device)  # DirectML: device_map can't target it,
    load_dev = "cpu" if is_dml else device   # so load the block on CPU, then move it onto the GPU below.
    off = "cpu" if str(load_dev) == "cpu" else "meta"
    dmap = {
        "model.embed_tokens": load_dev if (is_head or (is_tail and tied)) else off,
        "model.rotary_emb": load_dev,
        "model.norm": load_dev if is_tail else off,
        "lm_head": load_dev if is_tail else off,
    }
    for j in range(n_layers):
        dmap[f"model.layers.{j}"] = load_dev if (lo <= j < hi) else off
    logger.info("pipeline: meta-loading %s block [%d:%d] on %s as %s", model_id, lo, hi, device, role)
    model = AutoModelForCausalLM.from_pretrained(
        model_id, dtype=dtype, device_map=dmap, attn_implementation="eager"
    )
    m = model.model
    parts = {"rotary": m.rotary_emb, "n_layers": n_layers}
    if is_head:
        parts["embed"] = m.embed_tokens
    if is_tail:
        parts["norm"] = m.norm
        parts["lm_head"] = model.lm_head
    kept = [m.layers[i] for i in range(lo, hi)]
    for i, layer in enumerate(kept):  # reindex cache slots 0-based per node
        layer.self_attn.layer_idx = i
    parts["layers"] = torch.nn.ModuleList(kept)
    if is_dml:  # move this node's block from CPU onto the DirectML GPU
        for key in ("embed", "rotary", "norm", "lm_head"):
            if key in parts:
                parts[key] = parts[key].to(device)
        parts["layers"] = parts["layers"].to(device)
    import gc
    gc.collect()
    _empty_cache(device)
    logger.info("pipeline: %s loaded %d/%d layers on %s, mem=%.1fGB",
                role, len(parts["layers"]), n_layers, device, _mem_gb(device))
    return parts


def _mask(q_len, kv_len, start, dtype, device):
    # query i (abs pos start+i) attends key j (abs pos j) iff j <= start+i
    if q_len == 1:
        return None  # single new token attends to all cached keys
    rows = torch.arange(q_len, device=device) + start
    cols = torch.arange(kv_len, device=device)
    allow = cols[None, :] <= rows[:, None]
    minv = torch.finfo(dtype).min
    return torch.where(
        allow,
        torch.zeros((), dtype=dtype, device=device),
        torch.full((), minv, dtype=dtype, device=device),
    )[None, None]


def run_layers(h, parts, cache, start):
    q_len = h.shape[1]
    pos = torch.arange(start, start + q_len, device=h.device).unsqueeze(0)
    pe = parts["rotary"](h, pos)
    mask = _mask(q_len, start + q_len, start, h.dtype, h.device)
    for layer in parts["layers"]:
        h = layer(h, attention_mask=mask, position_ids=pos,
                  past_key_values=cache, use_cache=True, position_embeddings=pe)
    return h


def _connect_with_retry(peer, port, timeout, tries=12):
    """Open a socket to the next hop, retrying transient errors. A direct LAN link can
    throw EHOSTUNREACH (stale ARP) right after the model load; retry rather than die."""
    sock = None
    for attempt in range(tries):
        try:
            sock = socket.socket()
            sock.settimeout(timeout)
            sock.connect((peer, port))
            return sock
        except OSError as e:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            if attempt == tries - 1:
                raise TransportError(
                    f"could not connect to {peer}:{port} after {tries} tries -- {type(e).__name__}: {e}"
                )
            time.sleep(1.0)


def _send_recv(sock, h, abs_start):
    """One supervised round-trip to the next stage: send activations, receive the next
    token. Returns (token, tail_compute_ms, bytes_sent). A dict response carries the tail's
    own compute time; a bare int is an older tail (tail_ms unknown). `token` is an int for
    a single stream or a list of ints for a batch."""
    nbytes = send_msg(sock, (h.cpu(), abs_start))
    resp = recv_msg(sock)
    if isinstance(resp, dict):
        return resp["tok"], float(resp.get("tail_ms", 0.0)), nbytes
    return resp, 0.0, nbytes


def _send_recv_verify(sock, h, abs_start, crop):
    """One SPECULATIVE round-trip: send the K+1 candidate activations plus the prior
    round's rollback length (`crop`), receive the tail's K+1 argmax tokens. This is a
    keyed dict message ({"op": "verify", ...}) distinct from the legacy positional
    (h, abs_start) tuple _send_recv sends, so a spec head and a greedy head can hit the
    SAME tail and the tail dispatches on shape -- the default greedy wire is untouched.
    Returns (toks, tail_compute_ms, bytes_sent); toks is a python list of K+1 ints."""
    nbytes = send_msg(sock, {"op": "verify", "h": h.cpu(), "start": abs_start, "crop": crop})
    resp = recv_msg(sock)
    if isinstance(resp, dict):
        return resp["toks"], float(resp.get("tail_ms", 0.0)), nbytes
    return resp, 0.0, nbytes  # tolerate a bare list from an older spec tail


def accept_prefix(drafts, targets):
    """Greedy speculative acceptance (PURE). drafts = the K tokens the draft proposed for
    positions pos+1..pos+K; targets = the tail's K+1 argmax tokens for positions
    pos..pos+K (targets[j] is the model's own next-token given the first j candidates).

    Accept the longest prefix where draft[j] == target[j] (the target would itself have
    produced that token), then take target[n] as the correction. The committed tokens are
    drafts[:n] + [targets[n]] -- exactly what plain greedy decode would have emitted, which
    is why speculative decoding is EXACT, not approximate. Returns (n_accepted, committed)."""
    n = 0
    for j in range(len(drafts)):
        if drafts[j] == targets[j]:
            n += 1
        else:
            break
    committed = list(drafts[:n]) + [targets[n]]
    return n, committed


def _head_forward(parts, tokens, cache, start, dev, sync=False):
    """Run the head's embed + layer block under the shared GPU lock. sync=True forces the
    device to finish so the caller can time the wire leg separately from compute."""
    with _COMPUTE:
        h = run_layers(parts["embed"](tokens), parts, cache, start)
        if sync:
            _sync(dev)
    return h


def generate_one(parts, tok, peer, port, prompt, max_new, dev, timeout=30.0):
    """One full generation over a fresh connection (=> fresh caches on every node).
    Returns the decoded text plus timing + edge health. Opens and closes its own socket
    so it can be called in a loop against a persistent tail.

    Every edge round-trip is bounded by `timeout`: a dead OR frozen tail raises
    TransportError with context (which step, why) instead of hanging forever."""
    enc = tok.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True, return_tensors="pt", return_dict=True,
    )
    ids = enc["input_ids"].to(dev)
    cache = DynamicCache()
    sock = _connect_with_retry(peer, port, timeout)
    out, start, t0, t_prefill = [], 0, time.time(), None
    bytes_up, rt_ms, head_ms, tail_ms_list, step = 0, [], [], [], 0

    def edge_exchange(h, abs_start):  # one supervised round-trip through the tail, timed
        nonlocal bytes_up
        t = time.time()
        tok_id, tail_ms, nbytes = _send_recv(sock, h, abs_start)
        rt_ms.append((time.time() - t) * 1e3)
        bytes_up += nbytes
        tail_ms_list.append(tail_ms)
        return tok_id

    try:
        with torch.no_grad():
            # prefill the whole prompt
            h = _head_forward(parts, ids, cache, start, dev)
            nxt = edge_exchange(h, start)
            t_prefill = time.time()
            start += ids.shape[1]
            out.append(nxt)
            # decode one token at a time
            for step in range(1, max_new):
                if nxt == tok.eos_token_id:
                    out.pop()
                    break
                _th = time.time()
                h = _head_forward(parts, torch.tensor([[nxt]], device=dev), cache, start, dev, sync=True)
                head_ms.append((time.time() - _th) * 1e3)  # compute incl. lock wait
                nxt = edge_exchange(h, start)
                start += 1
                out.append(nxt)
    except EDGE_ERRORS as e:
        kind = "timed out" if isinstance(e, socket.timeout) else "dropped"
        raise TransportError(
            f"tail {peer}:{port} {kind} at decode step {step} "
            f"(after {timeout:.0f}s budget) -- {type(e).__name__}: {e}"
        ) from e
    finally:
        sock.close()
    if out and out[-1] == tok.eos_token_id:
        out.pop()
    decode_s = (time.time() - t_prefill) if t_prefill else 0.0
    dec_rt = rt_ms[1:]  # skip index 0 (the prefill exchange)
    dec_tail = tail_ms_list[1:]
    dec_net = [max(r - t, 0.0) for r, t in zip(dec_rt, dec_tail)]
    _avg = lambda xs: (sum(xs) / len(xs)) if xs else 0.0
    return {
        "text": tok.decode(out, skip_special_tokens=True),
        "n_tokens": len(out),
        "prefill_s": (t_prefill - t0) if t_prefill else 0.0,
        "decode_s": decode_s,
        "total_s": time.time() - t0,
        "tok_s": len(out) / decode_s if decode_s > 0 else 0.0,
        "hops": len(rt_ms),
        "rt_ms_avg": _avg(rt_ms),
        "rt_ms_max": max(rt_ms) if rt_ms else 0.0,
        "mb_up": bytes_up / 1e6,
        "head_ms_avg": _avg(head_ms),  # per decode token: compute on this node
        "tail_ms_avg": _avg(dec_tail),  # per decode token: compute on the peer
        "net_ms_avg": _avg(dec_net),  # per decode token: wire round-trip (rt - tail)
    }


def generate_batch(parts, tok, peer, port, prompt, batch, max_new, dev, timeout=30.0):
    """Continuous-batching PRIMITIVE: drive `batch` sequences through the pipeline in
    ONE batched forward per step, vs concurrency's N separate batch=1 forwards that take
    turns on the GPU. The win: a batched matmul serves N rows at far less than N x the
    single-row cost, so aggregate throughput climbs past the GPU-serialized ceiling.

    Scope honesty: this uses a UNIFORM-LENGTH lockstep batch (same prompt x batch), so a
    single shared KV cache with broadcast positions/mask is correct with no padding, and
    all rows finish together. Ragged-length inputs + dynamic in-flight admission/eviction
    (paged-attention KV, vLLM-style) is the real follow-on; this measures the compute
    primitive the rest builds on."""
    enc = tok.apply_chat_template(
        [{"role": "user", "content": prompt}],
        add_generation_prompt=True, return_tensors="pt", return_dict=True,
    )
    ids = enc["input_ids"].to(dev).repeat(batch, 1)  # [B, L] -- lockstep, no padding
    cache = DynamicCache()
    sock = _connect_with_retry(peer, port, timeout)
    outs = [[] for _ in range(batch)]
    start, t0, t_prefill = 0, time.time(), None

    def exchange(h, abs_start):
        tok_ids, _, _ = _send_recv(sock, h, abs_start)
        return tok_ids  # list of B tokens

    try:
        with torch.no_grad():
            h = _head_forward(parts, ids, cache, start, dev)
            nxt = exchange(h, start)  # [B]
            t_prefill = time.time()
            start += ids.shape[1]
            for b in range(batch):
                outs[b].append(nxt[b])
            for step in range(1, max_new):
                if nxt[0] == tok.eos_token_id:  # lockstep: identical rows end together
                    for b in range(batch):
                        outs[b].pop()
                    break
                col = torch.tensor([[t] for t in nxt], device=dev)  # [B, 1]
                h = _head_forward(parts, col, cache, start, dev, sync=True)
                nxt = exchange(h, start)
                start += 1
                for b in range(batch):
                    outs[b].append(nxt[b])
    except EDGE_ERRORS as e:
        kind = "timed out" if isinstance(e, socket.timeout) else "dropped"
        raise TransportError(
            f"tail {peer}:{port} {kind} during batched decode -- {type(e).__name__}: {e}"
        ) from e
    finally:
        sock.close()
    for b in range(batch):
        if outs[b] and outs[b][-1] == tok.eos_token_id:
            outs[b].pop()
    decode_s = (time.time() - t_prefill) if t_prefill else 0.0
    total = sum(len(o) for o in outs)
    return {
        "text0": tok.decode(outs[0], skip_special_tokens=True),
        "per_row_tokens": len(outs[0]),
        "total_tokens": total,
        "decode_s": decode_s,
        "total_s": time.time() - t0,
        "agg_tok_s": total / decode_s if decode_s > 0 else 0.0,
        "batch": batch,
    }


class PipelineParallelEngine:
    """Head-side engine the LLMRuntime delegates to when a model is loaded with the
    'pipeline' strategy. Holds this node's head block + tokenizer and drives generation
    across the downstream chain (mid/tail peers), presenting a streaming generate() so the
    chat / gateway / peer endpoints work unchanged (mirrors llama.cpp's stream surface).

    One socket per generation => fresh KV on every node. Greedy decode (the tail argmaxes)
    in v0; temperature/sampling is a follow-on. Scope: my-machines / trusted-circle only
    (activation privacy unsolved -> never the open pool)."""

    def __init__(self, model_id, *, split, peer, port, split2=0,
                 device="", dtype=None, timeout=30.0, draft_model=None, draft_k=4):
        _ensure_wire_key()  # programmatic path skips main(); load SHARD_PSK before any I/O
        self.model_id = model_id
        self.split = split
        self.split2 = split2
        self.peer = peer
        self.port = port
        self.timeout = timeout
        self.device = resolve_device(device)
        self.dtype = dtype if dtype is not None else default_dtype(self.device)
        self.parts = load_parts(model_id, split, "head",
                                device=self.device, dtype=self.dtype, split2=split2)
        self.n_layers = self.parts["n_layers"]
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        # OPT-IN speculative decoding. draft_model unset => the greedy path runs
        # byte-for-byte unchanged. When set, a small local draft proposes draft_k tokens
        # per round and the chain verifies all K+1 in ONE round-trip; output is identical
        # to greedy (exact speculative decoding). The draft loads WHOLE locally on this
        # node (it is small), independent of the head's layer split.
        self.draft_k = max(1, int(draft_k))
        self.draft = None
        if draft_model is not None:
            self.draft = AutoModelForCausalLM.from_pretrained(
                draft_model, dtype=self.dtype, device_map=str(self.device),
                attn_implementation="eager",
            ).eval()
            self.draft_model_id = draft_model
            logger.info("pipeline head: speculative draft %s loaded on %s, K=%d",
                        draft_model, self.device, self.draft_k)

    def describe(self):
        """Status for /health + the Models page (mirrors loaded_rpc_servers)."""
        d = {
            "model_id": self.model_id,
            "strategy": "pipeline",
            "head_layers": self.split,
            "n_layers": self.n_layers,
            "downstream": f"{self.peer}:{self.port}",
            "device": str(self.device),
            "dtype": str(self.dtype),
        }
        if self.draft is not None:  # speculative-decoding opt-in is active
            d["speculative"] = {"draft_model": self.draft_model_id, "K": self.draft_k}
        return d

    def stream_tokens(self, messages, *, max_new=512,
                      temperature=0.0, top_k=0, top_p=1.0, seed=None):
        """Blocking generator yielding text DELTAS as each token is decoded. The caller
        (LLMRuntime.stream_chat) runs this in a worker thread and pumps it onto an async
        queue. Raises TransportError on a dead/stalled edge instead of hanging.

        Sampling parameters
        -------------------
        temperature <= 0 (default 0.0)  ->  GREEDY: uses the existing tuple wire
            (h, abs_start) -> tail argmaxes -> {tok}.  Zero behavior change, zero
            wire format change.  This is the default and is byte-for-byte identical
            to the pre-sampling code path.
        temperature > 0                 ->  SAMPLING: sends op="sample_step" keyed
            dict; tail returns top-K (values + indices); head runs sample_from_topk
            with the per-request generator.  The generator is built once from seed.

        K sizing: clamp(max(top_k, 64), 64, vocab).  For top_k <= 64 and a nucleus
        that fits within the top-64 tokens (normal chat: top_p~0.9, top_k~40) the
        result is identical to sampling over the full vocab.  Document the bound:
        very large top_k > 64 is honored (K scales up to top_k); near-1.0 top_p over
        a very flat distribution may be truncated to K tokens.

        When a speculative draft is loaded (opt-in), this delegates to the spec path,
        which stays greedy (spec decoding is out of scope for sampling).
        """
        if self.draft is not None:
            yield from self._stream_tokens_spec(messages, max_new=max_new)
            return

        sampling = temperature > 0.0
        # K for top-K logit return: at least 64, at least top_k, clamped to vocab size.
        # Only used when sampling; computed once here, forwarded in each step message.
        topk_k = max(top_k if (top_k and top_k > 0) else 0, 64) if sampling else 0

        # Build a seeded per-call generator for deterministic sampling.
        gen = None
        if sampling and seed is not None:
            gen = torch.Generator(device="cpu")  # logits arrive on CPU from tail
            gen.manual_seed(seed)

        enc = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        ids = enc["input_ids"].to(self.device)
        cache = DynamicCache()
        sock = _connect_with_retry(self.peer, self.port, self.timeout)
        produced, start, emitted = [], 0, 0

        def step_greedy(h, abs_start):
            """Greedy: legacy tuple wire — tail argmaxes, returns int."""
            tok_id, _, _ = _send_recv(sock, h, abs_start)
            return tok_id

        def step_sample(h, abs_start):
            """Sampling: op=sample_step — tail returns top-K; head samples."""
            nbytes = send_msg(sock, {
                "op": "sample_step",
                "h": h.cpu(),
                "start": abs_start,
                "topk_k": topk_k,
            })
            resp = recv_msg(sock)
            vals = resp["topk_vals"]
            ids_topk = resp["topk_ids"]
            return sample_from_topk(
                vals, ids_topk,
                temperature=temperature, top_k=top_k, top_p=top_p, generator=gen,
            )

        step = step_sample if sampling else step_greedy

        try:
            with torch.no_grad():
                h = _head_forward(self.parts, ids, cache, start, self.device)
                nxt = step(h, start)
                start += ids.shape[1]
                for _ in range(max_new):
                    if nxt == self.tokenizer.eos_token_id:
                        break
                    produced.append(nxt)
                    text = self.tokenizer.decode(produced, skip_special_tokens=True)
                    if len(text) > emitted:  # emit only the newly-decoded suffix
                        yield text[emitted:]
                        emitted = len(text)
                    h = _head_forward(
                        self.parts, torch.tensor([[nxt]], device=self.device),
                        cache, start, self.device, sync=True)
                    nxt = step(h, start)
                    start += 1
        except EDGE_ERRORS as e:
            kind = "timed out" if isinstance(e, socket.timeout) else "dropped"
            raise TransportError(
                f"pipeline edge {self.peer}:{self.port} {kind} -- {type(e).__name__}: {e}") from e
        finally:
            try:
                sock.close()
            except OSError:
                pass

    def _stream_tokens_spec(self, messages, *, max_new=512, _stats=None):
        """SPECULATIVE-DECODING stream (opt-in). Per round: the local draft proposes K
        tokens greedily; the head embeds [cur, d_1..d_K] (K+1 positions) and the chain
        verifies them in ONE round-trip; accept the longest matching prefix and take the
        target's correction. Output is token-for-token identical to the greedy path --
        exact speculative decoding (accept_prefix only commits tokens the target itself
        would have produced). Yields the SAME text-delta contract as stream_tokens.

        KV rollback: after verifying K+1 positions every node's cache grew by K+1 but only
        (accepted+1) are real. The head + draft crop their caches LOCALLY each round; the
        downstream chain crops LAZILY -- the next verify message carries crop=committed_pos
        and each node (mid, tail) truncates before running, so a round still costs exactly
        one round-trip (no extra rollback trip). _stats: optional dict the caller passes to
        collect round-trip + acceptance counters (used by tests; never affects output)."""
        enc = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        ids = enc["input_ids"].to(self.device)
        eos = self.tokenizer.eos_token_id
        head_cache, draft_cache = DynamicCache(), DynamicCache()
        sock = _connect_with_retry(self.peer, self.port, self.timeout)
        produced, emitted = [], 0
        K = self.draft_k
        round_trips = 0

        def emit_deltas():
            nonlocal emitted
            text = self.tokenizer.decode(produced, skip_special_tokens=True)
            if len(text) > emitted:
                out = text[emitted:]
                emitted = len(text)
                return out
            return None

        try:
            with torch.no_grad():
                # ---- prefill: chain processes the whole prompt; draft fills its cache ----
                h = _head_forward(self.parts, ids, head_cache, 0, self.device)
                preds, _, _ = _send_recv_verify(sock, h, 0, None)
                round_trips += 1
                self.draft(input_ids=ids, past_key_values=draft_cache, use_cache=True)
                cur = preds[-1]  # the target's next token after the prompt
                pos = ids.shape[1]
                emitted_eos = False
                tail_crop = None  # piggybacked downstream rollback (None on the first round)
                steps = 0
                # Emit the prefill token first -- the greedy path appends this same token
                # (its first decode token) before its loop, so the spec path must too or it
                # would drop the leading token. cur is the committed token at `pos`.
                if cur == eos:
                    emitted_eos = True
                else:
                    produced.append(cur)
                    steps += 1
                    d = emit_deltas()
                    if d is not None:
                        yield d
                while not emitted_eos and steps < max_new:
                    if cur == eos:
                        emitted_eos = True
                        break
                    # 1. draft proposes K tokens greedily (feed cur, keep d_1..d_K)
                    drafts, dtok = [], cur
                    for i in range(K):
                        dl = self.draft(input_ids=torch.tensor([[dtok]], device=self.device),
                                        past_key_values=draft_cache, use_cache=True).logits
                        dtok = int(dl[0, -1].argmax())
                        drafts.append(dtok)
                    # 2. verify [cur, d_1..d_K] (K+1 positions) in ONE chain round-trip,
                    #    carrying the prior round's lazy rollback
                    h = _head_forward(self.parts, torch.tensor([[cur] + drafts], device=self.device),
                                      head_cache, pos, self.device, sync=True)
                    targets, _, _ = _send_recv_verify(sock, h, pos, tail_crop)
                    round_trips += 1
                    # 3. greedy acceptance: longest prefix where draft[j] == target[j],
                    #    then the target's correction -- identical to plain greedy output
                    n, committed = accept_prefix(drafts, targets)
                    # 4. roll caches back to the committed length: head + draft NOW
                    #    (local, no round-trip), downstream LAZILY on the next verify
                    pos += n + 1
                    head_cache.crop(pos)
                    draft_cache.crop(pos)
                    tail_crop = pos
                    cur = committed[-1]
                    # 5. emit the committed tokens (stopping at eos), preserving the delta
                    #    contract: append, decode, yield only the new suffix
                    for t in committed:
                        if t == eos:
                            emitted_eos = True
                            break
                        produced.append(t)
                        steps += 1
                        d = emit_deltas()
                        if d is not None:
                            yield d
                        if steps >= max_new:
                            break
                    if emitted_eos:
                        break
                if _stats is not None:
                    _stats["round_trips"] = round_trips
                    _stats["n_tokens"] = len(produced)
        except EDGE_ERRORS as e:
            kind = "timed out" if isinstance(e, socket.timeout) else "dropped"
            raise TransportError(
                f"pipeline edge {self.peer}:{self.port} {kind} -- {type(e).__name__}: {e}") from e
        finally:
            try:
                sock.close()
            except OSError:
                pass


class WholeModelEngine:
    """Head-side engine for WHOLE-MODEL LOCAL LOAD: the model fits on this machine's own
    VRAM, so there is no pipeline split and no peer chain -- plain local transformers
    inference. It exists so the topology-aware planner's mode=="whole" short-circuit has
    something real to load instead of merely signalling "load it elsewhere".

    It presents the SAME surface as PipelineParallelEngine -- stream_tokens(messages,
    *, max_new) (a blocking generator of text deltas) + describe() -- so LLMRuntime can
    serve it through the identical slot/machinery with no special-casing (see
    runtime.load_whole_model). The whole HF model is materialized here (no meta-load, no
    layer split): AutoModelForCausalLM.from_pretrained fully onto the resolved device.

    Greedy decode (argmax) in v0, matching the pipeline engine; temperature/sampling is a
    follow-on. This path has no activation-on-the-wire, so it has no open-pool restriction
    of its own -- it is just the local-serving fallback of the auto-pipeline path."""

    def __init__(self, model_id, *, device="", dtype=None):
        self.model_id = model_id
        self.device = resolve_device(device)
        self.dtype = dtype if dtype is not None else default_dtype(self.device)
        # DirectML mirror of load_parts: device_map can't target a privateuseone device,
        # so load on CPU then move the whole model onto the DML GPU.
        is_dml = "privateuseone" in str(self.device)
        if is_dml:
            model = AutoModelForCausalLM.from_pretrained(self.model_id, dtype=self.dtype)
            model = model.to(self.device)
        else:
            # device_map targets the resolved device directly (cuda|mps|cpu) -- the model
            # fits, so the whole thing lands on one device, no offload/meta split.
            model = AutoModelForCausalLM.from_pretrained(
                self.model_id, dtype=self.dtype, device_map=str(self.device),
            )
        model.eval()
        self.model = model
        self.tokenizer = AutoTokenizer.from_pretrained(model_id)
        # n_layers for /health parity with the pipeline engine's describe().
        self.n_layers = int(getattr(model.config, "num_hidden_layers", 0) or 0)

    def describe(self):
        """Status for /health + the Models page (mirrors PipelineParallelEngine.describe)."""
        return {
            "model_id": self.model_id,
            "strategy": "whole-model",
            "n_layers": self.n_layers,
            "device": str(self.device),
            "dtype": str(self.dtype),
        }

    def stream_tokens(self, messages, *, max_new=512,
                      temperature=0.0, top_k=0, top_p=1.0, seed=None):
        """Blocking generator yielding text DELTAS as each token is decoded -- the SAME
        interface PipelineParallelEngine.stream_tokens exposes, so LLMRuntime serves it
        identically. Manual decode loop emitting only the newly-decoded suffix each step.

        Sampling parameters
        -------------------
        temperature <= 0 (default 0.0)  ->  greedy argmax; identical to the original
                                             argmax behavior and all existing tests.
        temperature > 0                 ->  stochastic sampling via services.sampling.
        top_k   : keep only the k highest-logit tokens before sampling (0 = disabled).
        top_p   : nucleus probability threshold (1.0 = disabled).
        seed    : integer seed for a per-call torch.Generator, giving deterministic
                  sampled output across two calls with the same seed + temperature.
                  None = use the default (global) RNG.  Ignored when temperature <= 0.

        Scope note: PipelineParallelEngine (distributed) and the speculative path stay
        greedy -- sampling applies only to this local whole-model path where logits are
        fully available on this node.
        """
        enc = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        ids = enc["input_ids"].to(self.device)
        attn = enc.get("attention_mask")
        attn = attn.to(self.device) if attn is not None else None
        cache = DynamicCache()
        produced, emitted = [], 0
        eos_id = self.tokenizer.eos_token_id

        # Build a seeded generator once per call when sampling is active.
        # temperature <= 0 => greedy path in sample_token; no generator needed.
        gen = None
        if temperature > 0.0 and seed is not None:
            gen = torch.Generator(device=self.device)
            gen.manual_seed(seed)

        def _next_token(logits):
            return sample_token(
                logits[0, -1, :],
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                generator=gen,
            )

        with torch.no_grad():
            out = self.model(input_ids=ids, attention_mask=attn,
                             past_key_values=cache, use_cache=True)
            nxt = _next_token(out.logits)
            for _ in range(max_new):
                if eos_id is not None and nxt == eos_id:
                    break
                produced.append(nxt)
                text = self.tokenizer.decode(produced, skip_special_tokens=True)
                if len(text) > emitted:  # emit only the newly-decoded suffix
                    yield text[emitted:]
                    emitted = len(text)
                step_ids = torch.tensor([[nxt]], device=self.device)
                out = self.model(input_ids=step_ids, past_key_values=cache, use_cache=True)
                nxt = _next_token(out.logits)


def _cb_build_batched_cache(row_ids: list, cb_cache: dict,
                            kv_max: int, n_layers: int, dev, dtype) -> "DynamicCache":
    """Build a left-padded batched DynamicCache from per-row DynamicCaches.

    Mirrors ContinuousBatcher._step()'s cache construction but operates on this
    node's layers only (0-based reindexed, as load_parts sets layer_idx). Called by
    the tail's cb_step handler; cb_cache maps row_id -> DynamicCache for this node.
    Uses each row's actual cache depth (self-consistent with the tail's own state)."""
    batched = DynamicCache()
    for li in range(n_layers):
        k_list, v_list = [], []
        for i, rid in enumerate(row_ids):
            row_cache = cb_cache[rid]
            # Use the tail's actual cache depth as the authoritative source.
            # This should match kv_depths[i] from the head (same tokens processed),
            # but deriving it from the tail's own cache makes this self-consistent.
            actual_depth = row_cache.get_seq_length()
            if li < len(row_cache.layers):
                k = row_cache.layers[li].keys    # [1, H, actual_depth, D]
                v = row_cache.layers[li].values
            else:
                # Layer not yet in cache. Should not happen after a prefill, but
                # guard defensively with a zero-size placeholder.
                k = torch.zeros(1, 1, 0, 1, dtype=dtype, device=dev)
                v = torch.zeros(1, 1, 0, 1, dtype=dtype, device=dev)
                actual_depth = 0
            pad_len = kv_max - actual_depth
            if pad_len > 0:
                pad_k = torch.zeros(1, k.shape[1], pad_len, k.shape[3],
                                    dtype=k.dtype, device=k.device)
                pad_v = torch.zeros(1, v.shape[1], pad_len, v.shape[3],
                                    dtype=v.dtype, device=v.device)
                k = torch.cat([pad_k, k], dim=2)
                v = torch.cat([pad_v, v], dim=2)
            k_list.append(k)
            v_list.append(v)
        kb = torch.cat(k_list, dim=0)  # [B, H, kv_max, D]
        vb = torch.cat(v_list, dim=0)
        batched.update(kb, vb, layer_idx=li)
    return batched


def _cb_slice_back(out_cache: "DynamicCache", row_ids: list, cb_cache: dict,
                   kv_depths_pre: list, kv_max: int, n_layers: int) -> None:
    """Slice the output batched cache back into per-row DynamicCaches in cb_cache.

    out_cache.layers[li].keys has shape [B, H, kv_max+1, D] after the decode step.
    For row i, real tokens start at index (kv_max - kv_depths_pre[i]) in the padded
    axis; kv_depths_pre[i] is the row's tail-side KV depth BEFORE this step.
    Mirrors ContinuousBatcher._step()'s slice-back logic."""
    for i, rid in enumerate(row_ids):
        real_start = kv_max - kv_depths_pre[i]
        new_cache = DynamicCache()
        for li in range(n_layers):
            k_full = out_cache.layers[li].keys    # [B, H, kv_max+1, D]
            v_full = out_cache.layers[li].values
            new_cache.update(
                k_full[i:i+1, :, real_start:, :].clone(),
                v_full[i:i+1, :, real_start:, :].clone(),
                layer_idx=li,
            )
        cb_cache[rid] = new_cache


def serve_tail(parts, dev, *, port, timeout=30.0, host="0.0.0.0", stop_event=None, ready_event=None):
    """Run the tail stage: accept upstream connections (from a head or a mid), run this
    node's final block + norm + lm_head, and return the next token. One DynamicCache per
    connection => each concurrent stream gets its own KV state; the shared GPU is
    serialized by _COMPUTE (held for compute only, not across the wire), so streams
    overlap I/O while taking turns on the device.

    stop_event: when set, the accept loop exits and the listen socket closes (so a
    PipelineStageWorker can shut a stage down). ready_event: set once bound + listening,
    so a programmatic caller can wait for readiness. Blocks until stopped.

    DISTRIBUTED CONTINUOUS BATCHING (opt-in):
    When the head sends op="cb_prefill" or op="cb_step", the tail switches into
    continuous-batch mode for that connection, maintaining per-row KV caches keyed by
    row_id. The existing single-stream, lockstep-batch, and speculative paths are
    byte-for-byte unchanged."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    srv.settimeout(1.0)  # wake the accept loop periodically to honor stop_event
    if ready_event is not None:
        ready_event.set()
    logger.info("pipeline tail: listening on :%d (edge timeout %.0fs, multi-stream)", port, timeout)
    active = {"n": 0}
    active_lock = threading.Lock()
    stage_dtype = next(parts["layers"].parameters()).dtype  # cast inbound activations to ours
    n_tail_layers = len(parts["layers"])

    def serve(conn, addr):
        with active_lock:
            active["n"] += 1
            n = active["n"]
        logger.info("pipeline tail: upstream %s connected (active streams: %d)", addr, n)
        cache = DynamicCache()
        # Per-row KV for distributed continuous batching (op="cb_prefill"/"cb_step").
        # Populated lazily; only used when the head sends cb ops.
        cb_cache: dict = {}  # row_id -> DynamicCache (this node's layers only)
        steps = 0
        try:
            with torch.no_grad():
                while True:
                    try:
                        msg = recv_msg(conn)
                        op = msg.get("op") if isinstance(msg, dict) else None

                        if op == "cb_prefill":
                            # DISTRIBUTED CB — new row prefill. The head has already run
                            # its layers; h [1, q, H] are the head's output activations.
                            # We run our layer block to build this row's tail-side KV,
                            # run norm+lm_head on the last position, return the first token.
                            # sample_k > 0: return top-K instead of argmax (head samples).
                            row_id = msg["row_id"]
                            h = msg["h"].to(dev, dtype=stage_dtype)
                            start = int(msg["start"])
                            sample_k = int(msg.get("sample_k", 0))
                            row_cache = DynamicCache()
                            with _COMPUTE:
                                _t = time.time()
                                h = run_layers(h, parts, row_cache, start)
                                logit_row = parts["lm_head"](parts["norm"](h))[0, -1, :].float()
                                if sample_k > 0:
                                    k_actual = min(sample_k, logit_row.size(-1))
                                    topk_vals, topk_ids = torch.topk(logit_row, k_actual)
                                    tok = None  # head samples
                                else:
                                    tok = int(logit_row.argmax())
                                    topk_vals = topk_ids = None
                                tail_ms = (time.time() - _t) * 1e3
                            cb_cache[row_id] = row_cache
                            resp = {"tok": tok, "tail_ms": tail_ms}
                            if sample_k > 0:
                                resp["topk_vals"] = topk_vals.cpu()
                                resp["topk_ids"] = topk_ids.cpu()
                            send_msg(conn, resp)
                            steps += 1
                            continue

                        if op == "cb_step":
                            # DISTRIBUTED CB — one decode step over the active batch.
                            # h [B, 1, H]: the head's batched decode activations (left-padded,
                            # attention_mask and position_ids already applied on the head side).
                            # kv_depths[i]: the real KV depth of row i BEFORE this step
                            # (== number of tokens the tail has seen for that row, i.e.
                            # prompt_len + decode_steps_so_far).
                            # kv_max: max across active rows (used to build the batched cache).
                            # drop: row_ids to evict from cb_cache after this step.
                            # attn_mask [B, kv_max+1]: forwarded from the head so our
                            # run_layers sees the correct mask for the left-padded batch.
                            # pos_ids [B, 1]: per-row absolute position, forwarded from head.
                            row_ids = [int(r) for r in msg["rows"]]
                            drop = [int(r) for r in msg.get("drop", [])]
                            h = msg["h"].to(dev, dtype=stage_dtype)
                            kv_max = int(msg["kv_max"])

                            # Capture actual tail KV depths BEFORE the forward so
                            # _cb_slice_back can compute the real_start slice offset.
                            tail_kv_depths_pre = [
                                cb_cache[rid].get_seq_length() for rid in row_ids
                            ]

                            with _COMPUTE:
                                _t = time.time()
                                # Build batched cache from per-row tail caches.
                                batched = _cb_build_batched_cache(
                                    row_ids, cb_cache, kv_max,
                                    n_tail_layers, dev, stage_dtype)
                                # Run this node's layer block. run_layers uses its own
                                # position_ids/mask from start+q_len; for the decode step
                                # we pass start=kv_depths[0] but the rows differ in depth.
                                # We replicate the head's approach: build a per-layer
                                # forward manually so we can supply the batched attn_mask
                                # and per-row position_ids that the head already computed.
                                # attn_mask from head is [B, kv_max+1] long (1=attend, 0=pad).
                                # Convert to 4-D additive float mask [B, 1, 1, kv_max+1]
                                # that transformers SDPA layers expect.
                                attn_mask_wire = msg.get("attn_mask")
                                pos_ids = msg.get("pos_ids")
                                if pos_ids is not None:
                                    pos_ids = pos_ids.to(dev)
                                min_val = torch.finfo(h.dtype).min
                                if attn_mask_wire is not None:
                                    am = attn_mask_wire.to(dev).bool()  # [B, kv_max+1]
                                    attn_mask_4d = torch.where(
                                        am.unsqueeze(1).unsqueeze(2),
                                        torch.zeros((), dtype=h.dtype, device=dev),
                                        torch.full((), min_val, dtype=h.dtype, device=dev),
                                    )  # [B, 1, 1, kv_max+1]
                                else:
                                    attn_mask_4d = None
                                # Run layers with the ragged mask+positions.
                                # We call each layer directly (not run_layers) because
                                # run_layers computes a single-start causal mask internally,
                                # which is wrong for ragged batches. The batched cache +
                                # forwarded mask + position_ids from the head are correct.
                                pe = parts["rotary"](h, pos_ids)
                                for layer in parts["layers"]:
                                    h = layer(h, attention_mask=attn_mask_4d,
                                              position_ids=pos_ids,
                                              past_key_values=batched,
                                              use_cache=True,
                                              position_embeddings=pe)
                                h = parts["norm"](h)
                                logits_all = parts["lm_head"](h[:, -1, :])  # [B, vocab]
                                # sample_k: list of per-row K (0 = greedy argmax for that row).
                                # When ALL rows are greedy (or sample_k not present), we fall
                                # back to the identical all-argmax path.
                                sample_k = msg.get("sample_k")
                                if sample_k and any(k > 0 for k in sample_k):
                                    # Mixed greedy/sampling batch: argmax greedy rows,
                                    # return top-K for sampled rows (head samples on its side).
                                    toks = []
                                    topk_rows = []
                                    for i in range(len(row_ids)):
                                        ki = int(sample_k[i]) if sample_k and i < len(sample_k) else 0
                                        if ki > 0:
                                            k_actual = min(ki, logits_all.shape[-1])
                                            vals, ids = torch.topk(logits_all[i].float(), k_actual)
                                            toks.append(None)   # placeholder; head samples
                                            topk_rows.append({
                                                "vals": vals.cpu(),
                                                "ids": ids.cpu(),
                                            })
                                        else:
                                            toks.append(int(logits_all[i].argmax().item()))
                                            topk_rows.append(None)
                                else:
                                    toks = logits_all.argmax(-1).tolist()
                                    topk_rows = None
                                tail_ms = (time.time() - _t) * 1e3
                            # Slice batched output cache back to per-row caches.
                            _cb_slice_back(batched, row_ids, cb_cache,
                                           tail_kv_depths_pre, kv_max, n_tail_layers)
                            # Evict finished rows.
                            for rid in drop:
                                cb_cache.pop(rid, None)
                            resp = {"toks": toks, "tail_ms": tail_ms}
                            if topk_rows is not None:
                                resp["topk_rows"] = topk_rows
                            send_msg(conn, resp)
                            steps += 1
                            continue

                        if op == "verify":
                            # SPECULATIVE verify: K+1 candidate positions in, K+1 argmax
                            # tokens out (one per position). `crop` rolls this node's cache
                            # back to the prior round's COMMITTED length before running, so
                            # rejected speculation never persists. A greedy head never sends
                            # this shape, so its wire is untouched.
                            crop = msg.get("crop")
                            h, start = msg["h"], msg["start"]
                            with _COMPUTE:
                                _t = time.time()
                                if crop is not None:
                                    cache.crop(crop)
                                h = run_layers(h.to(dev, dtype=stage_dtype), parts, cache, start)
                                h = parts["norm"](h)
                                toks = parts["lm_head"](h).argmax(-1)[0].tolist()  # [q_len] argmax/pos
                                tail_ms = (time.time() - _t) * 1e3
                            send_msg(conn, {"toks": toks, "tail_ms": tail_ms})
                            steps += 1
                            continue

                        if op == "sample_step":
                            # SAMPLING single-stream step: the head has set temperature>0 so
                            # it needs the raw logits to sample on its side (where the
                            # per-request generator lives).  Instead of returning a single
                            # argmax token we return the top-K (values + indices) of the
                            # final logits so the head can run sample_from_topk.
                            # K is chosen by the head to fit top_k and the nucleus; typical
                            # value is 64.  The greedy tuple wire is NOT used for this path
                            # so legacy tails that don't know this op would raise -- which is
                            # the correct behavior (they cannot do head-side sampling anyway).
                            h_in = msg["h"]
                            start = int(msg["start"])
                            topk_k = int(msg.get("topk_k", 64))
                            with _COMPUTE:
                                _t = time.time()
                                h = run_layers(h_in.to(dev, dtype=stage_dtype), parts, cache, start)
                                h = parts["norm"](h)
                                logit_row = parts["lm_head"](h[0, -1, :]).float()
                                k = min(topk_k, logit_row.size(-1))
                                topk_vals, topk_ids = torch.topk(logit_row, k)
                                tail_ms = (time.time() - _t) * 1e3
                            send_msg(conn, {
                                "topk_vals": topk_vals.cpu(),
                                "topk_ids": topk_ids.cpu(),
                                "tail_ms": tail_ms,
                            })
                            steps += 1
                            continue

                        h, start = msg
                        with _COMPUTE:
                            _t = time.time()
                            h = run_layers(h.to(dev, dtype=stage_dtype), parts, cache, start)
                            h = parts["norm"](h)
                            # [B] next tokens; one batched forward serves the whole batch.
                            # batch=1 sends a bare int (back-compat with the single-stream
                            # / concurrency head); batch>1 sends a list.
                            toks = parts["lm_head"](h[:, -1, :]).argmax(-1).tolist()
                            tail_ms = (time.time() - _t) * 1e3  # .tolist() already synced this node
                        send_msg(conn, {"tok": (toks[0] if len(toks) == 1 else toks), "tail_ms": tail_ms})
                    except EDGE_ERRORS as e:
                        why = "stalled" if isinstance(e, socket.timeout) else "closed"
                        logger.info("pipeline tail: stream %s %s after %d steps (%s)", addr, why, steps, type(e).__name__)
                        try:
                            conn.close()
                        except OSError:
                            pass
                        break
                    steps += 1
        finally:
            with active_lock:
                active["n"] -= 1
                left = active["n"]
            logger.info("pipeline tail: stream %s ended after %d steps (active: %d)", addr, steps, left)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(timeout)
            threading.Thread(target=serve, args=(conn, addr), daemon=True).start()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        logger.info("pipeline tail: stage on :%d stopped", port)


def serve_mid(parts, dev, *, port, peer2, port2, timeout=30.0, host="0.0.0.0", stop_event=None, ready_event=None):
    """Run a mid stage of a 3+ node chain. A mid speaks the tail's wire protocol upstream
    and the head's downstream: recv activations from the head, run THIS node's block,
    forward downstream to the tail, relay the tail's token back up. Each upstream stream
    gets its own downstream socket so the tail keeps a separate KV cache per stream.
    Blocks forever (accept loop)."""
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    srv.settimeout(1.0)  # wake the accept loop periodically to honor stop_event
    if ready_event is not None:
        ready_event.set()
    logger.info("pipeline mid: listening on :%d; relaying to tail %s:%d (edge timeout %.0fs)", port, peer2, port2, timeout)
    stage_dtype = next(parts["layers"].parameters()).dtype  # cast inbound activations to ours

    def serve(conn, addr):
        try:
            tail_sock = _connect_with_retry(peer2, port2, timeout)
        except TransportError:
            logger.warning("pipeline mid: could not reach tail %s:%d; dropping head %s", peer2, port2, addr)
            try:
                conn.close()
            except OSError:
                pass
            return
        logger.info("pipeline mid: head %s connected; downstream tail open", addr)
        cache = DynamicCache()
        # Per-row KV for distributed continuous batching (op="cb_prefill"/"cb_step").
        # The mid maintains its OWN per-row KV for ITS layer block, exactly like the
        # tail does for its layers. Keyed by row_id, populated on cb_prefill, evicted
        # via the drop list in cb_step.
        cb_cache: dict = {}  # row_id -> DynamicCache (this node's layers only)
        n_mid_layers = len(parts["layers"])
        steps = 0
        try:
            with torch.no_grad():
                while True:
                    try:
                        msg = recv_msg(conn)
                        op = msg.get("op") if isinstance(msg, dict) else None

                        if op == "cb_prefill":
                            # DISTRIBUTED CB relay — mid path.
                            # 1. Run the mid's layer block on the head's prefill activations,
                            #    building this node's per-row KV for row_id.
                            # 2. Forward the mid's output activations downstream as cb_prefill.
                            # 3. Relay the tail's first-token response back upstream.
                            row_id = msg["row_id"]
                            h = msg["h"].to(dev, dtype=stage_dtype)
                            start = int(msg["start"])
                            row_cache = DynamicCache()
                            with _COMPUTE:
                                h = run_layers(h, parts, row_cache, start)
                            cb_cache[row_id] = row_cache
                            send_msg(tail_sock, {
                                "op": "cb_prefill",
                                "row_id": row_id,
                                "h": h.cpu(),
                                "start": start,
                            })
                            send_msg(conn, recv_msg(tail_sock))  # relay first token upstream
                            steps += 1
                            continue

                        if op == "cb_step":
                            # DISTRIBUTED CB relay — mid path.
                            # 1. Apply the drop list to the mid's own cb_cache.
                            # 2. Build mid's batched cache; run each layer with the forwarded
                            #    attn_mask + pos_ids (same pattern as serve_tail's cb_step).
                            # 3. Forward the mid's output activations downstream as cb_step
                            #    with the SAME metadata (rows, kv_depths, kv_max, drop,
                            #    attn_mask, pos_ids) so the tail's handler is unchanged.
                            # 4. Relay the tail's [B] toks response back upstream.
                            # 5. Slice mid's output cache back to per-row caches.
                            row_ids = [int(r) for r in msg["rows"]]
                            drop = [int(r) for r in msg.get("drop", [])]
                            h = msg["h"].to(dev, dtype=stage_dtype)
                            kv_max = int(msg["kv_max"])
                            attn_mask_wire = msg.get("attn_mask")
                            pos_ids = msg.get("pos_ids")

                            # Evict dropped rows from the mid's own cache first (same step
                            # they are dropped from head and tail — keep all three in sync).
                            for rid in drop:
                                cb_cache.pop(rid, None)

                            # Capture mid's actual KV depths BEFORE the forward so
                            # _cb_slice_back can compute the correct real_start offset.
                            mid_kv_depths_pre = [
                                cb_cache[rid].get_seq_length() for rid in row_ids
                            ]

                            with _COMPUTE:
                                batched = _cb_build_batched_cache(
                                    row_ids, cb_cache, kv_max,
                                    n_mid_layers, dev, stage_dtype)

                                if pos_ids is not None:
                                    pos_ids_dev = pos_ids.to(dev)
                                else:
                                    pos_ids_dev = None

                                min_val = torch.finfo(h.dtype).min
                                if attn_mask_wire is not None:
                                    am = attn_mask_wire.to(dev).bool()  # [B, kv_max+1]
                                    attn_mask_4d = torch.where(
                                        am.unsqueeze(1).unsqueeze(2),
                                        torch.zeros((), dtype=h.dtype, device=dev),
                                        torch.full((), min_val, dtype=h.dtype, device=dev),
                                    )  # [B, 1, 1, kv_max+1]
                                else:
                                    attn_mask_4d = None

                                pe = parts["rotary"](h, pos_ids_dev)
                                for layer in parts["layers"]:
                                    h = layer(h, attention_mask=attn_mask_4d,
                                              position_ids=pos_ids_dev,
                                              past_key_values=batched,
                                              use_cache=True,
                                              position_embeddings=pe)

                            # Slice mid's output cache back to per-row caches.
                            _cb_slice_back(batched, row_ids, cb_cache,
                                           mid_kv_depths_pre, kv_max, n_mid_layers)

                            # Forward downstream with identical metadata so the tail's
                            # existing cb_step handler runs unchanged.
                            send_msg(tail_sock, {
                                "op": "cb_step",
                                "rows": row_ids,
                                "drop": drop,
                                "h": h.cpu(),
                                "kv_depths": msg.get("kv_depths"),
                                "kv_max": kv_max,
                                "attn_mask": attn_mask_wire,
                                "pos_ids": pos_ids,
                            })
                            send_msg(conn, recv_msg(tail_sock))  # relay [B] toks upstream
                            steps += 1
                            continue

                        if op == "verify":
                            # SPECULATIVE relay: crop THIS node's cache to the prior round's
                            # committed length, run our block on the K+1 candidates, forward
                            # downstream carrying the SAME crop (so the tail rolls back too),
                            # relay the tail's K+1 tokens back up. crop=None on the prefill /
                            # first round, which both sides treat as "no rollback".
                            crop = msg.get("crop")
                            h, start = msg["h"], msg["start"]
                            with _COMPUTE:
                                if crop is not None:
                                    cache.crop(crop)
                                h = run_layers(h.to(dev, dtype=stage_dtype), parts, cache, start)
                            send_msg(tail_sock, {"op": "verify", "h": h.cpu(), "start": start, "crop": crop})
                            send_msg(conn, recv_msg(tail_sock))  # relay K+1 tokens upstream
                            steps += 1
                            continue

                        if op == "sample_step":
                            # SAMPLING single-stream relay: mid runs its block on the
                            # activations, then forwards op="sample_step" to the tail with
                            # the mid's output activations.  The tail returns top-K; relay
                            # that directly back to the head (head samples on its side).
                            h_in = msg["h"]
                            start = int(msg["start"])
                            topk_k = int(msg.get("topk_k", 64))
                            with _COMPUTE:
                                h = run_layers(h_in.to(dev, dtype=stage_dtype), parts, cache, start)
                            send_msg(tail_sock, {
                                "op": "sample_step",
                                "h": h.cpu(),
                                "start": start,
                                "topk_k": topk_k,
                            })
                            send_msg(conn, recv_msg(tail_sock))  # relay top-K upstream
                            steps += 1
                            continue

                        h, start = msg
                        with _COMPUTE:
                            h = run_layers(h.to(dev, dtype=stage_dtype), parts, cache, start)
                        send_msg(tail_sock, (h.cpu(), start))  # forward downstream to the tail
                        resp = recv_msg(tail_sock)  # token comes back from the tail
                        send_msg(conn, resp)  # relay it upstream to the head
                    except EDGE_ERRORS as e:
                        why = "stalled" if isinstance(e, socket.timeout) else "closed"
                        logger.info("pipeline mid: stream %s %s after %d steps (%s)", addr, why, steps, type(e).__name__)
                        break
                    steps += 1
        finally:
            for s in (conn, tail_sock):
                try:
                    s.close()
                except OSError:
                    pass
            logger.info("pipeline mid: stream %s ended after %d steps", addr, steps)

    try:
        while True:
            if stop_event is not None and stop_event.is_set():
                break
            try:
                conn, addr = srv.accept()
            except socket.timeout:
                continue
            conn.settimeout(timeout)
            threading.Thread(target=serve, args=(conn, addr), daemon=True).start()
    finally:
        try:
            srv.close()
        except OSError:
            pass
        logger.info("pipeline mid: stage on :%d stopped", port)


class PipelineStageWorker:
    """Runs ONE pipeline stage (tail or mid) on this node, started PROGRAMMATICALLY (by the
    /peer pipeline-stage endpoint) rather than the CLI. Loads the assigned layer block and
    serves an upstream head over the mesh. A node hosts at most one active stage at a time
    (a pipeline run binds the device); start() raises if one is already running."""

    def __init__(self):
        self._thread = None
        self._stop = None
        self._info = None
        self._lock = threading.Lock()

    def start(self, model_id, *, role, split, port, split2=0, device="", dtype=None,
              peer2="", port2=0, timeout=30.0):
        if role not in ("tail", "mid"):
            raise ValueError("role must be 'tail' or 'mid'")
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                raise RuntimeError("a pipeline stage is already running on this node")
            _ensure_wire_key()  # programmatic path skips main(); load SHARD_PSK before serving
            dev = resolve_device(device)
            dt = dtype if dtype is not None else default_dtype(dev)
            parts = load_parts(model_id, split, role, device=dev, dtype=dt, split2=split2)  # blocking load
            stop = threading.Event()
            ready = threading.Event()
            if role == "tail":
                def target():
                    serve_tail(parts, dev, port=port, timeout=timeout, stop_event=stop, ready_event=ready)
            else:
                def target():
                    serve_mid(parts, dev, port=port, peer2=peer2, port2=port2,
                              timeout=timeout, stop_event=stop, ready_event=ready)
            t = threading.Thread(target=target, daemon=True, name=f"pipeline-stage-{role}")
            t.start()
            if not ready.wait(timeout=10.0):
                stop.set()
                raise RuntimeError("pipeline stage failed to start listening within 10s")
            self._thread, self._stop = t, stop
            self._info = {
                "role": role, "model_id": model_id, "port": port,
                "split": split, "split2": split2, "device": str(dev), "dtype": str(dt),
            }
            return dict(self._info)

    def status(self):
        running = self._thread is not None and self._thread.is_alive()
        if not running and self._info is None:
            return {"running": False}
        return {"running": running, **(self._info or {})}

    def stop(self):
        with self._lock:
            if self._stop is not None:
                self._stop.set()
            if self._thread is not None:
                self._thread.join(timeout=4.0)
            self._thread = self._stop = self._info = None
            return {"running": False}


_stage_worker = None


def get_stage_worker() -> "PipelineStageWorker":
    """Process-wide singleton stage worker (this node hosts at most one stage)."""
    global _stage_worker
    if _stage_worker is None:
        _stage_worker = PipelineStageWorker()
    return _stage_worker


# ---------------------------------------------------------------------------
# Head-side orchestration: build a chain across this node + household peers
# ---------------------------------------------------------------------------


def plan_pipeline_chain(n_layers: int, n_nodes: int) -> tuple[int, int]:
    """Even contiguous split of n_layers across n_nodes. Returns (split, split2):
    2-node -> (split, 0)  [head=[0:split], tail=[split:n]];
    3-node -> (split, split2)  [head=[0:split], mid=[split:split2], tail=[split2:n]].
    Capped at 3 nodes (the engine is head/mid/tail; 4+ needs multi-mid support). Pure
    function -> unit-testable without a mesh."""
    n_nodes = max(2, min(n_nodes, 3))
    per = max(1, n_layers // n_nodes)
    if n_nodes == 2:
        return per, 0
    return per, 2 * per


# ---------------------------------------------------------------------------
# Topology-aware placement (pure, deterministic, unit-testable)
# ---------------------------------------------------------------------------
#
# The even-split planner above is order-blind: it ignores how much VRAM each node
# has and how far away it is. On a real household the nodes are heterogeneous (a
# 24GB 3090 next to a 8GB laptop next to a Mac) and the links are heterogeneous
# (a wired LAN peer vs a DERP-relayed one). Every pipeline hop is on the per-token
# critical path, so two things matter: put the model on the FEWEST, LOWEST-LATENCY
# nodes that can hold it, and give the big/fast node MORE layers. This planner does
# both, while staying a pure function so it's testable without a mesh.

# Conservative runtime overhead factor on top of raw weight bytes. A transformer
# at inference needs more VRAM than just its weights: the KV cache grows with
# context, activations are live during a forward, and the framework (CUDA context,
# cuDNN workspaces, allocator fragmentation) reserves a slab. 1.3x is deliberately
# pessimistic -- under-claiming a fit only costs us an unnecessary hop, while
# OVER-claiming a fit wedges a node mid-load. When unsure we prefer to split.
_VRAM_OVERHEAD = 1.3

# Fallback per-node-tier VRAM weight estimate (GB) when a node advertises no
# usable vram number (e.g. a CPU-only or detection-failed peer). Used both as the
# capacity ceiling AND as the proportional weight, so a "high" node still pulls
# more layers than a "low" one even without a hard VRAM reading. Conservative.
_TIER_VRAM_GB = {"large": 16.0, "high": 16.0, "mid": 8.0, "medium": 8.0, "low": 4.0, "small": 4.0}
_DEFAULT_TIER_VRAM_GB = 6.0

# Fallback model footprint (GB, fp16 weights pre-overhead) keyed off the head's
# recommended tier when we can't read the model's own param count. Picked so the
# fits-on-one-box short-circuit stays conservative: assume a model roughly as big
# as the biggest thing that tier is meant to run, so we don't claim a whole-model
# fit we can't back up. fp16 bytes; the overhead factor is applied on top.
_TIER_MODEL_GB = {"large": 20.0, "high": 20.0, "mid": 14.0, "medium": 14.0, "low": 8.0, "small": 8.0}
_DEFAULT_MODEL_GB = 14.0

# ---- Link cost (used for BOTH peer selection and chain ordering) ----------
#
# Every pipeline hop is on the per-token critical path, and a token relays through that
# hop on EVERY step. So a link's cost isn't just its mean latency: jitter compounds (one
# slow sample stalls that whole token) and a WiFi link -- especially a USB WiFi adapter
# -- is both higher-latency AND jittery. We fold all three into one scalar so selection
# and ordering use the same notion of "good link":
#
#     cost = median_rtt_ms + Kj * jitter_ms + link_penalty
#
# Kj weights jitter heavily because a jittery link hurts streaming smoothness out of
# proportion to its median (a chain is only as steady as its worst hop). The penalties
# are ADDITIVE constants in the same ms unit as RTT, sized so that on a typical LAN
# (single-digit ms RTT) the wired/wifi distinction dominates ordering, while two wired
# peers still sort by their actual measured RTT.
_JITTER_WEIGHT = 2.0          # Kj: each ms of p90-p50 jitter counts as 2ms of cost
_WIFI_PENALTY_MS = 50.0       # large: WiFi compounds per-token; only used when needed to fit
_UNKNOWN_LINK_PENALTY_MS = 10.0  # mild: conservative nudge, never a hard exclusion
_WIRED_PENALTY_MS = 0.0       # wired is the reference (no penalty)

_LINK_PENALTY_MS = {
    "wired": _WIRED_PENALTY_MS,
    "wifi": _WIFI_PENALTY_MS,
    "unknown": _UNKNOWN_LINK_PENALTY_MS,
}


def link_cost(node: dict) -> float:
    """Scalar link cost for ordering + selecting pipeline nodes. PURE.

        cost = median_rtt_ms + _JITTER_WEIGHT * jitter_ms + link penalty

    Reads (with safe fallbacks) the fields the orchestrator attaches per node:
      median_rtt_ms -- robust central RTT (falls back to legacy rtt_ms, else 0).
      jitter_ms     -- p90 - p50 of the RTT burst (falls back to 0 == unknown jitter).
      link_type     -- 'wired'|'wifi'|'unknown'; anything unrecognized => unknown penalty.

    Wired => 0 penalty (reference). WiFi => a large additive penalty because the link is
    re-traversed every token and jitter compounds; the head only falls to a WiFi peer when
    it's needed to fit the model. Unknown => a mild penalty (conservative: we don't hard-
    exclude a peer just because detection failed). The head itself has rtt 0 + jitter 0,
    so its cost reduces to its own link penalty -- which never affects ordering since the
    head is pinned at position 0, but is still recorded for visibility."""
    median = node.get("median_rtt_ms")
    if median is None:
        median = node.get("rtt_ms", 0.0)  # back-compat with single-sample callers/tests
    median = float(median)
    jitter = float(node.get("jitter_ms", 0.0) or 0.0)
    penalty = _LINK_PENALTY_MS.get((node.get("link_type") or "unknown").lower(),
                                   _UNKNOWN_LINK_PENALTY_MS)
    return median + _JITTER_WEIGHT * jitter + penalty


def estimate_model_vram_bytes(n_params: int | None, *, dtype_bytes: int = 2,
                              fallback_tier: str | None = None) -> float:
    """Conservative inference-time VRAM footprint (BYTES) for a model.

    Heuristic: weights ~= n_params * dtype_bytes (fp16 => 2 bytes/param), then a
    runtime overhead factor (_VRAM_OVERHEAD) for KV cache + activations + framework
    reservation. When n_params is unknown, fall back to a per-tier model-size guess
    keyed off the HEAD's recommended tier (the requester's own machine). Pure.

    Deliberately pessimistic -- see _VRAM_OVERHEAD. We would rather split a model
    that would just barely have fit than claim a fit and wedge a node on load."""
    if n_params and n_params > 0:
        weight_bytes = float(n_params) * dtype_bytes
    else:
        gb = _TIER_MODEL_GB.get((fallback_tier or "").lower(), _DEFAULT_MODEL_GB)
        weight_bytes = gb * 1e9  # the tier guesses are already fp16-weight GB
    return weight_bytes * _VRAM_OVERHEAD


def _node_capacity_gb(node: dict) -> float:
    """USABLE VRAM (GB) for placing layers on a node. Prefer the advertised GPU
    VRAM; fall back to the tier estimate when a node advertises none (CPU-only or
    detection-failed peer). Never trusts a 0/None vram as 'tons of room'."""
    vram = node.get("vram_gb")
    if vram and vram > 0:
        return float(vram)
    return _TIER_VRAM_GB.get((node.get("tier") or "").lower(), _DEFAULT_TIER_VRAM_GB)


def plan_pipeline_placement(
    n_layers: int,
    model_bytes: float,
    nodes: list[dict],
    *,
    max_nodes: int = 3,
) -> dict:
    """Topology-aware placement for an AUTO pipeline run. PURE + DETERMINISTIC.

    Inputs:
      n_layers   -- the model's transformer layer count.
      model_bytes-- conservative inference VRAM footprint (see estimate_model_vram_bytes).
      nodes      -- candidate nodes, HEAD FIRST then peers. Each:
                    {"id": str, "vram_gb": float|None, "tier": str|None,
                     "median_rtt_ms": float, "jitter_ms": float, "link_type": str}.
                    The head's median_rtt_ms/jitter_ms are 0 (no link to itself), but its
                    own link_type IS recorded. Legacy callers may pass "rtt_ms" instead of
                    "median_rtt_ms"; link_cost() falls back to it, so old shapes still work.
      max_nodes  -- hard cap on chain length (engine is head/mid/tail => <=3).

    Returns one of:
      {"mode": "whole", "nodes": [head]}        -- the head alone fits the model;
                                                   skip the pipeline entirely (fastest).
      {"mode": "pipeline",                       -- a multi-node chain:
       "order": [node, ...],                     -- chosen nodes in chain order (head first),
                                                    each annotated with link_cost/link_type/
                                                    median_rtt_ms/jitter_ms for visibility
       "layers": [count, ...],                   -- contiguous per-node layer counts (sum=n)
       "split": int, "split2": int}             -- the (split, split2) the rest expects.

    Decision rules:
      1. fits-on-one-box: if the head's own capacity >= model_bytes, return whole-model.
         No hop is faster than no hop.
      2. otherwise build a chain: head first, then peers ordered by LINK COST ascending
         (see link_cost): cost = median_rtt + Kj*jitter + wifi/unknown penalty. Lowest-
         cost links sit adjacent to the head -- every hop relays every token, so this
         prefers wired + low-jitter peers and pushes WiFi/jittery ones to the far end.
      3. add only as many peers as needed for the cumulative capacity to cover the
         model (fewest hops), capped at max_nodes. Because peers are pre-sorted by link
         cost, a WiFi peer is admitted only when a cheaper-link peer can't make the fit.
      4. split layers PROPORTIONAL to each chosen node's capacity (big/fast node holds
         more), then CAP each node at what its VRAM can hold and spill the remainder to
         nodes with slack. Blocks stay contiguous: head=[0:a], mid=[a:b], tail=[b:n]."""
    if not nodes:
        raise ValueError("plan_pipeline_placement: need at least the head node")
    max_nodes = max(2, min(max_nodes, 3))
    head = nodes[0]
    bytes_per_gb = 1e9

    # Rule 1 -- fits-on-one-box short-circuit (the fastest option: no pipeline). The
    # head still carries its own link_type for visibility, but no hop means no link cost.
    head_cap_bytes = _node_capacity_gb(head) * bytes_per_gb
    if head_cap_bytes >= model_bytes:
        return {"mode": "whole", "nodes": [_annotate_link(head)]}

    # Rule 2 -- order peers by LINK COST ascending (prefer wired/low-jitter, deprioritize
    # WiFi/jittery), id as a deterministic tiebreak; head stays pinned at position 0.
    peers = sorted(nodes[1:], key=lambda p: (link_cost(p), str(p.get("id", ""))))

    # Rule 3 -- greedily admit the lowest-cost-link peers until cumulative capacity covers
    # the model OR we hit the node cap. Fewest hops that fit, best links first => a WiFi
    # peer is only pulled in when a wired/closer peer can't complete the fit.
    chosen = [head]
    cum_bytes = head_cap_bytes
    for p in peers:
        if cum_bytes >= model_bytes or len(chosen) >= max_nodes:
            break
        chosen.append(p)
        cum_bytes += _node_capacity_gb(p) * bytes_per_gb

    caps_gb = [_node_capacity_gb(nd) for nd in chosen]
    layers = _proportional_contiguous_split(n_layers, caps_gb)

    split = layers[0]
    split2 = (layers[0] + layers[1]) if len(layers) >= 3 else 0
    return {"mode": "pipeline", "order": [_annotate_link(nd) for nd in chosen],
            "layers": layers, "split": split, "split2": split2}


def _annotate_link(node: dict) -> dict:
    """Return a copy of `node` with its resolved link fields surfaced so the chosen chain
    is self-describing: link_type, median_rtt_ms, jitter_ms, and the computed link_cost.
    Pure; never mutates the caller's dict. Keeps the returned chain backward-compatible
    (only ADDS fields; existing keys like id/vram_gb/tier/rtt_ms pass through unchanged)."""
    out = dict(node)
    median = node.get("median_rtt_ms")
    if median is None:
        median = node.get("rtt_ms", 0.0)
    out["median_rtt_ms"] = float(median)
    out["jitter_ms"] = float(node.get("jitter_ms", 0.0) or 0.0)
    out["link_type"] = (node.get("link_type") or "unknown")
    out["link_cost"] = link_cost(node)
    return out


def _proportional_contiguous_split(n_layers: int, caps_gb: list[float]) -> list[int]:
    """Split n_layers across len(caps_gb) nodes PROPORTIONAL to capacity, with each
    node getting >=1 layer and the counts summing EXACTLY to n_layers. Pure.

    Largest-remainder (Hamilton) apportionment keeps the proportions honest and the
    rounding deterministic; we then nudge the floor up so no node is starved of a
    layer (a stage with 0 layers is pointless and breaks the contiguous mapping)."""
    k = len(caps_gb)
    if k <= 1:
        return [n_layers]
    n_layers = max(n_layers, k)  # need at least one layer per node
    total_cap = sum(caps_gb) or float(k)
    exact = [n_layers * c / total_cap for c in caps_gb]
    floors = [max(1, int(f)) for f in exact]  # floor, but never below 1
    # Distribute the leftover (or reclaim an overshoot from the >=1 flooring) by
    # largest fractional remainder, so the split stays as proportional as integers allow.
    diff = n_layers - sum(floors)
    order = sorted(range(k), key=lambda i: exact[i] - int(exact[i]), reverse=True)
    i = 0
    while diff > 0:  # hand out remaining layers to the biggest remainders first
        floors[order[i % k]] += 1
        diff -= 1
        i += 1
    # If the >=1 minimum overshot (tiny model, many nodes), claw back from the
    # nodes with the most layers so we never exceed n_layers.
    while diff < 0:
        j = max(range(k), key=lambda i: floors[i])
        if floors[j] > 1:
            floors[j] -= 1
            diff += 1
        else:
            break  # everyone is at the floor of 1; can't reduce further
    return floors


def estimate_params_from_config(cfg) -> int | None:
    """Best-effort parameter count from a HF config WITHOUT downloading weights.

    Standard decoder-only transformer accounting: embeddings + per-layer (attention
    projections + MLP) + final norm/head. Handles GQA (num_key_value_heads) and tied
    embeddings. Returns None if the config lacks the fields we need, so the caller
    falls back to the per-tier model-size guess. Pure (no I/O); the config read is
    done by the caller. Approximate by design -- it feeds a conservative VRAM estimate,
    not an exact memory plan."""
    try:
        h = int(cfg.hidden_size)
        n_layers = int(cfg.num_hidden_layers)
        vocab = int(cfg.vocab_size)
        inter = int(getattr(cfg, "intermediate_size", 4 * h))
        n_heads = int(getattr(cfg, "num_attention_heads", max(1, h // 128)))
        n_kv = int(getattr(cfg, "num_key_value_heads", n_heads))
    except (AttributeError, TypeError, ValueError):
        return None
    head_dim = h // n_heads if n_heads else h
    # Attention: q [h,h] + k,v [h, n_kv*head_dim] each + o [h,h]
    attn = h * h + 2 * (h * n_kv * head_dim) + h * h
    # MLP: gate + up + down (gated SwiGLU-style; 3 matrices of ~h*inter). A plain
    # 2-matrix MLP over-counts slightly, which is the safe direction here.
    mlp = 3 * (h * inter)
    per_layer = attn + mlp
    embed = vocab * h
    tied = bool(getattr(cfg, "tie_word_embeddings", False))
    head = 0 if tied else vocab * h
    return embed + n_layers * per_layer + head


def _start_peer_stage(mesh_ip: str, body: dict, timeout: float) -> dict:
    """POST /peer/pipeline/stage on a household peer; raise on a non-200."""
    import httpx
    r = httpx.post(f"http://{mesh_ip}:8321/peer/pipeline/stage", json=body, timeout=timeout)
    if r.status_code != 200:
        raise RuntimeError(f"peer {mesh_ip} stage start failed: HTTP {r.status_code} {r.text[:200]}")
    return r.json()


def _stop_peer_stage(mesh_ip: str, timeout: float = 10.0) -> None:
    """Best-effort DELETE /peer/pipeline/stage on a peer (rollback / teardown). Never
    raises -- it's cleanup; a peer that's already stopped or unreachable is fine."""
    import httpx
    try:
        httpx.delete(f"http://{mesh_ip}:8321/peer/pipeline/stage", timeout=timeout)
    except Exception as e:
        logger.warning("pipeline: failed to stop stage on %s during teardown: %s", mesh_ip, e)


def _probe_peer(mesh_ip: str, timeout: float = 5.0, samples: int = 7) -> tuple[dict | None, float, float]:
    """GET a peer's /peer/capabilities N times and characterize the LINK, not just one
    sample. Returns (caps, median_rtt_ms, jitter_ms); (None, inf, inf) on any failure.

    Why N samples: a single RTT can't see JITTER, and jitter is what wrecks pipeline-
    parallel -- every token relays through every hop, so one peer with occasional 60ms+
    stalls makes the whole stream uneven even if its MEDIAN looks fine. We take a small
    burst (default 7), use the MEDIAN as the central tendency (robust to a single outlier,
    unlike the mean) and p90 - p50 as the JITTER metric (the size of the slow tail a token
    can hit -- directly what hurts streaming, and bounded below by 0). Reusing the
    capabilities call keeps this endpoint-free; first request also warms the connection.

    Kept fast: small N, the caller's short timeout per request. A request that errors is
    skipped (not counted as inf), so a transient blip doesn't poison the median; only a
    peer that fails EVERY sample returns (None, inf, inf)."""
    import httpx

    rtts: list[float] = []
    caps: dict | None = None
    with httpx.Client(timeout=timeout) as client:
        for _ in range(max(1, samples)):
            t0 = time.monotonic()
            try:
                r = client.get(f"http://{mesh_ip}:8321/peer/capabilities")
                dt = (time.monotonic() - t0) * 1e3
            except Exception:
                continue
            rtts.append(dt)
            if caps is None:  # capabilities are static within a run; keep the first good one
                caps = r.json()
    if not rtts or caps is None:
        return None, float("inf"), float("inf")
    median = _percentile(rtts, 50)
    jitter = max(0.0, _percentile(rtts, 90) - median)  # slow-tail size; >=0
    return caps, median, jitter


def _percentile(xs: list[float], pct: float) -> float:
    """Nearest-rank percentile of a non-empty list (pure, no numpy on this path). pct in
    [0,100]; clamps the rank into range so p100 -> max and p0 -> min. Small N here, so
    the simple rank is plenty -- we only need a robust median + a slow-tail estimate."""
    if not xs:
        return 0.0
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    rank = int(round((pct / 100.0) * (len(s) - 1)))
    rank = min(max(rank, 0), len(s) - 1)
    return s[rank]


def _collect_household_peers() -> list[dict]:
    """Roster of reachable household peers. household_peers() is async (gateway_routing),
    and we run inside a thread off the event loop (load_model -> run_in_executor), so drive
    it with asyncio.run(). [] on any failure (degrade to whole-model / no peers)."""
    try:
        from .gateway_routing import household_peers
    except ImportError:
        from potluck_local.services.gateway_routing import household_peers
    import asyncio
    try:
        return asyncio.run(household_peers())
    except Exception:
        logger.debug("household_roster_unavailable_for_pipeline", exc_info=True)
        return []


def orchestrate_household_pipeline(model_id, *, device="", dtype="float16",
                                   max_nodes=3, stage_port=29900, timeout=30.0) -> dict:
    """Build a pipeline chain across THIS node (head) + available household peers using
    TOPOLOGY-AWARE placement (plan_pipeline_placement), start each peer's stage, and
    return the chain config for runtime.load_pipeline:
    {split, split2, peer, port, dtype, n_layers, nodes:[...]}.

    Whole-model signal: when the head's own VRAM fits the model, the planner returns
    mode="whole" and there is no fast path in placing a pipeline at all -- this function
    returns {"mode": "whole", ...} (no split/peer/stage_peers). The caller (load_model)
    sees mode=="whole" and loads the model LOCALLY instead of forcing a split.

    Selection: peers from the live roster whose /peer/capabilities reports
    available_for_pipeline. Layers are placed PROPORTIONAL to each node's VRAM and the
    chain is ORDERED + SELECTED by LINK COST (median RTT + jitter + a WiFi/unknown-link
    penalty), so wired, low-jitter peers sit adjacent to the head and a WiFi peer is only
    pulled in when needed to fit. Uses only as many peers as needed to fit (fewest hops;
    capped at max_nodes). dtype is forced uniform
    across the chain (default float16 -- the only dtype every backend including DirectML
    supports; bf16 is better for large models on non-DirectML chains). Stages start in
    REVERSE order (tail first) so a mid can dial its downstream tail. Raises if a split
    is required but no peer is eligible. Scope: my-machines / trusted-circle (never the
    open pool)."""
    from transformers import AutoConfig

    cfg = AutoConfig.from_pretrained(model_id)
    n_layers = cfg.num_hidden_layers

    # Head (self) resource signal from the local hardware detector.
    try:
        from .hardware_detect import detect_hardware
    except ImportError:
        from potluck_local.services.hardware_detect import detect_hardware
    hw = detect_hardware()
    head_tier = hw.get("recommended_tier")
    # The head has no link to itself (rtt/jitter 0) but DOES record its own link type --
    # carried through to the chain summary so an operator can see the head's link too.
    try:
        from .link_detect import detect_link_type
    except ImportError:
        from potluck_local.services.link_detect import detect_link_type
    head_node = {
        "id": "this", "median_rtt_ms": 0.0, "jitter_ms": 0.0,
        "link_type": detect_link_type(),
        "vram_gb": hw.get("gpu_vram_gb"), "tier": head_tier,
    }

    # Conservative model footprint: prefer the config-derived param count; fall back to
    # the head's tier when the config is too sparse to estimate from (see the heuristic
    # docstrings). fp16 (2 bytes/param) -- matches the default dtype the chain loads in.
    n_params = estimate_params_from_config(cfg)
    model_bytes = estimate_model_vram_bytes(n_params, dtype_bytes=2, fallback_tier=head_tier)

    # Eligible peers + per-peer LINK characterization (N-sample capabilities probe; no
    # new endpoint). We capture the median RTT, the jitter (p90-p50) and the peer's
    # self-advertised link_type so the planner can prefer wired/low-jitter peers.
    candidates = [head_node]
    peer_meta: dict[str, dict] = {}  # id -> {mesh_ip, node_name} to resolve placement back
    for p in _collect_household_peers():
        caps, median_rtt, jitter = _probe_peer(p["mesh_ip"])
        if not caps or not caps.get("available_for_pipeline"):
            continue
        hwp = caps.get("hardware") or {}
        node = {
            "id": p["mesh_ip"],
            "median_rtt_ms": median_rtt, "jitter_ms": jitter,
            "link_type": caps.get("link_type") or "unknown",
            "vram_gb": hwp.get("gpu_vram_gb"), "tier": hwp.get("recommended_tier"),
        }
        candidates.append(node)
        peer_meta[p["mesh_ip"]] = {"mesh_ip": p["mesh_ip"], "node_name": p.get("node_name")}

    placement = plan_pipeline_placement(n_layers, model_bytes, candidates, max_nodes=max_nodes)

    # Fits-on-one-box: tell the caller to load locally; no pipeline, no peer stages.
    if placement["mode"] == "whole":
        logger.info("pipeline: model %s fits on the head alone -> whole-model (no split)", model_id)
        return {"mode": "whole", "model_id": model_id, "n_layers": n_layers, "dtype": dtype}

    order = placement["order"]      # chosen nodes, head first, in chain order (link-annotated)
    counts = placement["layers"]    # contiguous per-node layer counts
    split, split2 = placement["split"], placement["split2"]
    chain_peers = order[1:]         # everything after the head is a downstream stage
    if not chain_peers:
        raise RuntimeError("no household peers are available for pipeline-parallel inference")

    by_id = {nd["id"]: nd for nd in order}  # resolve a chosen node's link fields by id

    def _link_summary(nid: str) -> dict:
        """Surface the chosen node's link characterization in the chain summary so an
        operator/UI can see WHY the chain was ordered this way (wired vs wifi, jitter)."""
        nd = by_id.get(nid, {})
        return {
            "link_type": nd.get("link_type", "unknown"),
            "rtt_ms": round(float(nd.get("median_rtt_ms", 0.0)), 1),
            "jitter_ms": round(float(nd.get("jitter_ms", 0.0)), 1),
            "link_cost": round(float(nd.get("link_cost", 0.0)), 1),
        }

    nodes = [{"role": "head", "node": "this", "range": f"[0:{split}]", **_link_summary("this")}]
    started = []  # peer mesh IPs whose stage we've started, for rollback on partial failure
    try:
        if len(chain_peers) == 1:
            tail_ip = chain_peers[0]["id"]
            _start_peer_stage(tail_ip, {
                "model_id": model_id, "role": "tail", "split": split,
                "port": stage_port, "device": device, "dtype": dtype,
            }, timeout)
            started.append(tail_ip)
            nodes.append({"role": "tail", "node": peer_meta[tail_ip]["node_name"],
                          "range": f"[{split}:{n_layers}]", **_link_summary(tail_ip)})
            chain = {"split": split, "split2": 0, "peer": tail_ip, "port": stage_port}
        else:
            mid_ip, tail_ip = chain_peers[0]["id"], chain_peers[1]["id"]
            _start_peer_stage(tail_ip, {  # tail first
                "model_id": model_id, "role": "tail", "split": split, "split2": split2,
                "port": stage_port, "device": device, "dtype": dtype,
            }, timeout)
            started.append(tail_ip)
            _start_peer_stage(mid_ip, {  # then mid (dials the tail)
                "model_id": model_id, "role": "mid", "split": split, "split2": split2,
                "port": stage_port, "device": device, "dtype": dtype,
                "peer2": tail_ip, "port2": stage_port,
            }, timeout)
            started.append(mid_ip)
            nodes.append({"role": "mid", "node": peer_meta[mid_ip]["node_name"],
                          "range": f"[{split}:{split2}]", **_link_summary(mid_ip)})
            nodes.append({"role": "tail", "node": peer_meta[tail_ip]["node_name"],
                          "range": f"[{split2}:{n_layers}]", **_link_summary(tail_ip)})
            chain = {"split": split, "split2": split2, "peer": mid_ip, "port": stage_port}
    except Exception:
        for ip in reversed(started):  # roll back stages already started so peers aren't wedged
            _stop_peer_stage(ip)
        raise

    # stage_peers lets the caller (load endpoint) tear the chain down if the HEAD load
    # then fails -- otherwise the peer stages would be left running.
    chain.update(mode="pipeline", dtype=dtype, n_layers=n_layers, nodes=nodes, stage_peers=started)
    return chain


def main(argv=None):
    # Standalone CLI: surface the module's log lines (load/serve) like the old prints.
    # No-op inside the sidecar, which already configures logging.
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser(description="Potluck pipeline-parallel inference node")
    ap.add_argument("--role", required=True, choices=["head", "mid", "tail"])
    ap.add_argument("--model", default="Qwen/Qwen2.5-3B-Instruct")
    ap.add_argument("--split", type=int, required=True, help="first layer boundary: head=[0:split]")
    ap.add_argument("--split2", type=int, default=0,
                    help="second boundary for a 3-node chain: mid=[split:split2], tail=[split2:]. 0 => 2-node")
    ap.add_argument("--peer", default="127.0.0.1", help="head/mid: address of the NEXT hop downstream")
    ap.add_argument("--port", type=int, default=29501, help="this node's listen port (mid/tail) or the next hop's port (head)")
    ap.add_argument("--peer2", default="", help="mid: address of the tail (downstream of the mid)")
    ap.add_argument("--port2", type=int, default=0, help="mid: the tail's listen port")
    ap.add_argument("--prompt", default="Explain decentralized computing in two sentences.")
    ap.add_argument("--max-new", type=int, default=64)
    ap.add_argument("--timeout", type=float, default=30.0,
                    help="per-edge round-trip budget (s); exceed it => clean fail, never a hang")
    ap.add_argument("--device", default="", help="cuda|mps|cpu|dml; autodetect if empty")
    ap.add_argument("--dtype", default="", help="float16|bfloat16|float32; default fp16 on mps/dml else bf16")
    ap.add_argument("--mesh", action="store_true",
                    help="head: resolve the peer via the Potluck mesh /peers socket and dial its 100.64.x mesh IP (no tunnel)")
    ap.add_argument("--mesh-peer", default="", help="household peer node_name to target (default: first peer with a mesh IP)")
    ap.add_argument("--concurrency", type=int, default=1,
                    help="head: number of simultaneous generation streams to drive (throughput test)")
    ap.add_argument("--batch", type=int, default=1,
                    help="head: run N sequences in ONE batched forward per step (continuous-batching primitive)")
    ap.add_argument("--draft", default="",
                    help="head: HF id of a small DRAFT model -> SPECULATIVE DECODING (K+1 verified per "
                         "chain round-trip, output identical to greedy). Empty => plain greedy decode.")
    ap.add_argument("--draft-k", type=int, default=4, help="speculative: draft tokens proposed per round")
    args = ap.parse_args(argv)

    if args.mesh and args.role == "head":
        ip, pname = resolve_mesh_peer(args.mesh_peer or None)
        if not ip:
            print("[head] --mesh: no household peer with a mesh IP found via the mesh socket", flush=True)
            raise SystemExit(2)
        args.peer = ip
        print(f"[head] mesh peer: {pname} @ {ip} (Potluck wg mesh -- no SSH tunnel, no firewall change)", flush=True)

    wire.key_from_env()  # shared swarm key (SHARD_PSK); fail fast before the model load
    dev = resolve_device(args.device)
    dtype = getattr(torch, args.dtype) if args.dtype else default_dtype(dev)
    print(f"[{args.role}] device={dev} dtype={dtype}", flush=True)
    parts = load_parts(args.model, args.split, args.role, device=dev, dtype=dtype, split2=args.split2)

    if args.role == "tail":
        serve_tail(parts, dev, port=args.port, timeout=args.timeout)
        return

    if args.role == "mid":
        if not args.peer2 or not args.port2:
            print("[mid] --peer2 and --port2 (the downstream tail) are required for a 3-node chain", flush=True)
            raise SystemExit(2)
        serve_mid(parts, dev, port=args.port, peer2=args.peer2, port2=args.port2, timeout=args.timeout)
        return

    # role == head
    tok = AutoTokenizer.from_pretrained(args.model)

    if args.batch > 1:
        print(f"[head] batched run: {args.batch} sequences in one forward/step against {args.peer}:{args.port} ...", flush=True)
        try:
            r = generate_batch(parts, tok, args.peer, args.port, args.prompt, args.batch, args.max_new, dev, args.timeout)
        except TransportError as e:
            print(f"\n[head] TRANSPORT FAILURE: {e}", flush=True)
            os._exit(2)
        print(f"\n[head] === BATCH {r['batch']} ===  {r['per_row_tokens']} tokens/row | decode {r['decode_s']:.1f}s", flush=True)
        print(f"[head] AGGREGATE throughput: {r['total_tokens']} tokens / {r['decode_s']:.1f}s = {r['agg_tok_s']:.2f} tok/s (one batched forward/step)", flush=True)
        print(f"[head] sample output [row 0]: {r['text0'][:160]!r}", flush=True)
        return

    if args.concurrency > 1:
        # N simultaneous streams against one tail. Each stream is an independent
        # generate_one (own socket => own KV cache on the tail). Report per-stream tok/s
        # AND aggregate (all tokens / wall-clock): if aggregate beats a single stream,
        # concurrency is hiding the wire leg.
        n = args.concurrency
        prompts = [f"{args.prompt} (request {i + 1})" for i in range(n)]
        results = [None] * n
        print(f"[head] concurrency test: {n} simultaneous streams against {args.peer}:{args.port} ...", flush=True)

        def worker(i):
            try:
                results[i] = generate_one(parts, tok, args.peer, args.port, prompts[i], args.max_new, dev, args.timeout)
            except TransportError as e:
                results[i] = {"error": str(e)}

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        t_wall = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        wall = time.time() - t_wall
        ok = [r for r in results if r and "error" not in r]
        errs = [r for r in results if r and "error" in r]
        total_tokens = sum(r["n_tokens"] for r in ok)
        per_stream = sorted(r["tok_s"] for r in ok)
        agg = total_tokens / wall if wall > 0 else 0.0
        avg_ps = (sum(per_stream) / len(per_stream)) if per_stream else 0.0
        print(f"\n[head] === CONCURRENCY {n} ===  streams ok={len(ok)} failed={len(errs)} | wall {wall:.1f}s", flush=True)
        if per_stream:
            print(f"[head] per-stream tok/s: min {per_stream[0]:.2f} / avg {avg_ps:.2f} / max {per_stream[-1]:.2f}", flush=True)
        print(f"[head] AGGREGATE throughput: {total_tokens} tokens / {wall:.1f}s = {agg:.2f} tok/s across all streams", flush=True)
        if ok:
            print(f"[head] sample output [stream 1]: {ok[0]['text'][:160]!r}", flush=True)
        for e in errs:
            print(f"[head] stream error: {e['error']}", flush=True)
        return

    if args.draft:
        # SPECULATIVE head: drive generation through the engine's spec path (loads the
        # small draft locally). Reuse the already-loaded head block by attaching it.
        print(f"[head] speculative decode: draft={args.draft} K={args.draft_k} against tail "
              f"{args.peer}:{args.port} ...", flush=True)
        eng = PipelineParallelEngine.__new__(PipelineParallelEngine)
        eng.model_id, eng.split, eng.split2 = args.model, args.split, args.split2
        eng.peer, eng.port, eng.timeout = args.peer, args.port, args.timeout
        eng.device, eng.dtype, eng.parts = dev, dtype, parts
        eng.n_layers, eng.tokenizer = parts["n_layers"], tok
        eng.draft_k = max(1, args.draft_k)
        eng.draft = AutoModelForCausalLM.from_pretrained(
            args.draft, dtype=dtype, device_map=str(dev), attn_implementation="eager").eval()
        eng.draft_model_id = args.draft
        stats = {}
        t0 = time.time()
        text = "".join(eng._stream_tokens_spec(
            [{"role": "user", "content": args.prompt}], max_new=args.max_new, _stats=stats))
        dt = time.time() - t0
        print(f"\n[head] === OUTPUT ===\n{text}\n", flush=True)
        print(f"[head] {stats.get('n_tokens', 0)} tokens in {stats.get('round_trips', 0)} chain "
              f"round-trips ({stats.get('n_tokens', 0)/max(stats.get('round_trips', 1),1):.2f} tok/round-trip "
              f"vs 1.0 greedy) | {stats.get('n_tokens', 0)/max(dt, 1e-9):.2f} tok/s | "
              f"draft={args.draft.split('/')[-1]} K={args.draft_k}", flush=True)
        return

    print(f"[head] generating against tail {args.peer}:{args.port} (edge timeout {args.timeout:.0f}s) ...", flush=True)
    try:
        r = generate_one(parts, tok, args.peer, args.port, args.prompt, args.max_new, dev, args.timeout)
    except TransportError as e:
        # unrecoverable edge failure: socket already closed in generate_one's finally,
        # message already flushed -- die now rather than wait out the multi-second
        # torch/cuda teardown (which an operator can't tell from a hang).
        print(f"\n[head] TRANSPORT FAILURE: {e}", flush=True)
        os._exit(2)
    print(f"\n[head] === OUTPUT ===\n{r['text']}\n", flush=True)
    print(f"[head] {r['n_tokens']} tokens, total {r['total_s']:.1f}s | prefill {r['prefill_s']:.2f}s | "
          f"decode {r['tok_s']:.2f} tok/s (kv-cache, head holds {args.split}/{parts['n_layers']} layers)", flush=True)
    print(f"[head] edge health: {r['hops']} round-trips | rt/step avg {r['rt_ms_avg']:.1f}ms "
          f"max {r['rt_ms_max']:.1f}ms | {r['mb_up']:.1f}MB up", flush=True)
    print(f"[head] per-token breakdown: head-compute {r['head_ms_avg']:.1f}ms + network {r['net_ms_avg']:.1f}ms "
          f"+ tail-compute {r['tail_ms_avg']:.1f}ms (network is the part that shrinks on a wired link)", flush=True)


if __name__ == "__main__":
    main()
