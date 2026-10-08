"""Trusted circles through the engine, for a machine with only the CLI.

The engine makes these calls to the coordinator as the signed-in member. The
coordinator here is an in-test stand-in; what is pinned is what the engine
sends, which circle a name resolves to, and what the person at the terminal
is told when it goes wrong.
"""
from __future__ import annotations

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from potluck_local.services import pool_routing as pr

CIRCLE_A = "7f0c5a2e-58a1-4b0c-9a51-0f5e3a6f1d11"
_REAL_CLIENT = httpx.AsyncClient
TOKEN = "sidecar-test-token"


class Coordinator:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict | None, str | None]] = []
        self.circles = [{"id": CIRCLE_A, "name": "Friends", "role": "owner", "people": [], "members": []}]
        self.fail: tuple[int, dict] | None = None

    def handle(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body, request.headers.get("authorization")))
        if self.fail is not None:
            return httpx.Response(self.fail[0], json=self.fail[1])
        path, method = request.url.path, request.method
        if (method, path) == ("GET", "/me/circles"):
            return httpx.Response(200, json={"circles": self.circles})
        if path.endswith("/availability"):
            return httpx.Response(200, json={"circle_id": CIRCLE_A, "models": [{"slug": "m", "machines": 2}]})
        if (method, path) == ("POST", "/me/circles"):
            return httpx.Response(201, json={"id": CIRCLE_A, "name": body["name"], "role": "owner"})
        if path.endswith("/invites") and method == "POST":
            return httpx.Response(201, json={"code": "ABCD-EFGH-JKMN", "url": "https://x/?code=ABCD-EFGH-JKMN",
                                             "expires_at": "2026-10-15T00:00:00Z", "max_uses": body["max_uses"]})
        if path == "/circles/invites/preview":
            return httpx.Response(200, json={"circle_name": "Friends", "invited_by": "Ada", "people": 1, "already_in": False})
        if path == "/circles/invites/accept":
            return httpx.Response(200, json={"id": CIRCLE_A, "name": "Friends", "role": "member"})
        if method == "DELETE":
            return httpx.Response(204)
        return httpx.Response(404, json={"detail": "not found"})


@pytest.fixture()
def coordinator(monkeypatch):
    fake = Coordinator()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _REAL_CLIENT(transport=httpx.MockTransport(fake.handle), **kw))
    monkeypatch.setattr(pr, "coordinator_http_base", lambda: "https://coordinator.example")
    monkeypatch.setattr(pr, "_member_session_token", lambda: "sess-abc")
    pr._circle_cache.clear()
    yield fake
    pr._circle_cache.clear()


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("POTLUCK_SIDECAR_TOKEN", TOKEN)
    from potluck_local.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000), raise_server_exceptions=False) as c:
        c.headers["Authorization"] = f"Bearer {TOKEN}"
        yield c


def test_lists_circles_with_what_each_can_run_now(client, coordinator) -> None:
    body = client.get("/circles").json()
    assert body["schemaVersion"] == 1
    assert body["circles"][0]["name"] == "Friends"
    assert body["circles"][0]["models"] == [{"slug": "m", "machines": 2}]
    circle_calls = [c for c in coordinator.calls if "circles" in c[1]]
    assert circle_calls and all(auth == "Bearer sess-abc" for *_, auth in circle_calls)


def test_create_invite_by_circle_name_and_join_with_a_pasted_code(client, coordinator) -> None:
    assert client.post("/circles", json={"name": "  Friends  "}).json()["name"] == "Friends"
    invite = client.post("/circles/friends/invites", json={"max_uses": 2}).json()
    assert invite["code"] == "ABCD-EFGH-JKMN" and invite["max_uses"] == 2
    assert ("POST", f"/me/circles/{CIRCLE_A}/invites", {"expires_in_days": 7, "max_uses": 2}, "Bearer sess-abc") in coordinator.calls

    assert client.post("/circles/invites/preview", json={"code": " abcd-efgh-jkmn "}).json()["circle_name"] == "Friends"
    assert client.post("/circles/invites/accept", json={"code": "abcd-efgh-jkmn"}).json()["role"] == "member"
    assert ("POST", "/circles/invites/accept", {"code": "abcd-efgh-jkmn"}, "Bearer sess-abc") in coordinator.calls


def test_leave_and_delete_resolve_the_circle_and_forget_the_cached_list(client, coordinator) -> None:
    client.get("/circles")
    assert client.delete("/circles/Friends/membership").status_code == 204
    assert ("DELETE", f"/me/circles/{CIRCLE_A}/membership", None, "Bearer sess-abc") in coordinator.calls
    assert pr._circle_cache == {}, "the next gateway request must re-read the member's circles"
    assert client.delete(f"/circles/{CIRCLE_A}").status_code == 204
    missing = client.delete("/circles/Nope")
    assert missing.status_code == 404 and missing.headers["x-potluck-error"] == "circle_not_found"


def test_the_person_is_told_what_went_wrong(client, coordinator, monkeypatch) -> None:
    coordinator.fail = (404, {"detail": "That invite is not valid. It may have expired or already been used. Ask for a new one."})
    resp = client.post("/circles/invites/accept", json={"code": "AAAA-BBBB-CCCC"})
    assert resp.status_code == 404 and "not valid" in resp.json()["detail"]
    # The CLI never prints a response body, so the reason is also a code.
    assert resp.headers["x-potluck-error"] == "invite_invalid"

    coordinator.fail = (401, {"detail": "Session expired or invalid"})
    resp = client.get("/circles")
    assert resp.status_code == 401 and "potluck login" in resp.json()["detail"]
    assert resp.headers["x-potluck-error"] == "sign_in_expired"

    coordinator.fail = (409, {"detail": "You own this circle. Delete it to end it for everyone."})
    pr._circle_cache[("https://coordinator.example", "Bearer sess-abc")] = (__import__("time").monotonic(), coordinator.circles)
    assert client.delete("/circles/Friends/membership").headers["x-potluck-error"] == "owner_cannot_leave"

    coordinator.fail = (500, {"detail": "boom"})
    resp = client.get("/circles")
    assert resp.status_code == 502 and resp.headers["x-potluck-error"] == "coordinator_unreachable"

    coordinator.fail = None
    monkeypatch.setattr(pr, "_member_session_token", lambda: None)
    resp = client.post("/circles", json={"name": "x"})
    assert resp.status_code == 401 and resp.headers["x-potluck-error"] == "signed_out"
    assert coordinator.calls[-1][1] != "/me/circles" or coordinator.calls[-1][0] != "POST", "nothing is sent when signed out"


def test_circle_routes_need_the_local_runtime_token(monkeypatch, coordinator) -> None:
    monkeypatch.delenv("POTLUCK_SIDECAR_TOKEN", raising=False)
    from potluck_local.app import create_app

    with TestClient(create_app(), client=("127.0.0.1", 50000), raise_server_exceptions=False) as open_client:
        assert open_client.get("/circles").status_code == 503
    assert [c for c in coordinator.calls if "circles" in c[1]] == []
