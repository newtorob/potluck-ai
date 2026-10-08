"""The circle responses the desktop app reads still have the fields it reads.

The app's screens were built and tested against a stand-in for this service.
The fixture is the meeting point: each response model here must have exactly
the fixture's fields (so adding or dropping one forces the fixture, and with
it the app's own contract test, to change), and must accept the example.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator.routers.circles import (
    CircleAvailabilityOut,
    CircleMemberOut,
    CircleModelOut,
    CircleOut,
    CirclePersonOut,
    InviteCreatedOut,
    InviteOut,
    InvitePreviewOut,
)

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "circle_api_shapes.json").read_text())


@pytest.mark.parametrize("name,model,nested", [
    ("circle", CircleOut, {"people": CirclePersonOut, "members": CircleMemberOut}),
    ("invite_created", InviteCreatedOut, {}),
    ("invite", InviteOut, {}),
    ("invite_preview", InvitePreviewOut, {}),
    ("availability", CircleAvailabilityOut, {"models": CircleModelOut}),
])
def test_response_models_match_what_the_app_reads(name, model, nested) -> None:
    example = FIXTURE[name]
    assert set(model.model_fields) == set(example), name
    for field, item_model in nested.items():
        assert example[field], f"the {name} example needs at least one {field} entry"
        for item in example[field]:
            assert set(item_model.model_fields) == set(item), f"{name}.{field}"
    # The example round-trips through the model unchanged in its fields.
    assert set(model.model_validate(example).model_dump(mode="json")) == set(example)
