"""Privacy-invariant tests for the contributor-network protocol.

These pin the architectural rule from manifesto §10.3 (memory 42a7e671):
the coordinator must NEVER forward member-identifying information to a
contributor peer. The enforcement mechanism is structural — the
`JobAssignedMessage` schema simply has no field for it — and these tests
codify that so a future schema change can't accidentally re-introduce
the leak without breaking CI.

If one of these fails, you're either (a) intentionally redesigning
blinded routing (in which case update the manifesto + memory 42a7e671
first, then loosen the test) or (b) about to ship a privacy bug. There
is no third option.
"""
from __future__ import annotations

import uuid

from coordinator.schemas.peer import ChatMessage, JobAssignedMessage, SamplingParams

# Fields that JobAssignedMessage is allowed to carry. Anything outside
# this set is a privacy regression — review the schema diff carefully.
#
# `messages` was added for multi-turn pool/circle jobs (2026-06-29). It
# carries ChatMessage(role, content) turns only — no requester identity.
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

    Adding a field requires updating _ALLOWED_FIELDS. That is the point —
    every addition forces a code review on a privacy-critical surface."""
    actual = set(JobAssignedMessage.model_fields.keys())
    extras = actual - _ALLOWED_FIELDS
    missing = _ALLOWED_FIELDS - actual
    assert not extras, (
        f"JobAssignedMessage gained unexpected fields: {sorted(extras)}. "
        "If this is intentional, update _ALLOWED_FIELDS in this test AND "
        "update memory 42a7e671 / manifesto §10.3 to reflect the new "
        "privacy posture."
    )
    assert not missing, (
        f"JobAssignedMessage lost expected fields: {sorted(missing)}. "
        "Update _ALLOWED_FIELDS if the schema was intentionally simplified."
    )


def test_job_assigned_serialised_payload_has_no_identifying_keys() -> None:
    """A serialised JobAssignedMessage carries no identifying keys.

    Belt-and-suspenders for the structural test — catches the case where
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
        "Routing is supposed to be blinded — peers must never see who the "
        "requester is."
    )


# ---------------------------------------------------------------------------
# Multi-turn (messages) wire contract
# ---------------------------------------------------------------------------


def test_chat_message_field_set_is_role_and_content_only() -> None:
    """ChatMessage carries ONLY role + content — no id, timestamp, or metadata.

    The moment a metadata field is added (e.g. turn_id, created_at) a peer
    can start correlating turns across jobs. The field set is pinned here."""
    actual = set(ChatMessage.model_fields.keys())
    assert actual == {"role", "content"}, (
        f"ChatMessage field set changed: {sorted(actual)}. "
        "ChatMessage must have ONLY role + content — no metadata that could "
        "let a peer correlate turns across jobs or identify the requester."
    )


def test_job_assigned_messages_none_by_default() -> None:
    """Backward-compat: a prompt-only JobAssignedMessage has messages=None.

    Peers running older code that don't understand messages receive None and
    behave exactly as before. This test pins that the default never changes."""
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

    # Without messages — backward-compat
    req_without = JobRequest(prompt="hello")
    assert req_without.messages is None


def test_job_request_messages_default_is_none() -> None:
    """Prompt-only JobRequest has messages=None — identical behavior to today."""
    from coordinator.routers.jobs import JobRequest

    req = JobRequest(prompt="single turn prompt")
    assert req.messages is None
    assert req.prompt == "single turn prompt"
