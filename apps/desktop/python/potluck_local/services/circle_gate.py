"""Who may call this machine's circle listener.

The circle listener (routers/circle_link.py) is a second port that answers
people outside the owner's household. Its credential is a direct circle link:
the coordinator brokered a short-lived WireGuard peer for one other machine, and
this machine's own mesh sidecar lists that peer under /circle-peers with the
SHA-256 of the link's token. A caller is let in only when all of these hold:

  1. the connection was accepted on this machine's own mesh address, so a
     process on this machine, or a device on the same LAN, cannot pose as a peer
     by dialing 127.0.0.1 or a LAN address,
  2. its source address is a reachable /circle-peers entry whose `inbound` is
     not null (an `outbound`-only entry means this machine borrows from that
     peer; it gives the peer no right to call us),
  3. the bearer token's SHA-256, hex of the token text exactly as sent, equals
     that entry's `inbound.token_sha256`, compared in constant time, and
  4. this machine still shares with circles and its admission policy allows it
     (checked by the caller; see routers/circle_link.py).

The source address and the accepted address come from the TCP socket, never from
a header. Anything missing, malformed or unreadable is a refusal. A stream that
is already running is checked again every couple of seconds with rules 1 and 2
(the token is checked only when a request starts, so replacing a link with a new
one does not cut a stream on the old one).

The sidecar is read through a small cache with the shape of
peer_trust.HouseholdRoster: reads are shared and rate limited, whatever the
number of callers.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from . import circle_mesh, mesh_ipc
from .peer_trust import IPAddress, parse_ip

logger = logging.getLogger(__name__)

# A read is reused for this long. Ending a link takes effect for new requests
# within it.
CACHE_TTL_S = 1.0
# A stream that is running is checked against a read no older than this, so the
# checks of many streams share reads.
MIN_READ_INTERVAL_S = 0.25
# A caller that is turned away because of what the cache says gets one fresh look
# unless the cache is younger than this. The reason is a link made a moment ago: the
# lender's sidecar holds it before the requester has the token, and a cache that
# was read just before must not turn the first request away. Callers that fail
# for another reason (wrong local address, own address) cause no read at all.
RETRY_READ_INTERVAL_S = 0.02
# The sidecar answers in milliseconds on a local socket.
READ_TIMEOUT_S = 1.0
# A bearer token longer than this is refused without being hashed.
MAX_TOKEN_CHARS = 512

ALIVE = "alive"
GONE = "gone"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class CircleState:
    """This machine's mesh address, and the circle peers that may call it."""

    own_ip: IPAddress
    # source address -> hex SHA-256 of the token of the link that admits it
    inbound: Mapping[IPAddress, str] = field(default_factory=dict)


def parse_state(status: Any, peers: Any) -> CircleState | None:
    """A CircleState from the sidecar's /status and /circle-peers bodies, or None."""
    parsed = circle_mesh.parse_status(status)
    entries = circle_mesh.parse_circle_peers(peers)
    if parsed is None or parsed.mesh_ip is None or entries is None:
        return None
    own = parsed.mesh_ip
    inbound = {
        entry.mesh_ip: entry.inbound.token_sha256
        for entry in entries
        if entry.reachable and entry.inbound is not None and entry.mesh_ip != own
    }
    return CircleState(own_ip=own, inbound=inbound)


def token_digest(token: str) -> str | None:
    """Hex SHA-256 of the token text as sent. None for text that is not ASCII."""
    try:
        return hashlib.sha256(token.encode("ascii")).hexdigest()
    except UnicodeEncodeError:
        return None


def bearer_token(authorization: str | None) -> str | None:
    """The token in an `Authorization: Bearer <token>` value, or None."""
    if not authorization or authorization[:7].lower() != "bearer ":
        return None
    token = authorization[7:].strip()
    return token or None


def decide(
    state: CircleState | None,
    client_host: Any,
    server_host: Any,
    token: str | None,
    *,
    check_token: bool = True,
) -> str | None:
    """Why this caller may not use the listener, or None when it may.

    `client_host` is the TCP source address, `server_host` the local address the
    connection was accepted on. The reason is a short code for debug logs and for
    telling a missing token (401) from a caller that is not a circle peer (403);
    it is never sent to the caller.
    """
    if state is None:
        return "roster_unavailable"
    client = parse_ip(client_host)
    if client is None:
        return "bad_source"
    if client == state.own_ip:
        return "own_address"
    if parse_ip(server_host) != state.own_ip:
        return "wrong_local_address"
    expected = state.inbound.get(client)
    if expected is None:
        return "not_inbound"
    if not check_token:
        return None
    if token is None or len(token) > MAX_TOKEN_CHARS:
        return "no_token"
    digest = token_digest(token)
    if digest is None or not hmac.compare_digest(digest, expected):
        return "bad_token"
    return None


# Refusals that a newer read of the sidecar cannot turn into a yes.
_NOT_FIXED_BY_A_NEWER_READ = frozenset({"bad_source", "own_address", "wrong_local_address"})


