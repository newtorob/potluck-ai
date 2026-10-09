"""Shared stand-ins for the direct circle link tests.

`World` is a scripted coordinator, a scripted sidecar (the `fake_mesh` fixture
with links enabled) and scripted lenders, all behind one httpx mock transport,
so a borrower's algorithm can be driven branch by branch without a network.

`Fixture` is the wire contract: services/coordinator/tests/fixtures/
circle_link_shapes.json, read the way test_coordinator_requests.py reads
engine_job_bodies.json.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import json
import secrets
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from potluck_local.services import circle_link, pool_routing

FIXTURE: dict[str, Any] = json.loads(
    (Path(__file__).resolve().parents[4] / "services" / "coordinator" / "tests"
     / "fixtures" / "circle_link_shapes.json").read_text()
)

CIRCLE = "7f0c5a2e-58a1-4b0c-9a51-0f5e3a6f1d11"
NODE = "3b1f0c52-7d1e-4a55-9a0e-1f2b3c4d5e6f"
MODEL = "qwen3-4b-instruct-2507-q4"
BASE = "https://coordinator.example"
COORD_HOST = "coordinator.example"
MESSAGES = [{"role": "user", "content": "hi"}]

_REAL_CLIENT = httpx.AsyncClient


def sse(payload: Any) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode()


DONE = b"data: [DONE]\n\n"


def content(text: str) -> bytes:
    return sse({"choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}]})


def meta(tokens: int = 2, duration_ms: int = 120, finish: str = "stop", **extra: Any) -> bytes:
    body = {"served_by": "contributor", "served_scope": "circle", "output_tokens": tokens,
            "duration_ms": duration_ms, "finish_reason": finish, **extra}
    return sse({"meta": body})


def error_event(category: str = "inference_error", message: str = "Inference failed") -> bytes:
    return sse({"error": {"category": category, "message": message}})


class Body(httpx.AsyncByteStream):
    """A response body made of steps: bytes are sent, numbers sleep, an Event is
    waited for, an exception is raised."""

    def __init__(self, *steps: Any) -> None:
        self.steps = steps
        self.closed = False

    async def __aiter__(self):
        for step in self.steps:
            if isinstance(step, BaseException):
                raise step
            if isinstance(step, asyncio.Event):
                await step.wait()
            elif isinstance(step, (int, float)) and not isinstance(step, bool):
                await asyncio.sleep(step)
            else:
                yield step

    async def aclose(self) -> None:
        self.closed = True


def stream_response(*steps: Any, status: int = 200, headers: dict[str, str] | None = None) -> httpx.Response:
    return httpx.Response(status, headers={"content-type": "text/event-stream", **(headers or {})},
                          stream=Body(*steps))


def good_stream(*words: str) -> httpx.Response:
    words = words or ("Hello", " world")
    return stream_response(*(content(w) for w in words), meta(tokens=len(words)), DONE)


def lender_error(status: int, code: str, message: str = "x", **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"code": code, "message": message}}, headers=headers)


class World:
    """The coordinator, the sidecar and the lenders a borrower talks to."""

    def __init__(self, monkeypatch: Any, fake_mesh: Any) -> None:
        self.mesh = fake_mesh
        self.mesh.own_ip = "100.64.0.9"
        self.mesh.enable_links(NODE)
        self.calls: list[tuple[str, str, str]] = []  # method, host, path
        self.requests: list[httpx.Request] = []
        self.bodies: list[Any] = []
        self.client_kwargs: list[dict[str, Any]] = []
        self.creates: list[dict[str, Any]] = []
        self.links: dict[str, dict[str, Any]] = {}
        self.next_lender = 2
        self.ttl_s = 600
        self.renew_after_s = 300
        self.mode: Any = "direct"
        self.reachable = True
        self.create_delay = 0.0
        # Overrides: each returns an httpx.Response, or None to use the default.
        self.create: Callable[[dict[str, Any]], httpx.Response | None] | None = None
        self.renew: Callable[[str], httpx.Response | None] | None = None
        self.relay: Callable[[Any], httpx.Response | None] | None = None
        self.lender: dict[str, Callable[[httpx.Request, dict[str, Any]], httpx.Response | None]] = {}
        self.lender_default: Callable[[httpx.Request, dict[str, Any]], httpx.Response | None] | None = None
        monkeypatch.setattr(circle_link, "_new_client", self._client)
        monkeypatch.setattr(pool_routing.httpx, "AsyncClient", self._plain_client)
        monkeypatch.setattr(pool_routing, "coordinator_http_base", lambda: BASE)
        monkeypatch.setattr(pool_routing, "_member_session_token", lambda: "sess-abc")

    # ---- plumbing ----------------------------------------------------------

    def _client(self, **kwargs: Any) -> httpx.AsyncClient:
        self.client_kwargs.append(dict(kwargs))
        return _REAL_CLIENT(transport=httpx.MockTransport(self._handle), **kwargs)

    def _plain_client(self, **kwargs: Any) -> httpx.AsyncClient:
        return _REAL_CLIENT(transport=httpx.MockTransport(self._handle), **kwargs)

    def count(self, method: str, suffix: str = "", host: str | None = None) -> int:
        return sum(1 for m, h, p in self.calls
                   if m == method and p.endswith(suffix) and (host is None or h == host))

    def lender_calls(self) -> list[httpx.Request]:
        return [r for r in self.requests if r.url.host != COORD_HOST]

    def relay_bodies(self) -> list[dict[str, Any]]:
        return [b for r, b in zip(self.requests, self.bodies) if r.url.path == "/jobs/stream"]

    async def _handle(self, request: httpx.Request) -> httpx.Response:
        body: Any = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = None
        self.calls.append((request.method, request.url.host, request.url.path))
        self.requests.append(request)
        self.bodies.append(body)
        if request.url.host == COORD_HOST:
            path = request.url.path
            if path == "/jobs/stream":
                return self._relay(body)
            if request.method == "POST" and path.endswith("/renew"):
                return self._renew(path.rsplit("/", 2)[-2])
            if request.method == "POST" and path.endswith("/links"):
                self.creates.append(body)
                if self.create_delay:
                    await asyncio.sleep(self.create_delay)
                return self._create(body)
            if request.method == "DELETE":
                link_id = path.rsplit("/", 1)[-1]
                link = self.links.get(link_id)
                if link:
                    self.mesh.remove_circle_peer(link["ip"])
                return httpx.Response(204)
            return httpx.Response(404, json={"detail": "not found"})
        link = next((l for l in self.links.values() if l["ip"] == request.url.host), {})
        responder = self.lender.get(request.url.host) or self.lender_default
        if responder is not None:
            response = responder(request, link)
            if inspect.isawaitable(response):
                response = await response
            if response is not None:
                return response
        if request.headers.get("authorization") != f"Bearer {link.get('token')}":
            return lender_error(401, "bad_token", "unauthorized")
        return good_stream()

    # ---- the coordinator's answers ----------------------------------------

    def new_link(self, ip: str | None = None, *, listed: bool = True, reachable: bool | None = None) -> dict[str, Any]:
        n = self.next_lender
        self.next_lender += 1
        ip = ip or f"100.64.1.{n}"
        link = {
            "link_id": str(uuid.uuid4()),
            "token": base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode(),
            "ip": ip,
            "port": 8340,
        }
        for old in [k for k, v in self.links.items() if v["ip"] == ip]:
            del self.links[old]  # one live link per pair of machines: the new one replaces it
        self.links[link["link_id"]] = link
        if listed:
            self.mesh.add_circle_peer(
                ip, reachable=self.reachable if reachable is None else reachable, mode=self.mode,
                outbound={"link_id": link["link_id"], "port": link["port"], "expires_in_s": 590},
            )
        return link

    def created(self, link: dict[str, Any], **override: Any) -> httpx.Response:
        body = {
            "link_id": link["link_id"], "circle_id": CIRCLE, "model_slug": MODEL, "token": link["token"],
            "lender": {"mesh_ip": link["ip"], "port": link["port"]}, "ttl_s": self.ttl_s,
            "renew_after_s": self.renew_after_s, "expires_at": "2026-10-09T12:10:00Z", "ready": True,
        }
        body.update(override)
        return httpx.Response(201, json=body)

    def _create(self, body: dict[str, Any]) -> httpx.Response:
        if self.create is not None:
            response = self.create(body)
            if response is not None:
                return response
        return self.created(self.new_link())

    def _renew(self, link_id: str) -> httpx.Response:
        if self.renew is not None:
            response = self.renew(link_id)
            if response is not None:
                return response
        return httpx.Response(200, json={
            "link_id": link_id, "ttl_s": self.ttl_s, "renew_after_s": self.renew_after_s,
            "expires_at": "2026-10-09T12:20:00Z", "delivered": {"requester": True, "lender": True},
        })

    def _relay(self, body: Any) -> httpx.Response:
        if self.relay is not None:
            response = self.relay(body)
            if response is not None:
                return response
        return stream_response(
            sse({"choices": [{"delta": {"content": "Relay"}}]}),
            sse({"choices": [{"delta": {"content": "ed"}}]}),
            meta(tokens=2, duration_ms=300),
            DONE,
        )


def error_response(status: int, code: str, message: str = "x", reason: Any = None, **headers: str) -> httpx.Response:
    return httpx.Response(status, json={"detail": {"code": code, "message": message, "reason": reason}},
                          headers=headers)


async def collect(stream: Any) -> list[dict[str, Any]]:
    return [chunk async for chunk in stream]


def text_of(chunks: list[dict[str, Any]]) -> str:
    return "".join(c["choices"][0]["delta"].get("content", "") for c in chunks)


def circle_stream(**over: Any) -> Any:
    """pool_routing's circle stream, as the gateway calls it."""
    args = dict(messages=MESSAGES, model=MODEL, temperature=0.7, max_tokens=512, scope="circle", circle_id=CIRCLE)
    args.update(over)
    return pool_routing.stream_pool_openai(**args)


def fast_budget(monkeypatch: Any, ready: float = 0.4, poll: float = 0.02) -> None:
    """Shrink the readiness budget so a link that never comes up costs a fraction
    of a second. The real numbers are pinned by their own test."""
    monkeypatch.setattr(circle_link, "LINK_READY_BUDGET_S", ready)
    monkeypatch.setattr(circle_link, "LINK_POLL_INTERVAL_S", poll)
