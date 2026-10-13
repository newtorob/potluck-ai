"""Privacy-invariant tests for the contributor-network protocol.

These pin the architectural rule that the coordinator must never forward
member-identifying information to a contributor peer. The enforcement
mechanism is structural - the `JobAssignedMessage` schema simply has no
field for it - and these tests codify that so a future schema change can't
accidentally re-introduce the leak without breaking CI.

If one of these fails, you're either (a) intentionally redesigning
blinded routing (in which case update the privacy documentation first,
then loosen the test) or (b) about to ship a privacy bug. There is no
third option.
"""
from __future__ import annotations

import uuid

from coordinator.schemas.peer import ChatMessage, JobAssignedMessage, SamplingParams

# Fields that JobAssignedMessage is allowed to carry. Anything outside
# this set is a privacy regression - review the schema diff carefully.
#
# `messages` supports multi-turn pool/circle jobs. It
# carries ChatMessage(role, content) turns only - no requester identity.
# ChatMessage has no id / timestamp / metadata fields; the invariant holds.
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"type", "job_id", "model_slug", "prompt", "messages", "sampling"}
)

# Substrings that, if they appear as a key anywhere in a serialised
# JobAssignedMessage, almost certainly indicate a leak. These are
# heuristics; the structural assertion above is the real guarantee.
_FORBIDDEN_KEY_SUBSTRINGS: tuple[str, ...] = (
    "member",
    "conversation",
    "session",
    "user_id",
    "requester",
    "account",
)


def test_job_assigned_field_set_is_locked_down() -> None:
    """JobAssignedMessage carries only the routing-blind subset.

    Adding a field requires updating _ALLOWED_FIELDS. That is the point -
    every addition forces a code review on a privacy-critical surface."""
    actual = set(JobAssignedMessage.model_fields.keys())
    extras = actual - _ALLOWED_FIELDS
    missing = _ALLOWED_FIELDS - actual
    assert not extras, (
        f"JobAssignedMessage gained unexpected fields: {sorted(extras)}. "
        "If this is intentional, update _ALLOWED_FIELDS in this test AND "
        "update the privacy documentation to reflect the new "
        "privacy posture."
    )
    assert not missing, (
        f"JobAssignedMessage lost expected fields: {sorted(missing)}. "
        "Update _ALLOWED_FIELDS if the schema was intentionally simplified."
    )


def test_job_assigned_serialised_payload_has_no_identifying_keys() -> None:
    """A serialised JobAssignedMessage carries no identifying keys.

    Belt-and-suspenders for the structural test - catches the case where
    somebody adds a field with a benign-looking name but that smuggles
    member identity (e.g. ``ctx.requester_uuid``)."""
    msg = JobAssignedMessage(
        job_id=uuid.uuid4(),
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        prompt="hello",
        sampling=SamplingParams(),
    )
    payload = msg.model_dump(mode="json")

    def _walk_keys(obj: object) -> list[str]:
        keys: list[str] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.append(str(k))
                keys.extend(_walk_keys(v))
        elif isinstance(obj, list):
            for item in obj:
                keys.extend(_walk_keys(item))
        return keys

    all_keys = [k.lower() for k in _walk_keys(payload)]
    leaks = [
        k for k in all_keys if any(forbidden in k for forbidden in _FORBIDDEN_KEY_SUBSTRINGS)
    ]
    assert not leaks, (
        f"JobAssignedMessage payload contains identifier-shaped keys: {leaks}. "
        f"Forbidden substrings: {_FORBIDDEN_KEY_SUBSTRINGS}. "
        "Routing is supposed to be blinded - peers must never see who the "
        "requester is."
    )


# ---------------------------------------------------------------------------
# Multi-turn (messages) wire contract
# ---------------------------------------------------------------------------


def test_chat_message_field_set_is_role_and_content_only() -> None:
    """ChatMessage carries only role + content - no id, timestamp, or metadata.

    The moment a metadata field is added (e.g. turn_id, created_at) a peer
    can start correlating turns across jobs. The field set is pinned here."""
    actual = set(ChatMessage.model_fields.keys())
    assert actual == {"role", "content"}, (
        f"ChatMessage field set changed: {sorted(actual)}. "
        "ChatMessage must have only role + content - no metadata that could "
        "let a peer correlate turns across jobs or identify the requester."
    )


