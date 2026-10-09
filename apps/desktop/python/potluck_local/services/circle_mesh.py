"""What this machine's own mesh sidecar says about direct circle links.

A direct circle link is a restricted WireGuard peer that the mesh sidecar adds
on the coordinator's say-so (see services/circle_link.py for the borrowing
side and routers/circle_link.py for the lending side). The sidecar is the
authority on which links exist, so the engine reads two of its local endpoints
and trusts nothing else:

  GET /status        the machine's node id and the features the sidecar has.
                     "circle_link_v1" says the sidecar understands links and
                     that /circle-peers can be read.
  GET /circle-peers  one entry per circle peer. `inbound` is set when the peer
                     may call this machine (this machine lends to it) and
                     carries the SHA-256 of the link's token. `outbound` is
                     set when this machine may call the peer (it borrows).

The shapes are pinned to services/coordinator/tests/fixtures/
circle_link_shapes.json (sidecar.status_addition and sidecar.circle_peers).

Every parser here is strict and fails closed: a body that is not the expected
shape gives None, and an entry that is not the expected shape is dropped. A
dropped entry can only ever make a request refused, never allowed.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from . import mesh_ipc
from .peer_trust import IPAddress, parse_ip

logger = logging.getLogger(__name__)

# What a sidecar lists in /status "features" when it understands circle links.
LINK_FEATURE = "circle_link_v1"
# The sidecar answers in milliseconds on a local socket.
READ_TIMEOUT_S = 1.0
# The only scope a /circle-peers entry may have.
CIRCLE_SCOPE = "circle"
REACHABILITY_MODES = ("direct", "derp", "connecting")

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_FEATURES = 64
_MAX_PEERS = 256

_DEFAULT_MESH_CIDR = "100.64.0.0/10"


def mesh_network() -> ipaddress.IPv4Network | ipaddress.IPv6Network:
    """The range a lender's mesh address must be in. Same rule as the engine's
    household gate (POTLUCK_MESH_CIDR, default 100.64.0.0/10)."""
    raw = os.environ.get("POTLUCK_MESH_CIDR", "").strip() or _DEFAULT_MESH_CIDR
    try:
        return ipaddress.ip_network(raw, strict=False)
    except ValueError:
        return ipaddress.ip_network(_DEFAULT_MESH_CIDR)


def in_mesh(address: IPAddress | None) -> bool:
    if address is None:
        return False
    try:
        return address in mesh_network()
    except TypeError:  # address family differs from the range
        return False


def usable_listen_address(address: IPAddress | None) -> bool:
    """Whether the sidecar's reported mesh address is one the circle listener may
    bind: a plain IPv4 address in the mesh range. Never the unspecified address
    (that would bind every interface) or a multicast one."""
    return (
        address is not None
        and address.version == 4
        and in_mesh(address)
        and not address.is_unspecified
        and not address.is_multicast
    )


@dataclass(frozen=True)
class SidecarStatus:
    """The two things the engine needs from the sidecar's /status."""

    node_id: str | None  # canonical lowercase UUID text
    mesh_ip: IPAddress | None
    features: frozenset[str] = field(default_factory=frozenset)

    @property
    def supports_links(self) -> bool:
        return LINK_FEATURE in self.features and self.node_id is not None


@dataclass(frozen=True)
class Inbound:
    """This machine lends to the peer: the link, and the hash its token must have."""

    link_id: str
    port: int
    token_sha256: str
    expires_in_s: int | None


@dataclass(frozen=True)
class Outbound:
    """This machine borrows from the peer, on this port."""

    link_id: str
    port: int
    expires_in_s: int | None


@dataclass(frozen=True)
class CirclePeer:
    mesh_ip: IPAddress
    reachable: bool
    mode: str | None
    inbound: Inbound | None
    outbound: Outbound | None


def _uuid_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


def _port(value: Any) -> int | None:
    if type(value) is not int or not 1 <= value <= 65535:
        return None
    return value


def _seconds(value: Any) -> int | None:
    if type(value) is int and value >= 0:
        return value
    if type(value) is float and value >= 0:
        return int(value)
    return None


def parse_status(body: Any) -> SidecarStatus | None:
    """A SidecarStatus from the sidecar's /status body, or None when it is not a mapping."""
    if not isinstance(body, dict):
        return None
    raw_features = body.get("features")
    features: frozenset[str] = frozenset()
    if isinstance(raw_features, list):
        features = frozenset(f for f in raw_features[:_MAX_FEATURES] if isinstance(f, str))
    return SidecarStatus(
        node_id=_uuid_text(body.get("node_id")),
        mesh_ip=parse_ip(body.get("mesh_ip")),
        features=features,
    )


def _parse_inbound(value: Any) -> Inbound | None:
    if not isinstance(value, dict):
        return None
    link_id = _uuid_text(value.get("link_id"))
    port = _port(value.get("port"))
    digest = value.get("token_sha256")
    if link_id is None or port is None or not isinstance(digest, str):
        return None
    digest = digest.lower()
    if _SHA256_HEX.match(digest) is None:
        return None
    return Inbound(link_id, port, digest, _seconds(value.get("expires_in_s")))


def _parse_outbound(value: Any) -> Outbound | None:
    if not isinstance(value, dict):
        return None
    link_id = _uuid_text(value.get("link_id"))
    port = _port(value.get("port"))
    if link_id is None or port is None:
        return None
    return Outbound(link_id, port, _seconds(value.get("expires_in_s")))


def parse_circle_peers(body: Any) -> list[CirclePeer] | None:
    """The usable entries of the sidecar's /circle-peers body, or None when the
    body is not the expected shape.

    An entry is dropped unless it is a mapping with scope exactly "circle" and a
    parseable address. Two entries for one address are both dropped: the sidecar
    holds one entry per peer, so a second one means something is wrong, and a
    wrong answer must not authorise anything.
    """
    if not isinstance(body, dict) or not isinstance(body.get("peers"), list):
        return None
    peers: dict[IPAddress, CirclePeer] = {}
    conflicted: set[IPAddress] = set()
    for entry in body["peers"][:_MAX_PEERS]:
        if not isinstance(entry, dict) or entry.get("scope") != CIRCLE_SCOPE:
            continue
        ip = parse_ip(entry.get("mesh_ip"))
        if ip is None:
            continue
        if ip in peers or ip in conflicted:
            conflicted.add(ip)
            peers.pop(ip, None)
            continue
        mode = entry.get("reachability_mode")
        peers[ip] = CirclePeer(
            mesh_ip=ip,
            reachable=entry.get("reachable") is True,
            mode=mode if mode in REACHABILITY_MODES else None,
            inbound=_parse_inbound(entry.get("inbound")),
            outbound=_parse_outbound(entry.get("outbound")),
        )
    return list(peers.values())


async def read_status() -> SidecarStatus | None:
    """One read of the sidecar's /status. None when it cannot be read."""
    try:
        return parse_status(await mesh_ipc.mesh_get("/status", timeout=READ_TIMEOUT_S))
    except Exception:  # noqa: BLE001 - every failure means "nothing is known"
        return None


async def read_circle_peers() -> list[CirclePeer] | None:
    """One read of /circle-peers. None when the sidecar is down, too old to have
    the endpoint, or answers with something that is not the expected shape."""
    try:
        return parse_circle_peers(await mesh_ipc.mesh_get("/circle-peers", timeout=READ_TIMEOUT_S))
    except Exception:  # noqa: BLE001
        return None
