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

Config: the coordinator HTTP base comes from POTLUCK_COORDINATOR_HTTP_URL,
then settings, then the contributor's POTLUCK_COORDINATOR_URL, then the public
coordinator. Requests carry the signed-in member's session, which is what the
coordinator authenticates in production; POTLUCK_COORDINATOR_TOKEN is added
only where an operator set it (the dev fleet).

Trusted circles: scope "circle" always names one circle (circle_id). The
gateway caller picks it with X-Potluck-Circle (a name or an ID); with exactly
one circle no choice is needed. A circle request goes through
services/circle_link.py, which tries a direct link to a circle member's machine
and falls back to the coordinator relay here. It is never sent to the pool.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from contextlib import aclosing
from urllib.parse import urlsplit, urlunsplit

import httpx

from .remote_stream import collect_completion, openai_chunks

logger = logging.getLogger(__name__)

_CONNECT_TIMEOUT_S = 5.0
# Match the coordinator's per-job ceiling (JOB_TIMEOUT_S) loosely; the read is
# unbounded for the streaming body, only the connect is capped.
_POOL_SCOPE = "pool"
_CIRCLE_SCOPE = "circle"

# Where the engine reaches the coordinator when nothing overrides it: the same
# default the packaged app and the account service use.
DEFAULT_COORDINATOR_HTTP_BASE = "https://coordinator.trypotluck.ai"

# How long a member's circle list is reused before asking the coordinator again.
_CIRCLE_LIST_TTL_S = 30.0


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


def coordinator_http_base() -> str:
    """Resolve the coordinator's HTTP base URL.

    Order:
      1. POTLUCK_COORDINATOR_HTTP_URL — explicit http(s) base (e.g.
         "http://100.64.0.1:8400"). Env override, wins (tests / ops).
      2. settings.network.coordinator_url — synced from the frontend's
         connection setting; the normal source in the running app.
      3. POTLUCK_COORDINATOR_URL — the contributor's websocket URL
         (e.g. "ws://100.64.0.1:8400/peers/connect"); converted ws->http and
         stripped of its path.
      4. The public coordinator, so a default install needs no setup.
    Returns a base with no trailing slash.
    """
    explicit = os.environ.get("POTLUCK_COORDINATOR_HTTP_URL", "").strip()
    if explicit:
        return explicit.rstrip("/")

    from_settings = _settings_coordinator_url()
    if from_settings:
        return from_settings.rstrip("/")

    ws = os.environ.get("POTLUCK_COORDINATOR_URL", "").strip()
    if not ws:
        return DEFAULT_COORDINATOR_HTTP_BASE
    parts = urlsplit(ws)
    scheme = {"ws": "http", "wss": "https"}.get(parts.scheme, parts.scheme)
    if not parts.netloc:
        return DEFAULT_COORDINATOR_HTTP_BASE
    # Drop the path (/peers/connect) and any query/fragment; keep scheme + host.
    return urlunsplit((scheme, parts.netloc, "", "", "")).rstrip("/")


def _settings_coordinator_url() -> str | None:
    """The coordinator HTTP base persisted in settings (network.coordinator_url),
    synced from the frontend. None when empty/unavailable."""
    try:
        from potluck_local.services.settings_manager import get_settings

        url = (get_settings().network.coordinator_url or "").strip()
        return url or None
    except Exception:
        logger.debug("settings_coordinator_url_unavailable", exc_info=True)
        return None


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


def _pool_multiturn_enabled() -> bool:
    """True when the user explicitly opted in to sending the full messages array
    to pool contributors (gateway.pool_multiturn, default OFF).

    This is checked only on the pool path; circle always sends full messages
    regardless of this setting. Read live so toggling takes effect without a
    restart.
    """
    try:
        from potluck_local.services.settings_manager import get_settings

        return bool(get_settings().gateway.pool_multiturn)
    except Exception:
        logger.debug("pool_multiturn_settings_unavailable", exc_info=True)
        return False