def test_job_assigned_messages_none_by_default() -> None:
    """Backward-compat: a prompt-only JobAssignedMessage has messages=None.

    Peers that don't understand messages receive None and
    behave as for a single-turn prompt. This test pins that the default never changes."""
    msg = JobAssignedMessage(
        job_id=uuid.uuid4(),
        model_slug="stub-echo",
        prompt="hello",
        sampling=SamplingParams(),
    )
    assert msg.messages is None


def test_job_assigned_carries_messages_through() -> None:
    """JobAssignedMessage round-trips a messages array cleanly.

    Verifies the coordinator correctly relays multi-turn history and that the
    serialized form has no identifying keys beyond role + content."""
    turns = [
        ChatMessage(role="user", content="What is 2+2?"),
        ChatMessage(role="assistant", content="4"),
        ChatMessage(role="user", content="Are you sure?"),
    ]
    msg = JobAssignedMessage(
        job_id=uuid.uuid4(),
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        prompt="Are you sure?",
        messages=turns,
        sampling=SamplingParams(),
    )
    payload = msg.model_dump(mode="json")
    assert payload["messages"] is not None
    assert len(payload["messages"]) == 3
    assert payload["messages"][0] == {"role": "user", "content": "What is 2+2?"}
    assert payload["messages"][2] == {"role": "user", "content": "Are you sure?"}
    # Serialized keys must still be free of identifying substrings
    def _walk_keys(obj: object) -> list[str]:
        keys: list[str] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                keys.append(str(k))
                keys.extend(_walk_keys(v))
        elif isinstance(obj, list):
            for item in obj:
                keys.extend(_walk_keys(item))
        return keys

    all_keys = [k.lower() for k in _walk_keys(payload)]
    leaks = [k for k in all_keys if any(f in k for f in _FORBIDDEN_KEY_SUBSTRINGS)]
    assert not leaks, (
        f"Multi-turn JobAssignedMessage payload contains identifier-shaped keys: {leaks}."
    )


def test_job_request_accepts_messages_array() -> None:
    """JobRequest parses a messages array and exposes it as ChatMessage objects.

    The coordinator is policy-neutral: it relays whatever the sidecar sends.
    This test verifies the schema parses correctly and backward-compat holds
    (omitting messages yields None, not a validation error)."""
    from coordinator.routers.jobs import JobRequest

    # With messages
    req_with = JobRequest(
        prompt="Are you sure?",
        messages=[
            {"role": "user", "content": "What is 2+2?"},
            {"role": "assistant", "content": "4"},
            {"role": "user", "content": "Are you sure?"},
        ],
    )
    assert req_with.messages is not None
    assert len(req_with.messages) == 3
    assert req_with.messages[0].role == "user"
    assert req_with.messages[0].content == "What is 2+2?"

    # Without messages - backward-compat
    req_without = JobRequest(prompt="hello")
    assert req_without.messages is None


def test_job_request_messages_default_is_none() -> None:
    """Prompt-only JobRequest has messages=None - identical single-turn behavior."""
    from coordinator.routers.jobs import JobRequest

    req = JobRequest(prompt="single turn prompt")
    assert req.messages is None
    assert req.prompt == "single turn prompt"


# ---------------------------------------------------------------------------
# Direct circle links: what a sidecar is told about the machine at the other end
# ---------------------------------------------------------------------------
#
# A link joins two households that are not each other's. The messages that set
# it up are sent to both sidecars, and each must learn only what its tunnel
# needs about the other machine: its WireGuard key, mesh address, the port, a
# hash of the token, and (under the direct transport policy) its endpoints. No
# member id, node id, node name, hostname or circle name, ever. As with
# JobAssignedMessage, a new field forces a review of this list.

_CIRCLE_PEER_ALLOWED_FIELDS: dict[str, frozenset[str]] = {
    "SignalCirclePeerAdd": frozenset({
        "type", "link_id", "role", "peer_wg_public_key", "peer_mesh_ip", "port", "ttl_s",
        "token_sha256", "peer_endpoint", "peer_local_endpoints", "direct_window_ms",
    }),
    "SignalCirclePeerExtend": frozenset({"type", "link_id", "ttl_s", "peer_endpoint"}),
    "SignalCirclePeerRemove": frozenset({"type", "link_id", "reason"}),
    "SignalCirclePeerAck": frozenset({"type", "link_id", "op", "state", "reason"}),
}

