"""Schema-level tests for the mesh control plane.

These pin the on-the-wire shapes of `/mesh/*` endpoints so a future
schema drift can't accidentally break wire compatibility with the
desktop mesh sidecar.

Live-DB integration tests for the enroll/peers/heartbeat flow are in
`scripts/mesh-enroll-smoke.py` — they require a running coordinator
+ Postgres and aren't part of the pytest suite (which we want to keep
DB-free for fast inner-loop runs).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from coordinator.schemas.mesh import (
    DERPTokenRequest,
    DERPTokenResponse,
    EnrolledPeer,
    EnrollRequest,
    EnrollResponse,
    HeartbeatRequest,
    HeartbeatResponse,
    SignalEndpointUpdate,
    SignalError,
    SignalHello,
    SignalPing,
    SignalPong,
    SignalPunchCoord,
    SignalRequestPunch,
    SignalRosterUpdate,
    SignalWelcome,
)


# Fields the mesh-layer schemas are allowed to carry. Adding fields
# requires explicit acknowledgement (update _ALLOWED_FIELDS below).
# We do not include identifying fields like member_id, conversation_id,
# session_id — the mesh layer doesn't need them.
_ENROLL_REQUEST_FIELDS = frozenset(
    {"pairing_token", "wg_public_key", "node_name", "platform"}
)

# Substrings that, if they appear in a mesh-schema field name, suggest
# inference-layer identity is leaking down into the mesh layer (which
# would violate the two-layer privacy story: mesh sees household
# identity; inference dispatch is blinded).
_FORBIDDEN_SUBSTRINGS = (
    "conversation",
    "session",
    "prompt",
    "completion",
    "member_id",
)


def test_enroll_request_field_set_locked_down() -> None:
    """EnrollRequest carries exactly the routing-blind mesh-layer subset."""
    actual = set(EnrollRequest.model_fields.keys())
    extras = actual - _ENROLL_REQUEST_FIELDS
    missing = _ENROLL_REQUEST_FIELDS - actual
    assert not extras, (
        f"EnrollRequest gained unexpected fields: {sorted(extras)}. "
        "If intentional, update _ENROLL_REQUEST_FIELDS in this test."
    )
    assert not missing, (
        f"EnrollRequest lost expected fields: {sorted(missing)}."
    )


def test_no_inference_layer_identity_in_mesh_schemas() -> None:
    """No mesh-schema field name suggests inference-layer identity leak."""
    schemas = [
        EnrollRequest,
        EnrollResponse,
        EnrolledPeer,
        HeartbeatRequest,
        HeartbeatResponse,
    ]
    for schema in schemas:
        for field_name in schema.model_fields.keys():
            lowered = field_name.lower()
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"{schema.__name__}.{field_name} contains forbidden "
                    f"substring '{forbidden}'. The mesh layer must not carry "
                    "inference-layer identity. See docs/phase-g/mesh-design.md "
                    "§'Privacy posture'."
                )


def test_enroll_request_validates_token_length() -> None:
    """Pairing tokens must be 16-64 chars (matches the base32 generator)."""
    # Too short
    with pytest.raises(ValidationError):
        EnrollRequest(
            pairing_token="short",
            wg_public_key="A" * 44,
            node_name="laptop",
        )
    # Empty node name
    with pytest.raises(ValidationError):
        EnrollRequest(
            pairing_token="A" * 32,
            wg_public_key="A" * 44,
            node_name="",
        )
    # Happy
    req = EnrollRequest(
        pairing_token="A" * 32,
        wg_public_key="A" * 44,
        node_name="laptop",
        platform="darwin",
    )
    assert req.pairing_token == "A" * 32


def test_enroll_response_round_trip() -> None:
    """EnrollResponse serializes + deserializes cleanly."""
    resp = EnrollResponse(
        node_id=uuid.uuid4(),
        household_id=uuid.uuid4(),
        mesh_ip="100.64.0.2",
        mesh_cidr="100.64.0.0/24",
        peers=[
            EnrolledPeer(
                node_id=uuid.uuid4(),
                node_name="media-server",
                mesh_ip="100.64.0.3",
                wg_public_key="B" * 44,
                last_endpoint="203.0.113.1:51820",
                local_endpoints=["192.168.1.20:51820", "10.0.0.5:51820"],
                last_seen_at=datetime.now(timezone.utc),
                platform="linux",
            )
        ],
    )
    dumped = resp.model_dump(mode="json")
    parsed = EnrollResponse.model_validate(dumped)  # round-trip, must not raise
    # local_endpoints survives the round-trip intact (it's a roster field now).
    assert parsed.peers[0].local_endpoints == [
        "192.168.1.20:51820",
        "10.0.0.5:51820",
    ]


def test_heartbeat_request_minimal_shape() -> None:
    """HeartbeatRequest is small + carries no identity beyond node_id + key."""
    req = HeartbeatRequest(
        node_id=uuid.uuid4(),
        wg_public_key="C" * 44,
    )
    fields = set(req.model_dump().keys())
    # external_endpoint is allowed to be None; field exists either way
    assert fields == {"node_id", "wg_public_key", "external_endpoint"}


# ---------------------------------------------------------------------------
# LAN direct-path candidates (local_endpoints) — Phase 0/1 plumbing
# ---------------------------------------------------------------------------


def test_enrolled_peer_has_local_endpoints_field() -> None:
    """EnrolledPeer carries local_endpoints so LAN candidates flow through the
    roster. The field name is mesh-layer metadata (no inference identity), so
    the forbidden-substring invariant in test_no_inference_layer_identity_in_
    mesh_schemas still holds — this just pins the field's presence + default."""
    assert "local_endpoints" in EnrolledPeer.model_fields
    for forbidden in _FORBIDDEN_SUBSTRINGS:
        assert forbidden not in "local_endpoints"
    peer = EnrolledPeer(
        node_id=uuid.uuid4(),
        node_name="laptop",
        mesh_ip="100.64.0.2",
        wg_public_key="A" * 44,
    )
    assert peer.local_endpoints == []  # default empty, never None


