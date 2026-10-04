"""M2.5 hardening before the coordinator ships it.

Self-reported throughput steers routing, so it has to be finite and bounded;
a present-but-invalid session bearer must not quietly fall back to the
shared token; and production keeps refusing unregistered contributor builds.
"""
from __future__ import annotations

import math
import tomllib
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from coordinator.schemas.peer import (
    MAX_PROMPT_TOKENS_PER_SECOND,
    MAX_TOKENS_PER_SECOND,
    CapabilityEntry,
)
from coordinator.services.peer_registry import TPS_SCORE_CAP, measured_compute_score

from tests.test_contributor_m25 import _make_app


@pytest.mark.parametrize("field", ["tokens_per_second", "prompt_tokens_per_second"])
@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_non_finite_throughput_is_rejected(field, value):
    with pytest.raises(ValidationError):
        CapabilityEntry(model_slug="m", **{field: value})


def test_absurd_throughput_is_clamped_not_trusted():
    entry = CapabilityEntry(model_slug="m", tokens_per_second=1e12, prompt_tokens_per_second=1e12)
    assert entry.tokens_per_second == MAX_TOKENS_PER_SECOND
    assert entry.prompt_tokens_per_second == MAX_PROMPT_TOKENS_PER_SECOND
    honest = CapabilityEntry(model_slug="m", tokens_per_second=41.5, prompt_tokens_per_second=380.0)
    assert (honest.tokens_per_second, honest.prompt_tokens_per_second) == (41.5, 380.0)


def test_claimed_speed_has_a_bounded_routing_advantage():
    assert measured_compute_score(40.0) == 40.0
    assert measured_compute_score(MAX_TOKENS_PER_SECOND) == TPS_SCORE_CAP
    assert measured_compute_score(1e9) == TPS_SCORE_CAP
    assert TPS_SCORE_CAP < MAX_TOKENS_PER_SECOND


def test_invalid_bearer_never_falls_back_to_the_shared_token(monkeypatch):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "real-shared-token-0123456789")
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        with client.websocket_connect("/peers/connect", headers={
            "Authorization": "Bearer expired-or-forged",
            "X-Potluck-Coordinator-Token": "real-shared-token-0123456789",
        }) as ws:
            assert ws.receive_json() == {"type": "reject", "reason": "unauthorized: invalid session"}


def test_bearer_is_ignored_when_session_auth_is_off(monkeypatch):
    """Local compose runs without session auth; the in-app contributor still sends its bearer."""
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    with TestClient(_make_app(), raise_server_exceptions=False) as client:
        with client.websocket_connect("/peers/connect", headers={"Authorization": "Bearer local-dev"}) as ws:
            assert ws.receive_json()["type"] == "challenge"


def test_production_refuses_unregistered_contributor_builds():
    fly = tomllib.loads((Path(__file__).resolve().parents[1] / "fly.toml").read_text())
    assert fly["env"]["REQUIRE_REGISTERED_RELEASE"] == "true"
