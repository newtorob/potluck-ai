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
except ImportError:  # running this file directly rather than as a package module
    import pipeline_wire as wire
    from pipeline_wire import send_msg, recv_msg

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
                 device="", dtype=None, timeout=30.0):
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

    def describe(self):
        """Status for /health + the Models page (mirrors loaded_rpc_servers)."""
        return {
            "model_id": self.model_id,
            "strategy": "pipeline",
            "head_layers": self.split,
            "n_layers": self.n_layers,
            "downstream": f"{self.peer}:{self.port}",
            "device": str(self.device),
            "dtype": str(self.dtype),
        }

    def stream_tokens(self, messages, *, max_new=512):
        """Blocking generator yielding text DELTAS as each token is decoded. The caller
        (LLMRuntime.stream_chat) runs this in a worker thread and pumps it onto an async
        queue. Raises TransportError on a dead/stalled edge instead of hanging."""
        enc = self.tokenizer.apply_chat_template(
            messages, add_generation_prompt=True, return_tensors="pt", return_dict=True)
        ids = enc["input_ids"].to(self.device)
        cache = DynamicCache()
        sock = _connect_with_retry(self.peer, self.port, self.timeout)
        produced, start, emitted = [], 0, 0

        def step(h, abs_start):
            tok_id, _, _ = _send_recv(sock, h, abs_start)
            return tok_id

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


