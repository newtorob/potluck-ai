"""The desktop engine and this coordinator agree on the job body.

Each side passed its own unit tests while the engine's circle requests were
missing ``circle_id`` and failed validation here. The fixture holds the bodies
the engine builds (its suite pins that); this pins that ``JobRequest`` takes
them.
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from coordinator.routers.jobs import JobRequest

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "engine_job_bodies.json").read_text())


@pytest.mark.parametrize("name", sorted(FIXTURE["bodies"]))
def test_engine_job_body_is_a_valid_job_request(name: str) -> None:
    body = FIXTURE["bodies"][name]
    req = JobRequest.model_validate(body)
    assert req.scope == body["scope"]
    assert req.prompt == "follow-up"
    assert (req.messages is not None) == ("messages" in body)


def test_circle_body_names_its_circle() -> None:
    req = JobRequest.model_validate(FIXTURE["bodies"]["circle"])
    assert req.circle_id == uuid.UUID(FIXTURE["inputs"]["circle_id"])
    assert req.messages is not None and len(req.messages) == 4