def refusal_message(exc: "PoolDispatchError", scope: str, model: str) -> str:
    """What to tell the caller when the coordinator turned a request away
    before any answer started. The coordinator's own detail is written for
    API clients ("...or use scope=pool"); a circle request gets plain words."""
    if exc.status_code == 401:
        return "Your sign-in has expired. Sign in again to use the network."
    if scope == _CIRCLE_SCOPE:
        if exc.status_code == 503:
            return f"No machine in this circle is offering {model} right now."
        if exc.status_code == 404:
            return "That trusted circle no longer exists."
    return exc.detail


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


def _member_session_token() -> str | None:
    """The signed-in member's session, when the engine holds one: handed over
    by the desktop app (held in memory), or read from the account store the
    CLI shares on POSIX. Never logged."""
    try:
        from potluck_local.services.contributor_service import get_contributor_service

        token = get_contributor_service().member_session_token()
        if token:
            return token
    except Exception:
        logger.debug("member_session_from_app_unavailable", exc_info=True)
    if os.name != "nt":
        try:
            from potluck_local.services.member_account import get_member_account

            session = get_member_account().session()
            if session:
                return session["token"]
        except Exception:
            logger.debug("member_session_from_store_unavailable", exc_info=True)
    return None


def _session_may_travel(base: str) -> bool:
    """The session is a credential: it goes over TLS, or to a coordinator on
    this machine, the local network or the mesh, and nowhere else."""
    from potluck_local.services.contributor_service import _plaintext_ok

    parts = urlsplit(base)
    return parts.scheme == "https" or (parts.scheme == "http" and _plaintext_ok(parts.hostname))


def _headers(base: str | None = None) -> dict[str, str]:
    """Headers for a coordinator call. Pass ``base`` for calls made as the
    member (jobs, circles); leave it out for public reads."""
    h = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    tok = _coordinator_token()
    if tok:
        h["X-Potluck-Coordinator-Token"] = tok
    if base and _session_may_travel(base):
        session = _member_session_token()
        if session:
            h["Authorization"] = f"Bearer {session}"
    return h


