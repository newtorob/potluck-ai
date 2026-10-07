"""How the engine addresses, authenticates and scopes its coordinator calls.

Covers the default coordinator, the member sign-in on job requests, circle
selection, and the job-body contract shared with the coordinator suite
(services/coordinator/tests/fixtures/engine_job_bodies.json).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from potluck_local.services import pool_routing as pr

FIXTURE = json.loads(
    (Path(__file__).resolve().parents[4] / "services" / "coordinator" / "tests"
     / "fixtures" / "engine_job_bodies.json").read_text()
)
CIRCLE_A = "7f0c5a2e-58a1-4b0c-9a51-0f5e3a6f1d11"
CIRCLE_B = "0b0d7f0a-1f5d-4a39-8a65-2b0e2a9d6c22"
_REAL_CLIENT = httpx.AsyncClient


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fresh_circle_cache():
    pr._circle_cache.clear()
    yield
    pr._circle_cache.clear()


# ---- the job body both sides agree on ---------------------------------------


@pytest.mark.parametrize("name", sorted(FIXTURE["bodies"]))
def test_job_body_matches_the_shared_contract(name: str) -> None:
    i = FIXTURE["inputs"]
    scope = "circle" if name == "circle" else "pool"
    with patch.object(pr, "_pool_multiturn_enabled", return_value=name == "pool_multiturn"):
        body = pr._job_body(i["messages"], i["model"], i["temperature"], i["max_tokens"], scope,
                            i["circle_id"] if scope == "circle" else None)
    assert body == FIXTURE["bodies"][name]


# ---- where requests go ------------------------------------------------------


def test_default_install_uses_the_public_coordinator(monkeypatch) -> None:
    monkeypatch.delenv("POTLUCK_COORDINATOR_HTTP_URL", raising=False)
    monkeypatch.delenv("POTLUCK_COORDINATOR_URL", raising=False)
    monkeypatch.setattr(pr, "_settings_coordinator_url", lambda: None)
    assert pr.coordinator_http_base() == "https://coordinator.trypotluck.ai"


def test_settings_and_env_still_win_over_the_default(monkeypatch) -> None:
    monkeypatch.delenv("POTLUCK_COORDINATOR_HTTP_URL", raising=False)
    monkeypatch.setattr(pr, "_settings_coordinator_url", lambda: "http://10.0.0.5:8400/")
    assert pr.coordinator_http_base() == "http://10.0.0.5:8400"
    monkeypatch.setenv("POTLUCK_COORDINATOR_HTTP_URL", "http://127.0.0.1:8400")
    assert pr.coordinator_http_base() == "http://127.0.0.1:8400"


# ---- who the request is from ------------------------------------------------


def test_member_requests_carry_the_session(monkeypatch) -> None:
    monkeypatch.setattr(pr, "_member_session_token", lambda: "sess-abc")
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    h = pr._headers("https://coordinator.example")
    assert h["Authorization"] == "Bearer sess-abc"
    assert "X-Potluck-Coordinator-Token" not in h


def test_public_reads_carry_no_session(monkeypatch) -> None:
    monkeypatch.setattr(pr, "_member_session_token", lambda: "sess-abc")
    assert "Authorization" not in pr._headers()


@pytest.mark.parametrize("base,allowed", [
    ("https://coordinator.example", True),
    ("http://127.0.0.1:8400", True),
    ("http://192.168.1.20:8400", True),
    ("http://100.64.0.1:8400", True),
    ("http://coordinator.example", False),
    ("http://8.8.8.8:8400", False),
])
def test_session_never_travels_in_the_clear_to_a_public_host(monkeypatch, base, allowed) -> None:
    monkeypatch.setattr(pr, "_member_session_token", lambda: "sess-abc")
    assert ("Authorization" in pr._headers(base)) is allowed


def test_session_comes_from_the_app_handoff(monkeypatch) -> None:
    from potluck_local.services import contributor_service as cs

    svc = cs.ContributorService()
    svc.set_member_session("from-the-app")
    cs.set_contributor_service_for_tests(svc)
    try:
        assert pr._member_session_token() == "from-the-app"
    finally:
        cs.set_contributor_service_for_tests(None)


# ---- which circle -----------------------------------------------------------


def _coordinator(monkeypatch, circles, status=200, calls=None):
    """Stand in for GET /me/circles."""

    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(request)
        assert request.url.path == "/me/circles"
        return httpx.Response(status, json={"circles": circles} if status == 200 else {"detail": "x"})

    def client(**kw):
        return _REAL_CLIENT(transport=httpx.MockTransport(handler), **kw)

    monkeypatch.setattr(pr.httpx, "AsyncClient", client)
    monkeypatch.setattr(pr, "_member_session_token", lambda: "sess-abc")
    monkeypatch.setattr(pr, "coordinator_http_base", lambda: "https://coordinator.example")


def _fail(selector):
    with pytest.raises(pr.CircleSelectionError) as exc:
        run(pr.resolve_circle_id(selector))
    return exc.value


def test_an_id_is_used_as_given_without_a_lookup(monkeypatch) -> None:
    calls: list = []
    _coordinator(monkeypatch, [], calls=calls)
    assert run(pr.resolve_circle_id(CIRCLE_A.upper())) == CIRCLE_A
    assert calls == []


def test_a_name_resolves_ignoring_case(monkeypatch) -> None:
    _coordinator(monkeypatch, [{"id": CIRCLE_A, "name": "Friends"}, {"id": CIRCLE_B, "name": "Work"}])
    assert run(pr.resolve_circle_id("friends")) == CIRCLE_A


def test_one_circle_needs_no_choice(monkeypatch) -> None:
    calls: list = []
    _coordinator(monkeypatch, [{"id": CIRCLE_B, "name": "Work"}], calls=calls)
    assert run(pr.resolve_circle_id(None)) == CIRCLE_B
    assert calls[0].headers["authorization"] == "Bearer sess-abc"
    assert run(pr.resolve_circle_id(None)) == CIRCLE_B
    assert len(calls) == 1, "the list is reused for a short while"


def test_several_circles_and_no_choice_lists_them(monkeypatch) -> None:
    _coordinator(monkeypatch, [{"id": CIRCLE_A, "name": "Friends"}, {"id": CIRCLE_B, "name": "Work"}])
    err = _fail(None)
    assert err.status_code == 400 and "'Friends'" in err.detail and "'Work'" in err.detail


def test_unknown_name_duplicates_and_no_circles(monkeypatch) -> None:
    _coordinator(monkeypatch, [{"id": CIRCLE_A, "name": "Friends"}, {"id": CIRCLE_B, "name": "friends"}])
    assert _fail("Nope").status_code == 404
    assert _fail("Friends").status_code == 400
    pr._circle_cache.clear()
    _coordinator(monkeypatch, [])
    assert _fail(None).status_code == 409


def test_signed_out_and_expired_are_401(monkeypatch) -> None:
    _coordinator(monkeypatch, [], status=401)
    assert _fail("Friends").status_code == 401
    monkeypatch.setattr(pr, "_member_session_token", lambda: None)
    err = _fail(CIRCLE_A)
    assert err.status_code == 401 and "Sign in" in err.detail