def test_signal_endpoint_update_filters_non_private_endpoints() -> None:
    """The server-side validator drops anything that isn't a private ip:port:
    public, tailnet/CGNAT, loopback, link-local, IPv6, and malformed entries
    are all rejected (defense in depth — a client cannot get a tailnet or
    public address redistributed to peers)."""
    upd = SignalEndpointUpdate(
        external_endpoint="203.0.113.7:51820",
        local_endpoints=[
            "192.168.1.20:51820",      # keep — RFC1918
            "10.0.0.5:51820",          # keep — RFC1918
            "172.16.4.4:51820",        # keep — RFC1918
            "8.8.8.8:51820",           # drop — public
            "100.100.50.50:51820",     # drop — tailnet/CGNAT (100.64/10)
            "127.0.0.1:51820",         # drop — loopback
            "169.254.1.1:51820",       # drop — link-local
            "[fe80::1]:51820",         # drop — IPv6
            "192.168.1.20:51820",      # drop — duplicate
            "not-an-endpoint",         # drop — malformed (no port)
            "192.168.1.30:999999",     # drop — port out of range
        ],
    )
    assert upd.local_endpoints == [
        "192.168.1.20:51820",
        "10.0.0.5:51820",
        "172.16.4.4:51820",
    ]


def test_signal_endpoint_update_caps_local_endpoints() -> None:
    """The list is capped server-side so a node can't flood the roster."""
    many = [f"10.0.0.{i}:51820" for i in range(1, 20)]
    upd = SignalEndpointUpdate(external_endpoint="203.0.113.7:51820", local_endpoints=many)
    assert len(upd.local_endpoints) <= 4
    # All survivors are still valid private ip:port.
    for ep in upd.local_endpoints:
        assert ep.startswith("10.0.0.")


def test_signal_endpoint_update_defaults_empty() -> None:
    """Omitting local_endpoints yields an empty list, never None — so older
    clients that don't send the field still validate."""
    upd = SignalEndpointUpdate(external_endpoint="203.0.113.7:51820")
    assert upd.local_endpoints == []


# ---------------------------------------------------------------------------
# /mesh/signal protocol — signal messages
# ---------------------------------------------------------------------------


def test_signal_messages_carry_type_discriminator() -> None:
    """Every signal message has a `type` literal — clients use it as the
    discriminator to parse incoming traffic. If we ever drop it, peers
    can't tell messages apart."""
    classes = [
        SignalHello,
        SignalEndpointUpdate,
        SignalRequestPunch,
        SignalPing,
        SignalWelcome,
        SignalRosterUpdate,
        SignalPunchCoord,
        SignalPong,
        SignalError,
    ]
    for cls in classes:
        assert "type" in cls.model_fields, (
            f"{cls.__name__} lost its `type` discriminator field."
        )