def is_token_reason(reason: str) -> bool:
    """True for refusals that are about the token (401), false for the rest (403)."""
    return reason in ("no_token", "bad_token")


async def read_state() -> CircleState | None:
    """One read of this machine's mesh sidecar. None when it cannot be trusted."""
    try:
        status = await mesh_ipc.mesh_get("/status", timeout=READ_TIMEOUT_S)
        if not isinstance(status, dict) or parse_ip(status.get("mesh_ip")) is None:
            return None
        peers = await mesh_ipc.mesh_get("/circle-peers", timeout=READ_TIMEOUT_S)
        return parse_state(status, peers)
    except Exception:  # noqa: BLE001 - every failure means "nothing is known"
        return None


class CircleRoster:
    """A short-lived, rate-limited view of the circle peers for the listener gate."""

    def __init__(
        self,
        read: Callable[[], Awaitable[CircleState | None]] = read_state,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._clock = clock
        self._state: CircleState | None = None
        self._read_at: float | None = None  # when the read behind _state started
        self._done_at: float | None = None  # when that read finished
        self._inflight: asyncio.Task[CircleState | None] | None = None
        self._was_available: bool | None = None

    async def refusal_reason(self, client_host: Any, server_host: Any, token: str | None) -> str | None:
        """None when the caller may use the listener, else a short reason code."""
        try:
            began = self._clock()
            reason = decide(await self._snapshot(began, retry=False), client_host, server_host, token)
            if reason is None or reason in _NOT_FIXED_BY_A_NEWER_READ:
                return reason
            # A link made a moment ago should not have to wait out the cache:
            # look once more, within the read limit.
            return decide(await self._snapshot(began, retry=True), client_host, server_host, token)
        except Exception:  # noqa: BLE001 - whatever went wrong, the caller is refused
            return "check_failed"

    async def link_state(self, client_host: Any, server_host: Any) -> str:
        """For a stream in progress: ALIVE while the source is still a circle peer
        that may call us, GONE when the sidecar says it is not, UNKNOWN when the
        sidecar cannot be read. Always a recent read, never the cache."""
        try:
            state = await self._fresh()
        except Exception:  # noqa: BLE001
            return UNKNOWN
        if state is None:
            return UNKNOWN
        return ALIVE if decide(state, client_host, server_host, None, check_token=False) is None else GONE

    async def _fresh(self) -> CircleState | None:
        if self._read_at is not None and self._clock() - self._read_at < MIN_READ_INTERVAL_S:
            return self._state
        return await self._refresh()

    async def _snapshot(self, began: float, *, retry: bool) -> CircleState | None:
        if self._read_at is not None:
            age = self._clock() - self._read_at
            if retry:
                # Skip when a read finished since this request began (it is as
                # new as a retry would be) or when the last read began a moment ago.
                if (self._done_at is not None and self._done_at > began) or age < RETRY_READ_INTERVAL_S:
                    return self._state
            elif age < CACHE_TTL_S:
                return self._state
        return await self._refresh()

    async def _refresh(self) -> CircleState | None:
        """Share one in-flight read between every request that needs it."""
        loop = asyncio.get_running_loop()
        task = self._inflight
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._load())
            self._inflight = task
        # A caller that goes away must not cancel the read the others wait on.
        return await asyncio.shield(task)

    async def _load(self) -> CircleState | None:
        started = self._clock()
        try:
            state = await self._read()
        except Exception:  # noqa: BLE001
            state = None
        finally:
            if self._inflight is asyncio.current_task():
                self._inflight = None
        self._state, self._read_at, self._done_at = state, started, self._clock()
        available = state is not None
        if available != self._was_available:
            self._was_available = available
            if available:
                logger.info("circle_roster_available")
            else:
                logger.warning("circle_roster_unavailable circle callers are refused until the mesh sidecar answers")
        return state


class DirectSlot:
    """One direct request's place under the lender's job cap. Release is idempotent."""

    __slots__ = ("_admission", "_released")

    def __init__(self, admission: "DirectAdmission") -> None:
        self._admission = admission
        self._released = False

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._admission._in_flight -= 1


class DirectAdmission:
    """The direct requests in flight, counted against the same cap as relay jobs.

    `cap` is network.max_concurrent_jobs read live; `other_in_flight` is the
    number of relay jobs running. Both run on the engine's event loop, so a check
    followed by an increment cannot interleave with another admission.
    """

    def __init__(self, cap: Callable[[], int | None], other_in_flight: Callable[[], int]) -> None:
        self._cap = cap
        self._other = other_in_flight
        self._in_flight = 0

    @property
    def in_flight(self) -> int:
        return self._in_flight

    def snapshot(self) -> tuple[int, int | None]:
        """(jobs in flight across both paths, the cap)."""
        return self._in_flight + self._other(), self._cap()

    def try_acquire(self) -> DirectSlot | None:
        in_flight, cap = self.snapshot()
        if cap is not None and in_flight >= cap:
            return None
        self._in_flight += 1
        return DirectSlot(self)