class CircleSelectionError(RuntimeError):
    """A circle request that cannot name one circle. ``status_code`` and
    ``detail`` are what the gateway caller should see."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


_circle_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


async def _member_circles(base: str, headers: dict[str, str]) -> list[dict]:
    key = (base, headers.get("Authorization", ""))
    hit = _circle_cache.get(key)
    if hit and time.monotonic() - hit[0] < _CIRCLE_LIST_TTL_S:
        return hit[1]
    try:
        async with httpx.AsyncClient(timeout=_CONNECT_TIMEOUT_S) as c:
            r = await c.get(f"{base}/me/circles", headers={**headers, "Accept": "application/json"})
    except httpx.HTTPError:
        raise CircleSelectionError(502, "Could not reach the coordinator to read your trusted circles.") from None
    if r.status_code == 401:
        raise CircleSelectionError(401, "Your sign-in has expired. Sign in again to use a trusted circle.")
    if r.status_code != 200:
        raise CircleSelectionError(502, "Could not read your trusted circles from the coordinator.")
    data = r.json()
    circles = [c for c in (data.get("circles") or []) if isinstance(c, dict) and c.get("id")]
    _circle_cache.clear()
    _circle_cache[key] = (time.monotonic(), circles)
    return circles


async def resolve_circle_id(selector: str | None) -> str:
    """Turn the caller's circle choice (an ID, a name, or nothing) into one
    circle ID. Nothing is fine when the member has exactly one circle."""
    base = coordinator_http_base()
    headers = _headers(base)
    if "Authorization" not in headers:
        raise CircleSelectionError(401, "Sign in to use a trusted circle.")
    chosen = (selector or "").strip()
    if chosen:
        try:
            return str(uuid.UUID(chosen))
        except ValueError:
            pass
    circles = await _member_circles(base, headers)
    names = ", ".join(sorted(f"'{c.get('name')}'" for c in circles))
    if chosen:
        matches = [c for c in circles if str(c.get("name", "")).casefold() == chosen.casefold()]
        if len(matches) == 1:
            return str(matches[0]["id"])
        if matches:
            raise CircleSelectionError(400, f"More than one trusted circle is named '{chosen}'. Use its ID.")
        raise CircleSelectionError(
            404, f"No trusted circle named '{chosen}'." + (f" Yours: {names}." if names else "")
        )
    if len(circles) == 1:
        return str(circles[0]["id"])
    if not circles:
        raise CircleSelectionError(409, "You have no trusted circles yet. Create one in the app under Network.")
    raise CircleSelectionError(400, f"Choose a trusted circle with X-Potluck-Circle. Yours: {names}.")


def _job_body(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    scope: str = _POOL_SCOPE,
    circle_id: str | None = None,
) -> dict:
    """Build the coordinator job body, applying the multi-turn privacy policy.

    prompt = last_user_prompt(messages) is ALWAYS populated as the backward-compat
    fallback. An old contributor that does not know `messages` uses `prompt`.

    messages is included when:
      - scope == "circle" (trusted members always receive the full conversation), OR
      - scope == "pool" AND gateway.pool_multiturn is explicitly enabled by the user.

    Default (pool, multiturn off): only prompt is sent — no conversation history
    crosses the wire to strangers. This preserves the original privacy invariant.
    """
    prompt = last_user_prompt(messages)
    body: dict = {
        "prompt": prompt,
        "model_slug": model,
        "sampling": {"temperature": temperature, "max_tokens": max_tokens},
        "scope": scope,
    }
    if scope == _CIRCLE_SCOPE:
        if not circle_id:
            raise ValueError("a circle request needs a circle_id")
        body["circle_id"] = circle_id
    send_messages = scope == _CIRCLE_SCOPE or (
        scope == _POOL_SCOPE and _pool_multiturn_enabled()
    )
    if send_messages:
        body["messages"] = [
            {"role": m.get("role", ""), "content": m.get("content", "")}
            for m in messages
        ]
    return body


async def stream_pool_openai(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    scope: str = _POOL_SCOPE,
    circle_id: str | None = None,
):
    """Dispatch to the coordinator and yield OpenAI chat.completion.chunk dicts.

    Works for both pool (strangers) and circle (trusted members) scopes — the
    privacy policy in _job_body decides whether messages or only prompt is sent.
    Yields the same chunk shape as gateway_routing.stream_peer_openai so gateway
    routers consume both identically.
    """
    base = coordinator_http_base()
    if not base:
        raise RuntimeError("open-pool routing is not configured (no coordinator URL)")

    if scope == _CIRCLE_SCOPE and circle_id:
        # A circle request tries a direct link to a circle member's machine first
        # and falls back to the relay below. Whatever happens, it is never sent
        # to the open pool. Imported here because circle_link imports this module.
        from .circle_link import stream_circle_openai

        async with aclosing(stream_circle_openai(
            messages, model, temperature, max_tokens, circle_id,
            relay=lambda: _stream_coordinator(base, messages, model, temperature, max_tokens, scope, circle_id),
        )) as chunks:
            async for chunk in chunks:
                yield chunk
        return

    async with aclosing(_stream_coordinator(base, messages, model, temperature, max_tokens, scope, circle_id)) as chunks:
        async for chunk in chunks:
            yield chunk


async def _stream_coordinator(
    base: str,
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    scope: str,
    circle_id: str | None,
):
    """The coordinator's POST /jobs/stream, as OpenAI chunks: the relay."""
    timeout = httpx.Timeout(None, connect=_CONNECT_TIMEOUT_S)
    async with httpx.AsyncClient(timeout=timeout) as c:
        async with c.stream(
            "POST", f"{base}/jobs/stream",
            json=_job_body(messages, model, temperature, max_tokens, scope, circle_id),
            headers=_headers(base),
        ) as resp:
            if resp.status_code != 200:
                body = (await resp.aread()).decode("utf-8", "replace")[:300]
                raise PoolDispatchError(resp.status_code, body)
            async for chunk in openai_chunks(resp, model, coordinator=True):
                yield chunk


async def complete_pool_openai(
    messages: list[dict],
    model: str,
    temperature: float,
    max_tokens: int,
    scope: str = _POOL_SCOPE,
    circle_id: str | None = None,
) -> dict:
    """Dispatch to the coordinator (streamed under the hood) and return one OpenAI
    chat.completion envelope. Works for both pool and circle scopes."""
    return await collect_completion(
        stream_pool_openai(messages, model, temperature, max_tokens, scope, circle_id)
    )


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