def test_signal_messages_carry_no_inference_layer_identity() -> None:
    """Signal messages route mesh-layer concerns — endpoints, pubkeys,
    pairing — and must not carry inference-layer identifiers."""
    classes = [
        SignalHello,
        SignalEndpointUpdate,
        SignalRequestPunch,
        SignalPing,
        SignalWelcome,
        SignalRosterUpdate,
        SignalPunchCoord,
        SignalPong,
        SignalError,
    ]
    for cls in classes:
        for field_name in cls.model_fields.keys():
            lowered = field_name.lower()
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"{cls.__name__}.{field_name} contains forbidden "
                    f"substring '{forbidden}'."
                )


def test_signal_hello_validates_wg_key_length() -> None:
    """WG pubkeys are 32-128 chars in our wire encoding."""
    SignalHello(wg_public_key="A" * 44)  # happy
    # Too short
    from pydantic import ValidationError as VE
    try:
        SignalHello(wg_public_key="short")
        raise AssertionError("expected ValidationError")
    except VE:
        pass


def test_signal_punch_coord_role_locked_down() -> None:
    """`role` is a Literal — peers branch on it for simultaneous-send
    logic. Drift would silently break the hole-punch protocol."""
    msg = SignalPunchCoord(
        peer_node_id=uuid.uuid4(),
        peer_wg_public_key="A" * 44,
        peer_endpoint="1.2.3.4:51820",
        role="initiator",
    )
    assert msg.role == "initiator"
    # Wrong role string should fail validation.
    from pydantic import ValidationError as VE
    try:
        SignalPunchCoord(
            peer_node_id=uuid.uuid4(),
            peer_wg_public_key="A" * 44,
            peer_endpoint=None,
            role="other",  # type: ignore[arg-type]
        )
        raise AssertionError("expected ValidationError on invalid role")
    except VE:
        pass


# ---------------------------------------------------------------------------
# ip:port endpoint-string injection (LAUNCH-BLOCKER, coordinator half)
# ---------------------------------------------------------------------------
#
# HeartbeatRequest.external_endpoint, SignalEndpointUpdate.external_endpoint,
# and SignalPunchCoord.peer_endpoint all flow into the sidecar's WireGuard
# UAPI config on the receiving side. Before this fix they had only a
# max_length cap (or, for peer_endpoint, no cap at all) and no shape
# validation — a value like "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n" would have
# been accepted and relayed. These pin that a malformed value is REJECTED
# (raises ValidationError), not silently truncated or passed through.

_INJECTION_ENDPOINTS = [
    "1.1.1.1:1\nallowed_ip=0.0.0.0/0\n",  # newline-smuggled UAPI directive
    "1.1.1.1:51820 extra",  # embedded space
    "1.1.1.1:51820\r\nsomething",  # CRLF variant
    "not-an-endpoint",  # no port at all
    "1.1.1.1:",  # empty port
    "1.1.1.1:999999",  # port out of range
    "\n",  # bare newline
    "",  # empty string
]


def test_heartbeat_request_rejects_malformed_external_endpoint() -> None:
    for bad in _INJECTION_ENDPOINTS:
        with pytest.raises(ValidationError):
            HeartbeatRequest(
                node_id=uuid.uuid4(),
                wg_public_key="C" * 44,
                external_endpoint=bad,
            )


def test_heartbeat_request_accepts_well_formed_public_endpoint() -> None:
    req = HeartbeatRequest(
        node_id=uuid.uuid4(),
        wg_public_key="C" * 44,
        external_endpoint="203.0.113.7:51820",
    )
    assert req.external_endpoint == "203.0.113.7:51820"


def test_heartbeat_request_allows_none_external_endpoint() -> None:
    req = HeartbeatRequest(node_id=uuid.uuid4(), wg_public_key="C" * 44)
    assert req.external_endpoint is None


def test_signal_endpoint_update_rejects_malformed_external_endpoint() -> None:
    for bad in _INJECTION_ENDPOINTS:
        with pytest.raises(ValidationError):
            SignalEndpointUpdate(external_endpoint=bad)


def test_signal_punch_coord_rejects_malformed_peer_endpoint() -> None:
    """peer_endpoint previously had NO cap and no validation at all."""
    for bad in _INJECTION_ENDPOINTS:
        with pytest.raises(ValidationError):
            SignalPunchCoord(
                peer_node_id=uuid.uuid4(),
                peer_wg_public_key="A" * 44,
                peer_endpoint=bad,
                role="initiator",
            )


def test_signal_punch_coord_allows_none_peer_endpoint() -> None:
    msg = SignalPunchCoord(
        peer_node_id=uuid.uuid4(),
        peer_wg_public_key="A" * 44,
        peer_endpoint=None,
        role="responder",
    )
    assert msg.peer_endpoint is None