_CIRCLE_PEER_FORBIDDEN_KEY_SUBSTRINGS: tuple[str, ...] = (
    "member", "node_id", "node_name", "hostname", "host_name", "circle", "name", "household",
    "account", "session", "user", "peer_id", "conversation", "email",
)


def test_circle_peer_message_field_sets_are_locked_down() -> None:
    from coordinator.schemas import mesh as mesh_schemas

    for cls_name, allowed in _CIRCLE_PEER_ALLOWED_FIELDS.items():
        actual = set(getattr(mesh_schemas, cls_name).model_fields)
        assert actual == allowed, (
            f"{cls_name} fields changed: extra={sorted(actual - allowed)} "
            f"missing={sorted(allowed - actual)}. These messages cross households; "
            "if the change is intended, update _CIRCLE_PEER_ALLOWED_FIELDS and the fixture "
            "(tests/fixtures/circle_link_shapes.json) together, and say why in the review."
        )


def test_circle_peer_messages_have_no_identifying_keys() -> None:
    from coordinator.schemas import mesh as mesh_schemas

    for cls_name in _CIRCLE_PEER_ALLOWED_FIELDS:
        keys = [k.lower() for k in getattr(mesh_schemas, cls_name).model_fields]
        leaks = [k for k in keys if any(f in k for f in _CIRCLE_PEER_FORBIDDEN_KEY_SUBSTRINGS)]
        assert not leaks, f"{cls_name} has identifier-shaped fields: {leaks}"


def test_a_serialised_circle_peer_add_has_no_identifying_keys_or_values() -> None:
    from coordinator.schemas.mesh import SignalCirclePeerAdd

    msg = SignalCirclePeerAdd(
        link_id=uuid.uuid4(), role="lender",
        peer_wg_public_key="AAECAwQFBgcICQoLDA0ODxAREhMUFRYXGBkaGxwdHh8=",
        peer_mesh_ip="100.64.0.7", port=8340, ttl_s=600, token_sha256="a" * 64,
        peer_endpoint="198.51.100.4:51820", peer_local_endpoints=["192.168.1.2:51820"],
        direct_window_ms=3000,
    ).model_dump(mode="json")
    keys = [k.lower() for k in msg]
    assert not [k for k in keys if any(f in k for f in _CIRCLE_PEER_FORBIDDEN_KEY_SUBSTRINGS)]
    # The only UUID on the wire is the link's own.
    uuids = [v for v in msg.values() if isinstance(v, str) and len(v) == 36 and v.count("-") == 4]
    assert uuids == [msg["link_id"]]


def test_the_link_http_answer_names_the_lender_only_by_where_to_connect() -> None:
    from coordinator.schemas.circle_link import (
        CircleLinkCreateResponse,
        CircleLinkLender,
        CircleLinkRenewResponse,
    )

    assert set(CircleLinkLender.model_fields) == {"mesh_ip", "port"}
    assert set(CircleLinkCreateResponse.model_fields) == {
        "link_id", "circle_id", "model_slug", "token", "lender", "ttl_s", "renew_after_s",
        "expires_at", "ready",
    }
    assert set(CircleLinkRenewResponse.model_fields) == {
        "link_id", "ttl_s", "renew_after_s", "expires_at", "delivered",
    }


def test_link_messages_go_out_only_through_the_mesh_signal_send_path() -> None:
    """lint #20 inspects `<expr>.ws.send_*()` calls, so a new send could slip
    past it by another route. The link service never writes to a socket itself:
    every message goes through routers.mesh.signal_send_link, which uses the same
    _safe_send the rest of the mesh signal code does and refuses a connection
    that was not opened with the node's own token (tests/test_circle_link_node_token.py)."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "coordinator"
    for rel in ("services/circle_links.py", "routers/circle_links.py", "schemas/circle_link.py"):
        tree = ast.parse((root / rel).read_text(encoding="utf-8"))
        writes = [
            n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in {"send", "send_json", "send_text", "send_bytes"}
        ]
        assert not writes, f"{rel} writes to a socket directly: {writes}"
    service = (root / "services/circle_links.py").read_text(encoding="utf-8")
    assert "signal_send_link" in service
