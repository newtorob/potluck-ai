"""Pipeline wire + key-loading tests.

Two properties matter here and neither needs a GPU or a real model:

  1. The wire-key error path. The programmatic entry points (the engine, the /peer
     stage worker) reach the codec WITHOUT the CLI main() that historically loaded
     SHARD_PSK. _ensure_wire_key() exists to turn the codec's fail-fast SystemExit
     into a clean RuntimeError, so the HTTP layer returns a readable 500 instead of
     the sidecar process exiting. A SystemExit escaping here would kill the worker.

  2. The frame codec round-trips a (tensor, int) message exactly through
     pack -> seal -> open -> unpack — the actual payload shape _send_recv puts on
     the wire — and a tampered/wrong-key frame is rejected, not trusted.

Everything in this module imports torch, so the whole file is gated on
`pytest.importorskip("torch")`: in a torch-free base environment the suite still
passes (these are simply skipped).
"""

from __future__ import annotations

import socket
import threading

import pytest

# Gate the entire module: pipeline_parallel + pipeline_wire top-import torch. In a
# torch-free base env this skips cleanly so the suite still passes.
pytest.importorskip("torch")

import torch  # noqa: E402  (after importorskip by design)

from potluck_local.services import pipeline_parallel, pipeline_wire as wire  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate_wire_key(monkeypatch):
    """Each test starts with no SHARD_PSK and no process key, and we restore the
    module's global _KEY afterward so we never leak a key into other test modules."""
    monkeypatch.delenv("SHARD_PSK", raising=False)
    saved = wire._KEY
    wire._KEY = None
    yield
    wire._KEY = saved


def test_ensure_wire_key_raises_runtimeerror_when_psk_unset(monkeypatch) -> None:
    """With SHARD_PSK unset, _ensure_wire_key() raises a clean RuntimeError — NOT a
    SystemExit. A SystemExit would tear down the sidecar instead of returning an error."""
    monkeypatch.delenv("SHARD_PSK", raising=False)
    with pytest.raises(RuntimeError):
        pipeline_parallel._ensure_wire_key()
    # And specifically not SystemExit (BaseException, so it would slip past `except
    # Exception`). pytest.raises(RuntimeError) above already excludes it, but assert
    # the negative explicitly since that conversion is the whole point of the helper.
    monkeypatch.delenv("SHARD_PSK", raising=False)
    try:
        pipeline_parallel._ensure_wire_key()
    except SystemExit:  # pragma: no cover - this is the failure we are guarding against
        pytest.fail("_ensure_wire_key leaked a SystemExit instead of RuntimeError")
    except RuntimeError:
        pass


def test_ensure_wire_key_loads_key_when_psk_set(monkeypatch) -> None:
    """With SHARD_PSK set, _ensure_wire_key() loads the process key (no raise)."""
    monkeypatch.setenv("SHARD_PSK", "test-swarm-secret")
    assert wire._KEY is None
    pipeline_parallel._ensure_wire_key()
    assert wire._KEY is not None


def test_wire_frame_roundtrip_tensor_and_int() -> None:
    """A (tensor, int) message — the exact shape _send_recv frames — round-trips
    byte-exact through pack -> seal -> open -> unpack."""
    wire.use_key("test-swarm-secret")
    h = torch.randn(1, 4, 8, dtype=torch.bfloat16)
    start = 12
    decoded = wire._unpack(wire._open(wire._seal(wire._pack((h, start)))))
    h2, start2 = decoded  # tuple arrives as a 2-list, unpacks positionally
    assert start2 == start
    assert h2.dtype == torch.bfloat16
    assert torch.equal(h2, h)


def test_wire_tampered_frame_rejected() -> None:
    """A single flipped byte fails the auth tag — a forged/tampered frame is rejected,
    never decoded and trusted."""
    from cryptography.exceptions import InvalidTag

    wire.use_key("test-swarm-secret")
    frame = bytearray(wire._seal(wire._pack({"op": "reset"})))
    frame[-1] ^= 1
    with pytest.raises(InvalidTag):
        wire._open(bytes(frame))


def test_send_recv_roundtrip_over_socketpair() -> None:
    """End-to-end framing round-trip over an in-memory socket pair: send_msg on one
    end, recv_msg on the other, recovering the (tensor, abs_start) payload exactly.
    No real network, no model — just the framing layer the stages speak."""
    wire.use_key("test-swarm-secret")
    a, b = socket.socketpair()
    try:
        h = torch.randn(1, 3, 8, dtype=torch.bfloat16)
        abs_start = 7
        received: dict[str, object] = {}

        def _reader() -> None:
            received["msg"] = wire.recv_msg(b)

        t = threading.Thread(target=_reader)
        t.start()
        nbytes = wire.send_msg(a, (h.cpu(), abs_start))
        t.join(timeout=5.0)
        assert not t.is_alive(), "recv_msg did not return"

        assert nbytes > 0
        h2, start2 = received["msg"]
        assert start2 == abs_start
        assert torch.equal(h2, h)
    finally:
        a.close()
        b.close()
