"""Schema-level tests for the credit-ledger API surface.

Pins the on-the-wire shape of `/me/credit-balance` + `/me/credit-history`
so future schema drift can't accidentally leak inference-layer identity
(prompt content, conversation IDs, etc.) down into the per-member
endpoints, or accidentally remove the hash-chain fields that members
rely on to audit their own history.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from coordinator.schemas.credit import (
    CreditBalanceByModel,
    CreditBalanceResponse,
    CreditHistoryEntry,
    CreditHistoryResponse,
)

# Fields the per-member credit response surface is allowed to carry.
_BALANCE_FIELDS = frozenset({"member_id", "earned", "spent", "net", "by_model"})
_BALANCE_BY_MODEL_FIELDS = frozenset({"model_slug", "earned", "spent"})
_HISTORY_ENTRY_FIELDS = frozenset(
    {
        "id",
        "direction",
        "job_id",
        "model_slug",
        "output_tokens",
        "multiplier",
        "credits",
        "prev_row_hash",
        "row_hash",
        "created_at",
    }
)

# Substrings that, if they appear in any credit-response field name,
# indicate inference-layer identity is leaking into the ledger surface.
# Member_id is explicitly allowed — the ledger has to know which member
# a row belongs to. But conversation_id, prompt, response, etc. must not
# appear (they're inference-layer concepts the ledger doesn't touch).
_FORBIDDEN_SUBSTRINGS = (
    "conversation",
    "prompt",
    "response",
    "completion",
    "content",
    "session",
)


def test_balance_response_fields_locked_down() -> None:
    actual = set(CreditBalanceResponse.model_fields.keys())
    assert actual == _BALANCE_FIELDS, (
        f"CreditBalanceResponse fields drifted. Got {sorted(actual)}, "
        f"expected {sorted(_BALANCE_FIELDS)}. Update this test if the "
        "drift is intentional."
    )


def test_balance_by_model_fields_locked_down() -> None:
    actual = set(CreditBalanceByModel.model_fields.keys())
    assert actual == _BALANCE_BY_MODEL_FIELDS, (
        f"CreditBalanceByModel fields drifted. Got {sorted(actual)}, "
        f"expected {sorted(_BALANCE_BY_MODEL_FIELDS)}."
    )


def test_history_entry_includes_hash_chain_fields() -> None:
    """Hash-chain fields must be in the history response — they're the
    member's audit mechanism. See docs/security/threat-model.md §6."""
    actual = set(CreditHistoryEntry.model_fields.keys())
    assert actual == _HISTORY_ENTRY_FIELDS, (
        f"CreditHistoryEntry fields drifted. Got {sorted(actual)}, "
        f"expected {sorted(_HISTORY_ENTRY_FIELDS)}."
    )
    # Specifically verify the chain fields are present.
    assert "prev_row_hash" in actual, "hash chain prev_row_hash missing"
    assert "row_hash" in actual, "hash chain row_hash missing"


def test_no_inference_layer_identity_in_credit_schemas() -> None:
    """No credit-schema field name suggests inference-layer identity leak."""
    schemas = [
        CreditBalanceResponse,
        CreditBalanceByModel,
        CreditHistoryEntry,
        CreditHistoryResponse,
    ]
    for schema in schemas:
        for field_name in schema.model_fields.keys():
            lowered = field_name.lower()
            for forbidden in _FORBIDDEN_SUBSTRINGS:
                assert forbidden not in lowered, (
                    f"{schema.__name__}.{field_name} contains forbidden "
                    f"substring '{forbidden}'. The credit ledger must not "
                    "carry inference-layer identity. See "
                    "docs/security/threat-model.md."
                )


def test_balance_response_round_trip() -> None:
    """Round-trip serialization works for the typical balance shape."""
    resp = CreditBalanceResponse(
        member_id=uuid.uuid4(),
        earned=Decimal("13.50"),
        spent=Decimal("9.00"),
        net=Decimal("4.50"),
        by_model=[
            CreditBalanceByModel(
                model_slug="qwen2.5:3b-instruct-q4_K_M",
                earned=Decimal("13.50"),
                spent=Decimal("9.00"),
            )
        ],
    )
    CreditBalanceResponse.model_validate(resp.model_dump(mode="json"))


def test_history_entry_round_trip() -> None:
    entry = CreditHistoryEntry(
        id=uuid.uuid4(),
        direction="earned",
        job_id=uuid.uuid4(),
        model_slug="qwen2.5:3b-instruct-q4_K_M",
        output_tokens=9,
        multiplier=Decimal("1.50"),
        credits=Decimal("13.50"),
        prev_row_hash="0fe48b895fadfa7c218c879486a1bac3feb26ee6e53c1a3c1b1e0da8607c65c0",
        row_hash="1fd4057ac3550198542ce1e314ac1ef7ba06e564429483f9c173852ad4ff7076",
        created_at=datetime.now(timezone.utc),
    )
    CreditHistoryEntry.model_validate(entry.model_dump(mode="json"))
