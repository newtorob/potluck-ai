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

from coordinator.schemas.peer import JobAssignedMessage, SamplingParams

# Fields that JobAssignedMessage is allowed to carry. Anything outside
# this set is a privacy regression — review the schema diff carefully.
_ALLOWED_FIELDS: frozenset[str] = frozenset(
    {"type", "job_id", "model_slug", "prompt", "sampling"}
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