def serve_tail(parts, dev, *, port, timeout=30.0, host="0.0.0.0", stop_event=None, ready_event=None):
    """Run the tail stage: accept upstream connections (from a head or a mid), run this
    node's final block + norm + lm_head, and return the next token. One DynamicCache per
    connection => each concurrent stream gets its own KV state; the shared GPU is
    serialized by _COMPUTE (held for compute only, not across the wire), so streams
    overlap I/O while taking turns on the device.

    stop_event: when set, the accept loop exits and the listen socket closes (so a
    PipelineStageWorker can shut a stage down). ready_event: set once bound + listening,
    so a programmatic caller can wait for readiness. Blocks until stopped."""
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

    def serve(conn, addr):
        with active_lock:
            active["n"] += 1
            n = active["n"]
        logger.info("pipeline tail: upstream %s connected (active streams: %d)", addr, n)
        cache = DynamicCache()
        steps = 0
        try:
            with torch.no_grad():
                while True:
                    try:
                        h, start = recv_msg(conn)
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
        steps = 0
        try:
            with torch.no_grad():
                while True:
                    try:
                        h, start = recv_msg(conn)
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
                    {"id": str, "vram_gb": float|None, "tier": str|None, "rtt_ms": float}.
                    The head's rtt_ms is 0 (no link to itself).
      max_nodes  -- hard cap on chain length (engine is head/mid/tail => <=3).

    Returns one of:
      {"mode": "whole", "nodes": [head]}        -- the head alone fits the model;
                                                   skip the pipeline entirely (fastest).
      {"mode": "pipeline",                       -- a multi-node chain:
       "order": [node, ...],                     -- chosen nodes in chain order (head first)
       "layers": [count, ...],                   -- contiguous per-node layer counts (sum=n)
       "split": int, "split2": int}             -- the (split, split2) the rest expects.

    Decision rules:
      1. fits-on-one-box: if the head's own capacity >= model_bytes, return whole-model.
         No hop is faster than no hop.
      2. otherwise build a chain: head first, then peers ordered by rtt ASCENDING
         (lowest-latency links adjacent -- every hop is on the per-token critical path).
      3. add only as many peers as needed for the cumulative capacity to cover the
         model (fewest hops), capped at max_nodes.
      4. split layers PROPORTIONAL to each chosen node's capacity (big/fast node holds
         more), then CAP each node at what its VRAM can hold and spill the remainder to
         nodes with slack. Blocks stay contiguous: head=[0:a], mid=[a:b], tail=[b:n]."""
    if not nodes:
        raise ValueError("plan_pipeline_placement: need at least the head node")
    max_nodes = max(2, min(max_nodes, 3))
    head = nodes[0]
    bytes_per_gb = 1e9

    # Rule 1 -- fits-on-one-box short-circuit (the fastest option: no pipeline).
    head_cap_bytes = _node_capacity_gb(head) * bytes_per_gb
    if head_cap_bytes >= model_bytes:
        return {"mode": "whole", "nodes": [head]}

    # Rule 2 -- order peers by link latency (lowest RTT first); head stays at position 0.
    peers = sorted(nodes[1:], key=lambda p: (float(p.get("rtt_ms", 0.0)), str(p.get("id", ""))))

    # Rule 3 -- greedily admit the nearest peers until cumulative capacity covers the
    # model OR we hit the node cap. Fewest hops that fit.
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
    return {"mode": "pipeline", "order": chosen, "layers": layers,
            "split": split, "split2": split2}


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


def _probe_peer(mesh_ip: str, timeout: float = 5.0) -> tuple[dict | None, float]:
    """GET a peer's /peer/capabilities and TIME the round-trip. Returns (caps, rtt_ms);
    (None, inf) on any failure. Reusing the capabilities call as the latency probe means
    NO new endpoint -- the elapsed wall time of this request is a usable rtt signal for
    ordering peers (lowest-latency links adjacent on the per-token critical path)."""
    import httpx
    t0 = time.monotonic()
    try:
        caps = httpx.get(f"http://{mesh_ip}:8321/peer/capabilities", timeout=timeout).json()
    except Exception:
        return None, float("inf")
    return caps, (time.monotonic() - t0) * 1e3


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
    chain is ORDERED by link latency (lowest-RTT peers adjacent), using only as many
    peers as needed to fit (fewest hops; capped at max_nodes). dtype is forced uniform
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
    head_node = {
        "id": "this", "rtt_ms": 0.0,
        "vram_gb": hw.get("gpu_vram_gb"), "tier": head_tier,
    }

    # Conservative model footprint: prefer the config-derived param count; fall back to
    # the head's tier when the config is too sparse to estimate from (see the heuristic
    # docstrings). fp16 (2 bytes/param) -- matches the default dtype the chain loads in.
    n_params = estimate_params_from_config(cfg)
    model_bytes = estimate_model_vram_bytes(n_params, dtype_bytes=2, fallback_tier=head_tier)

    # Eligible peers + per-peer RTT (timed capabilities probe; no new endpoint).
    candidates = [head_node]
    peer_meta: dict[str, dict] = {}  # id -> {mesh_ip, node_name} to resolve placement back
    for p in _collect_household_peers():
        caps, rtt = _probe_peer(p["mesh_ip"])
        if not caps or not caps.get("available_for_pipeline"):
            continue
        hwp = caps.get("hardware") or {}
        node = {
            "id": p["mesh_ip"], "rtt_ms": rtt,
            "vram_gb": hwp.get("gpu_vram_gb"), "tier": hwp.get("recommended_tier"),
        }
        candidates.append(node)
        peer_meta[p["mesh_ip"]] = {"mesh_ip": p["mesh_ip"], "node_name": p.get("node_name")}

    placement = plan_pipeline_placement(n_layers, model_bytes, candidates, max_nodes=max_nodes)

    # Fits-on-one-box: tell the caller to load locally; no pipeline, no peer stages.
    if placement["mode"] == "whole":
        logger.info("pipeline: model %s fits on the head alone -> whole-model (no split)", model_id)
        return {"mode": "whole", "model_id": model_id, "n_layers": n_layers, "dtype": dtype}

    order = placement["order"]      # chosen nodes, head first, in chain order
    counts = placement["layers"]    # contiguous per-node layer counts
    split, split2 = placement["split"], placement["split2"]
    chain_peers = order[1:]         # everything after the head is a downstream stage
    if not chain_peers:
        raise RuntimeError("no household peers are available for pipeline-parallel inference")

    nodes = [{"role": "head", "node": "this", "range": f"[0:{split}]"}]
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
                          "range": f"[{split}:{n_layers}]"})
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
                          "range": f"[{split}:{split2}]"})
            nodes.append({"role": "tail", "node": peer_meta[tail_ip]["node_name"],
                          "range": f"[{split2}:{n_layers}]"})
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
