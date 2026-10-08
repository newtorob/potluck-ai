"""Who counts as a member of this machine's household.

The engine listens on every interface so the owner's other machines can reach
it over the WireGuard mesh. A source address inside the mesh range (100.64.0.0/10)
does not prove membership: Tailscale uses the same range and so do some
carrier and campus networks. This machine's own mesh sidecar knows the real
household, so a caller is a member only when all of these hold:

  1. its source address is a reachable machine in the sidecar's `/peers` roster
     (a WireGuard handshake in the last three minutes),
  2. that address is not this machine's own mesh address, and
  3. the connection was accepted on this machine's own mesh address, so a
     process on this machine cannot pose as a peer by dialing 127.0.0.1 or a
     LAN address.

The source address and the accepted address both come from the TCP socket,
never from request headers. Anything missing, malformed or unreadable means
"not a member".
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from . import mesh_ipc

logger = logging.getLogger(__name__)

# A roster is reused for this long. Removing a machine from the household takes
# effect within it.
CACHE_TTL_S = 3.0
# The sidecar is never read more often than this, however many strangers knock.
MIN_READ_INTERVAL_S = 1.0
# The sidecar answers in milliseconds on a local socket.
READ_TIMEOUT_S = 1.0

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


@dataclass(frozen=True)
class Roster:
    """This machine's mesh address and the addresses of its reachable peers."""

    own_ip: IPAddress
    peer_ips: frozenset[IPAddress]


def parse_ip(value: Any) -> IPAddress | None:
    """An IP address from a string, or None. IPv4-mapped IPv6 becomes plain IPv4.

    Only strings count: `ipaddress` would also take an int, which would let a
    junk roster value become an address.
    """
    if not isinstance(value, str):
        return None
    try:
        addr = ipaddress.ip_address(value)
    except ValueError:
        return None
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr


def parse_roster(status: Any, peers: Any) -> Roster | None:
    """Build a Roster from the sidecar's `/status` and `/peers` bodies, or None."""
    own = parse_ip(status.get("mesh_ip")) if isinstance(status, dict) else None
    if own is None or not isinstance(peers, dict) or not isinstance(peers.get("peers"), list):
        return None
    reachable: set[IPAddress] = set()
    for entry in peers["peers"]:
        if not isinstance(entry, dict) or entry.get("reachable") is not True:
            continue
        ip = parse_ip(entry.get("mesh_ip"))
        if ip is not None and ip != own:
            reachable.add(ip)
    return Roster(own_ip=own, peer_ips=frozenset(reachable))


def decide(roster: Roster | None, client_host: Any, server_host: Any) -> str | None:
    """Why this caller is not a household member, or None when it is.

    `client_host` is the TCP source address and `server_host` the local address
    the connection was accepted on. The reason is a short code for debug logs;
    it is never sent to the caller.
    """
    if roster is None:
        return "roster_unavailable"
    client = parse_ip(client_host)
    if client is None:
        return "bad_source"
    if client == roster.own_ip:
        return "own_address"
    if client not in roster.peer_ips:
        return "not_in_roster"
    if parse_ip(server_host) != roster.own_ip:
        return "wrong_local_address"
    return None


async def read_roster() -> Roster | None:
    """One read of this machine's mesh sidecar. None when it cannot be trusted."""
    try:
        status = await mesh_ipc.mesh_get("/status", timeout=READ_TIMEOUT_S)
        if not isinstance(status, dict) or parse_ip(status.get("mesh_ip")) is None:
            return None
        peers = await mesh_ipc.mesh_get("/peers", timeout=READ_TIMEOUT_S)
        return parse_roster(status, peers)
    except Exception:  # noqa: BLE001 - every failure means "nothing is known"
        return None


class HouseholdRoster:
    """A short-lived, rate-limited view of the household for the peer gate."""

    def __init__(
        self,
        read: Callable[[], Awaitable[Roster | None]] = read_roster,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._clock = clock
        self._roster: Roster | None = None
        self._read_at: float | None = None  # when the read behind _roster started
        self._done_at: float | None = None  # when that read finished
        self._inflight: asyncio.Task[Roster | None] | None = None
        self._was_available: bool | None = None

    async def refusal_reason(self, client_host: Any, server_host: Any) -> str | None:
        """None when the caller is a household member, else a short reason code."""
        try:
            began = self._clock()
            reason = decide(await self._snapshot(began, retry=False), client_host, server_host)
            if reason is None:
                return None
            # A machine that just joined, or a roster that went stale, should not
            # have to wait out the cache: look once more, within the read limit.
            return decide(await self._snapshot(began, retry=True), client_host, server_host)
        except Exception:  # noqa: BLE001 - whatever went wrong, the caller is not a member
            return "check_failed"

    async def _snapshot(self, began: float, *, retry: bool) -> Roster | None:
        if self._read_at is not None:
            age = self._clock() - self._read_at
            if retry:
                # Skip when a read finished since this request began (it is as
                # new as a retry would be) or when the last read began under a
                # second ago.
                if (self._done_at is not None and self._done_at > began) or age < MIN_READ_INTERVAL_S:
                    return self._roster
            elif age < CACHE_TTL_S:
                return self._roster
        return await self._refresh()

    async def _refresh(self) -> Roster | None:
        """Share one in-flight read between every request that needs it."""
        loop = asyncio.get_running_loop()
        task = self._inflight
        if task is None or task.done() or task.get_loop() is not loop:
            task = loop.create_task(self._load())
            self._inflight = task
        # A caller that goes away must not cancel the read the others wait on.
        return await asyncio.shield(task)

    async def _load(self) -> Roster | None:
        started = self._clock()
        try:
            roster = await self._read()
        except Exception:  # noqa: BLE001
            roster = None
        finally:
            if self._inflight is asyncio.current_task():
                self._inflight = None
        self._roster, self._read_at, self._done_at = roster, started, self._clock()
        available = roster is not None
        if available != self._was_available:
            self._was_available = available
            if available:
                logger.info("peer_roster_available")
            else:
                logger.warning("peer_roster_unavailable mesh callers are refused until the mesh sidecar answers")
        return roster


_default: HouseholdRoster | None = None


def household_roster() -> HouseholdRoster:
    global _default
    if _default is None:
        _default = HouseholdRoster()
    return _default


def reset() -> None:
    """Forget the shared roster. For tests."""
    global _default
    _default = None
