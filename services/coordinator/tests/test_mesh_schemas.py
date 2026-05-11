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
                last_seen_at=datetime.now(timezone.utc),
                platform="linux",
            )
        ],
    )
    dumped = resp.model_dump(mode="json")
    EnrollResponse.model_validate(dumped)  # round-trip, must not raise


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