def test_signal_punch_coord_accepts_well_formed_public_endpoint() -> None:
    msg = SignalPunchCoord(
        peer_node_id=uuid.uuid4(),
        peer_wg_public_key="A" * 44,
        peer_endpoint="198.51.100.9:51820",
        role="initiator",
    )
    assert msg.peer_endpoint == "198.51.100.9:51820"


def test_signal_welcome_includes_roster() -> None:
    """SignalWelcome must include the roster — that's how new peers
    learn their household composition."""
    msg = SignalWelcome(
        node_id=uuid.uuid4(),
        household_id=uuid.uuid4(),
        mesh_ip="100.64.0.1",
        roster=[],
    )
    fields = set(msg.model_dump().keys())
    assert "roster" in fields


def test_signal_roster_update_three_lists() -> None:
    """SignalRosterUpdate has three independent lists: added / updated /
    removed. All optional, default empty."""
    msg = SignalRosterUpdate()
    assert msg.added == []
    assert msg.updated == []
    assert msg.removed == []


# ---------------------------------------------------------------------------
# DERP token schemas
# ---------------------------------------------------------------------------


# Expected field sets for DERP schemas. Adding any field here requires
# explicit review — especially guard against inference-layer identity leaking
# into the JWT request/response (which flows to the relay).
_DERP_TOKEN_REQUEST_FIELDS = frozenset({"wg_public_key", "region"})
_DERP_TOKEN_RESPONSE_FIELDS = frozenset({"token", "expires_at", "region"})


def test_derp_token_request_field_set_locked_down() -> None:
    """DERPTokenRequest carries only wg_public_key + region.

    Adding member_id, household_id, node_id, or any inference-layer field
    here would leak identity to the DERP relay (which only needs to know
    the WG pubkey + allowed region). This test pins the minimal field set.
    """
    actual = set(DERPTokenRequest.model_fields.keys())
    extras = actual - _DERP_TOKEN_REQUEST_FIELDS
    missing = _DERP_TOKEN_REQUEST_FIELDS - actual
    assert not extras, (
        f"DERPTokenRequest gained unexpected fields: {sorted(extras)}. "
        "Review carefully: the relay must not learn caller identity."
    )
    assert not missing, (
        f"DERPTokenRequest lost expected fields: {sorted(missing)}."
    )


def test_derp_token_response_field_set_locked_down() -> None:
    """DERPTokenResponse carries only token + expires_at + region.

    No identity beyond what the relay needs for auth. The JWT itself
    carries wg_pubkey + region + exp/iat — the relay does not need more.
    """
    actual = set(DERPTokenResponse.model_fields.keys())
    extras = actual - _DERP_TOKEN_RESPONSE_FIELDS
    missing = _DERP_TOKEN_RESPONSE_FIELDS - actual
    assert not extras, (
        f"DERPTokenResponse gained unexpected fields: {sorted(extras)}."
    )
    assert not missing, (
        f"DERPTokenResponse lost expected fields: {sorted(missing)}."
    )


def test_no_inference_layer_identity_in_derp_schemas() -> None:
    """DERP token schemas must not carry inference-layer identity."""
    schemas = [DERPTokenRequest, DERPTokenResponse]
    for schema in schemas:
        for field_name in schema.model_fields.keys():
            lowered = field_name.lower()
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"{schema.__name__}.{field_name} contains forbidden "
                    f"substring '{forbidden}'. DERP token schemas must not "
                    "carry inference-layer identity — the relay only needs "
                    "wg_pubkey + region + exp/iat."
                )


def test_derp_token_request_validates_key_length() -> None:
    """wg_public_key must be at least 32 chars (base64-encoded 32 bytes)."""
    # Happy path.
    req = DERPTokenRequest(wg_public_key="A" * 44, region="ord")
    assert req.region == "ord"

    # Default region is "ord".
    req2 = DERPTokenRequest(wg_public_key="A" * 44)
    assert req2.region == "ord"

    # Too-short key should fail.
    with pytest.raises(ValidationError):
        DERPTokenRequest(wg_public_key="short")


def test_derp_token_response_round_trip() -> None:
    """DERPTokenResponse serialises + deserialises cleanly."""
    resp = DERPTokenResponse(
        token="eyJhbGciOiJIUzI1NiJ9.stub",
        expires_at="2026-05-11T12:00:00+00:00",
        region="ord",
    )
    dumped = resp.model_dump(mode="json")
    DERPTokenResponse.model_validate(dumped)  # must not raise


def test_derp_token_request_region_defaults_to_ord() -> None:
    """Region defaults to 'ord' when omitted (single-region v0 launch)."""
    req = DERPTokenRequest(wg_public_key="A" * 44)
    assert req.region == "ord"
