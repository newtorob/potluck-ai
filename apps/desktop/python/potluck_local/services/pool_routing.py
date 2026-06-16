"""Open-pool routing for the OpenAI/Ollama gateway.

The third and widest routing scope. When a gateway request asks for a model that
is neither loaded locally nor on a reachable household peer, this dispatches it
through the coordinator to the OPEN POOL (machines owned by other members),
reusing the same blinded job path the desktop chat uses for "Use contributor
network".

How it differs from household routing (gateway_routing.py):
  * household stays entirely on the user's own machines over the mesh; the
    coordinator is never involved.
  * pool goes through the coordinator's POST /jobs/stream, which selects a peer
    via blinded routing (no member identity crosses the wire) and streams tokens
    back. This serves STRANGERS' models, so it is opt-in (GatewaySettings.
    pool_routing, default off) and never automatic.

Privacy invariants (memory 42a7e671):
  * SINGLE-TURN. Only the latest user message is sent as the prompt — never the
    full conversation. The coordinator's job schema has no multi-turn slot and
    sending history to strangers is a privacy hazard. This matches the frontend
    streamNetworkJob decision; do not widen it without a thoughtful design.
  * No member-identifying metadata leaves this process. The coordinator does the
    blinding; we send only prompt + model_slug + sampling + scope.

Config: the coordinator HTTP base + token come from the environment
(POTLUCK_COORDINATOR_HTTP_URL, or derived from the contributor's
POTLUCK_COORDINATOR_URL; POTLUCK_COORDINATOR_TOKEN for auth). When neither is
configured, pool routing is simply unavailable and the gateway degrades to a 404
exactly as if no peer had the model. Wiring the coordinator URL from the
frontend/connectionStore into the main sidecar is a follow-on.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 5.0
# Match the coordinator's per-job ceiling (JOB_TIMEOUT_S) loosely; the read is
# unbounded for the streaming body, only the connect is capped.
_POOL_SCOPE = "pool"


class PoolDispatchError(RuntimeError):
    """The coordinator returned a non-200 for a pool job. Carries the upstream
    status + detail so the gateway can surface a faithful HTTP error (e.g. a 503
    "no peer serves this model") instead of a generic 500."""

    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.detail = self._extract(body)
        super().__init__(f"coordinator returned {status_code}: {self.detail}")

    @staticmethod
    def _extract(body: str) -> str:
        """Pull the human message out of the coordinator's JSON error envelope
        ({"detail": "..."}), falling back to the raw body."""
        try:
            obj = json.loads(body)
        except ValueError:
            return body
        if isinstance(obj, dict):
            d = obj.get("detail") or obj.get("message")
            if d is not None:
                return d if isinstance(d, str) else str(d)
        return body


def coordinator_http_base() -> str | None:
    """Resolve the coordinator's HTTP base URL, or None when unconfigured.

    Order:
      1. POTLUCK_COORDINATOR_HTTP_URL — explicit http(s) base (e.g.
         "http://100.64.0.1:8400"). Wins if set.
      2. POTLUCK_COORDINATOR_URL — the contributor's websocket URL
         (e.g. "ws://100.64.0.1:8400/peers/connect"); converted ws->http and
         stripped of its path.
    Returns a base with no trailing slash, or None.
    """
    explicit = os.environ.get("POTLUCK_COORDINATOR_HTTP_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    ws = os.environ.get("POTLUCK_COORDINATOR_URL", "").strip()
    if not ws:
        return None
    parts = urlsplit(ws)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    if not parts.netloc:
        return None
    # Drop the path (/peers/connect) and any query/fragment; keep scheme + host.
    return urlunsplit((scheme, parts.netloc, "", "", "")).rstrip("/")


def _coordinator_token() -> str | None:
    tok = os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip()
    return tok or None


def pool_available() -> bool:
    """True when a coordinator base is configured (independent of the opt-in
    setting)."""
    return coordinator_http_base() is not None


def pool_enabled() -> bool:
    """True when open-pool routing should be used: the user opted in
    (GatewaySettings.pool_routing) AND a coordinator is configured. Read live so
    toggling takes effect without a restart, matching the gateway key."""
    if not pool_available():
        return False
    try:
        from potluck_local.services.settings_manager import get_settings

        return bool(get_settings().gateway.pool_routing)
    except Exception:
        logger.debug("pool_enabled_settings_unavailable", exc_info=True)
        return False


def last_user_prompt(messages: list[dict]) -> str:
    """The latest user message content — the only thing we send to the pool.

    Single-turn by design (see module docstring). Returns "" when there is no
    user turn: we deliberately do NOT fall back to a system/assistant message,
    so a crafted request ending in a system prompt can't push that content to a
    stranger.
    """
    for m in reversed(messages):
        if m.get("role") == "user" and m.get("content"):
            return str(m["content"])
    return ""


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    tok = _coordinator_token()
    if tok:
        h["X-Potluck-Coordinator-Token"] = tok
    return h


def _job_body(prompt: str, model: str, temperature: float, max_tokens: int) -> dict:
    return {
        "prompt": prompt,
        "model_slug": model,
        "sampling": {"temperature": temperature, "max_tokens": max_tokens},
        "scope": _POOL_SCOPE,
    }


def _content_of(line: str) -> str | None:
    """Pull a content delta from one coordinator SSE line.

    The coordinator emits three shapes (services/coordinator/routers/jobs.py):
      data: {"choices":[{"delta":{"content":"..."}}]}   token
      data: {"meta":{...}}                               completion summary
      data: {"error":{"category","message"}}            failure
      data: [DONE]                                       terminator
    Raises RuntimeError on an error chunk. Returns None for non-content lines.
    """
    if not line.startswith("data:"):
        return None
    data = line[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        obj = json.loads(data)
    except ValueError:
        return None
    if "error" in obj:
        err = obj["error"]
        if isinstance(err, dict):
            raise RuntimeError(err.get("message") or err.get("category") or "pool job error")
        raise RuntimeError(str(err))
    if "meta" in obj:
        return None
    try:
        return obj["choices"][0].get("delta", {}).get("content")
    except (KeyError, IndexError, AttributeError):
        return None


async def stream_pool_openai(prompt: str, model: str, temperature: float, max_tokens: int):
    """Dispatch to the open pool via the coordinator and yield OpenAI
    chat.completion.chunk dicts (same shape gateway_routing.stream_peer_openai
    yields, so the gateway routers consume both identically)."""
    base = coordinator_http_base()
    if not base:
        raise RuntimeError("open-pool routing is not configured (no coordinator URL)")

    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    def chunk(delta: dict, finish: str | None) -> dict:
        return {
            "id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }

    timeout = httpx.Timeout(None, connect=_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream(
            "POST", f"{base}/jobs/stream",
            json=_job_body(prompt, model, temperature, max_tokens), headers=_headers(),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise PoolDispatchError(resp.status_code, body)
            yield chunk({"role": "assistant"}, None)
            async for line in resp.aiter_lines():
                content = _content_of(line)
                if content:
                    yield chunk({"content": content}, None)
            yield chunk({}, "stop")


async def complete_pool_openai(prompt: str, model: str, temperature: float, max_tokens: int) -> dict:
    """Dispatch to the open pool (streamed under the hood) and return one OpenAI
    chat.completion envelope."""
    parts: list[str] = []
    base = coordinator_http_base()
    if not base:
        raise RuntimeError("open-pool routing is not configured (no coordinator URL)")
    timeout = httpx.Timeout(None, connect=_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream(
            "POST", f"{base}/jobs/stream",
            json=_job_body(prompt, model, temperature, max_tokens), headers=_headers(),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise PoolDispatchError(resp.status_code, body)
            async for line in resp.aiter_lines():
                content = _content_of(line)
                if content:
                    parts.append(content)
    text = "".join(parts)
    return {
        "id": "chatcmpl-" + uuid.uuid4().hex[:24],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None},
    }


async def pool_model_slugs() -> list[str]:
    """Model slugs currently served by online pool peers, from the coordinator's
    aggregate /transparency/fleet. Best-effort: [] on any failure. Used only for
    /v1/models + /api/tags discovery so a tool can request a pool-only model."""
    base = coordinator_http_base()
    if not base:
        return []
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT_S) as c:
            r = await c.get(f"{base}/transparency/fleet", headers=_headers())
            if r.status_code != 200:
                return []
            data = r.json()
        models = data.get("models", []) if isinstance(data, dict) else []
        out: list[str] = []
        for m in models:
            slug = m.get("slug")
            if slug and (m.get("online_peer_count") or 0) > 0:
                out.append(slug)
        return out
    except Exception:
        logger.debug("pool_model_slugs_unavailable", exc_info=True)
        return []
