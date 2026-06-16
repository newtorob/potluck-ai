"""Closure scoring-challenge detector (OBSERVE-ONLY).

Accumulates the per-peer model-substitution signal from the scoring challenge
and produces an advisory suspicion verdict. It NEVER slashes, forfeits escrow,
or mutates reputation on its own; the coordinator decides what (if anything) to
do with a verdict, and for now that is: log and surface, nothing else.

Signal (see ~/Documents/potluck-closure-verification-mechanism-2026-06-15.md):
  For a fixed sequence O, an anchor running the genuine model X and the
  peer-under-test each return an average per-token log-prob (via /verify/score).
  gap = anchor_avg_logprob - peer_avg_logprob.
    - Honest peer (also running X): gap ~ 0, jittered by the quant/backend band.
    - Peer running a cheaper model Y: gap > 0 (X is more confident in O than Y).

Why accumulation (empirically required, not optional): one challenge cannot
separate a realistic 2x-cheaper impostor (gap mean ~0.27 nats/token, std ~0.21)
from an honest peer on a different quant (band mean ~0, std ~0.07) -- the
per-instance distributions overlap. The running mean does separate them: with
these numbers, at the default 2-sigma confidence (DEFAULT_Z=2.0), a peer needs
roughly 8-15 challenges before a 2x impostor clears the threshold (more at higher
z), a few for a 6x impostor, and a very close impostor may never clear it. So
this is a slow-burn reputation signal.

Defaults below come from the 2026-06-15 Mac experiment; they are placeholders
until calibrated on real heterogeneous peers (the gate before this ever drives
payout).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

# --- Calibration constants (placeholders; recalibrate on the fleet/real peers).
# Honest cross-quant band std, nats/token (q4-vs-q8 measured ~0.07).
DEFAULT_BAND_STD = 0.07
# Decision threshold on the running-mean gap, nats/token. Above the band, below
# the measured 2x-impostor mean (0.27), so an honest peer stays clear and a 2x
# impostor eventually crosses.
DEFAULT_THRESHOLD = 0.10
# Don't render a "suspected" verdict before this many challenges (the running
# mean is too noisy earlier).
DEFAULT_MIN_SAMPLES = 8
# Confidence margin: require the lower bound (mean - Z * stderr) to exceed the
# threshold before flagging. Z=2 ~ 97.5% one-sided.
DEFAULT_Z = 2.0


def gap_per_token(anchor_avg_logprob: float, peer_avg_logprob: float) -> float:
    """Per-token gap for one challenge. Positive => peer less confident than the
    anchor (the impostor direction)."""
    return anchor_avg_logprob - peer_avg_logprob


class _Welford:
    """Online mean/variance (Welford). Numerically stable, O(1) per sample."""

    __slots__ = ("n", "mean", "_m2")

    def __init__(self) -> None:
        self.n = 0
        self.mean = 0.0
        self._m2 = 0.0

    def update(self, x: float) -> None:
        self.n += 1
        d = x - self.mean
        self.mean += d / self.n
        self._m2 += d * (x - self.mean)

    @property
    def variance(self) -> float:
        return self._m2 / (self.n - 1) if self.n > 1 else 0.0

    @property
    def std(self) -> float:
        return math.sqrt(self.variance)

    @property
    def stderr(self) -> float:
        return self.std / math.sqrt(self.n) if self.n > 0 else float("inf")


@dataclass(frozen=True)
class Assessment:
    peer_id: str
    samples: int
    mean_gap: float
    std_gap: float
    stderr: float
    # Advisory only. True => the accumulated evidence suggests this peer is
    # serving a cheaper model than advertised. Never auto-acts.
    suspected: bool
    note: str


@dataclass
class ClosureDetector:
    """Observe-only per-peer accumulator for the scoring-challenge signal."""

    threshold: float = DEFAULT_THRESHOLD
    min_samples: int = DEFAULT_MIN_SAMPLES
    band_std: float = DEFAULT_BAND_STD
    z: float = DEFAULT_Z
    _peers: dict[str, _Welford] = field(default_factory=dict)

    def record(self, peer_id: str, anchor_avg_logprob: float, peer_avg_logprob: float) -> None:
        """Record one challenge result for a peer (anchor + peer avg log-probs)."""
        self._peers.setdefault(peer_id, _Welford()).update(
            gap_per_token(anchor_avg_logprob, peer_avg_logprob)
        )

    def record_gap(self, peer_id: str, gap: float) -> None:
        """Record a pre-computed per-token gap (for tests / replay)."""
        self._peers.setdefault(peer_id, _Welford()).update(gap)

    def assess(self, peer_id: str) -> Assessment:
        w = self._peers.get(peer_id)
        if w is None or w.n == 0:
            return Assessment(peer_id, 0, 0.0, 0.0, float("inf"), False,
                              "no challenges recorded")
        if w.n < self.min_samples:
            return Assessment(peer_id, w.n, w.mean, w.std, w.stderr, False,
                              f"accumulating ({w.n}/{self.min_samples} challenges)")
        # Lower confidence bound on the mean gap must clear the threshold.
        lcb = w.mean - self.z * w.stderr
        suspected = lcb > self.threshold
        if suspected:
            note = (f"suspected cheaper model: mean gap {w.mean:.3f} nats/tok "
                    f"(lcb {lcb:.3f} > {self.threshold}); ADVISORY, not enforced")
        else:
            note = (f"within tolerance: mean gap {w.mean:.3f} nats/tok "
                    f"(lcb {lcb:.3f} <= {self.threshold})")
        return Assessment(peer_id, w.n, w.mean, w.std, w.stderr, suspected, note)

    def reset_peer(self, peer_id: str) -> None:
        """Drop a peer's history (e.g. after it re-pairs / reloads a model)."""
        self._peers.pop(peer_id, None)
