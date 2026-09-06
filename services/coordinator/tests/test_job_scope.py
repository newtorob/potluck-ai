"""Reject ambiguous circle requests before any peer can receive a prompt."""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from coordinator.db.database import get_db
from coordinator.middleware.auth import require_coordinator_token
from coordinator.middleware.session_auth import resolve_session_member_id
from coordinator.routers import jobs
from coordinator.services import peer_registry

_CIRCLE_ID = '11111111-1111-4111-8111-111111111111'


@pytest.mark.parametrize('path', ['/jobs', '/jobs/stream'])
@pytest.mark.parametrize('routing', [
    {'circle_id': _CIRCLE_ID},  # The previously leaking desktop request.
    {'scope': 'pool', 'circle_id': _CIRCLE_ID},
    {'scope': 'mine', 'circle_id': _CIRCLE_ID},
    {'scope': 'circle'},
    {'scope': 'circle', 'circle_id': None},
    {'scope': 'circle', 'circle_id': ''},
])
def test_inconsistent_circle_request_is_rejected_before_dispatch(monkeypatch, path, routing):
    app = FastAPI()
    app.include_router(jobs.router, prefix='/jobs')
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[resolve_session_member_id] = uuid.uuid4
    app.dependency_overrides[require_coordinator_token] = lambda: None
    dispatch = AsyncMock(side_effect=AssertionError('invalid request reached dispatch'))
    monkeypatch.setattr(jobs, '_dispatch_to_peer', dispatch)

    with TestClient(app) as client:
        response = client.post(path, json={'prompt': 'circle-only prompt', **routing})

    assert response.status_code == 422
    dispatch.assert_not_awaited()


@pytest.mark.parametrize('routing, expected_scope', [
    ({'scope': 'circle', 'circle_id': _CIRCLE_ID}, 'circle'),
    ({'scope': 'pool'}, 'pool'),
    ({'scope': 'mine'}, 'mine'),
    ({}, 'pool'),  # Legacy pool callers without circle intent stay supported.
])
def test_consistent_job_scopes_remain_valid(routing, expected_scope):
    request = jobs.JobRequest.model_validate({'prompt': 'test', **routing})
    assert request.scope == expected_scope


@pytest.mark.parametrize('pubkeys', [None, set()])
async def test_circle_selection_without_members_never_uses_unfiltered_picker(monkeypatch, pubkeys):
    picker = AsyncMock(return_value=object())
    monkeypatch.setattr(peer_registry, 'pick_peer_for_model', picker)

    result = await peer_registry.select_peer_for_request(
        None, model_slug='test-model', requester_member_id=uuid.uuid4(),
        scope='circle', circle_pubkeys=pubkeys,
    )

    assert result == (None, None)
    picker.assert_not_awaited()
