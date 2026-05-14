#!/usr/bin/env python3
"""m3-smoke — Phase G milestone 3 acceptance harness.

Drives the full network-routed inference path end-to-end and asserts the
behaviours that mattered when m3 was specced:

  1. The coordinator is reachable and reports at least one online peer
     advertising the requested model (capability-aware routing, task #18).
  2. POST /jobs/stream returns Server-Sent Events the frontend understands:
     OpenAI-compatible delta chunks, a final meta envelope, then [DONE].
  3. The meta envelope reports `served_by: "contributor"` — confirming the
     prompt was actually served by a peer rather than a local fallback,
     and exposing zero identifying fields (manifesto §10.3).
  4. The streamed response contains non-empty content (catches the case
     where the peer accepts the job but produces no tokens before
     hitting finish_reason='stop' for whatever reason).

It is NOT a replacement for the static privacy lint (task #20) or the
in-memory-only invariant lint (task #21). Those guard the code paths;
this harness validates that the code paths actually work end-to-end.

Run from the repo root:
    python3 scripts/dev/m3-smoke.py
    python3 scripts/dev/m3-smoke.py --coordinator-url http://127.0.0.1:8400 \\
            --model qwen2.5:3b-instruct-q4_K_M \\
            --prompt "say hello in five words"

Exits 0 on full success, non-zero with a categorised failure on anything
unexpected. Stdlib-only — runnable from any Python 3.11+ without pip.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator


@dataclass
class SmokeResult:
    ok: bool
    content: str
    output_tokens: int
    duration_ms: int
    finish_reason: str
    meta_served_by: str
    failure_reason: str | None = None


def _http_get_json(url: str, timeout: float = 10.0) -> dict:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _check_fleet(coordinator_url: str, model_slug: str) -> tuple[int, list[str]]:
    """Return (online_peer_count, advertised_model_slugs).

    /transparency/fleet is aggregate-only by design — no peer ids leak
    here, just counts and the union of advertised models. Schema:
        { peers: { online_now: int, ... }, models: [{slug, online_peer_count}], ... }
    """
    fleet = _http_get_json(f"{coordinator_url}/transparency/fleet")
    peers = fleet.get("peers") or {}
    online = int(peers.get("online_now") or 0)
    models = [
        str(m.get("slug"))
        for m in (fleet.get("models") or [])
        if int(m.get("online_peer_count") or 0) > 0
    ]
    return online, models


def _stream_sse(url: str, body: dict, timeout: float = 180.0) -> Iterator[dict]:
    """POST a JSON body to a Server-Sent Events endpoint and yield each
    decoded `data:` line as a parsed dict.

    Stops yielding when [DONE] is seen or the response closes.
    """
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        # urllib's iteration is line-buffered when the server flushes
        # cleanly — which our coordinator does (see X-Accel-Buffering:no
        # in /jobs/stream).
        for raw_line in resp:
            line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
            if not line.startswith("data:"):
                continue
            payload = line[5:].lstrip()
            if payload == "[DONE]":
                return
            try:
                yield json.loads(payload)
            except json.JSONDecodeError:
                # Coordinator should never emit non-JSON, but be defensive.
                continue


def smoke(
    coordinator_url: str,
    model_slug: str,
    prompt: str,
    timeout_s: float,
) -> SmokeResult:
    # ---- Step 1: capability check ---- #
    print(f"→ probing {coordinator_url}/transparency/fleet ...", flush=True)
    try:
        online, models = _check_fleet(coordinator_url, model_slug)
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return SmokeResult(
            ok=False,
            content="",
            output_tokens=0,
            duration_ms=0,
            finish_reason="",
            meta_served_by="",
            failure_reason=f"coordinator unreachable: {e}",
        )

    if online == 0:
        return SmokeResult(
            ok=False,
            content="",
            output_tokens=0,
            duration_ms=0,
            finish_reason="",
            meta_served_by="",
            failure_reason="no contributor peers online",
        )

    if models and model_slug not in models:
        return SmokeResult(
            ok=False,
            content="",
            output_tokens=0,
            duration_ms=0,
            finish_reason="",
            meta_served_by="",
            failure_reason=(
                f"no online peer advertises model '{model_slug}'. "
                f"Online peers serve: {sorted(models)}"
            ),
        )

    print(f"  ok — {online} peer(s) online" + (f", {len(models)} model(s) available" if models else ""), flush=True)

    # ---- Step 2: dispatch + stream ---- #
    print(f"→ POST {coordinator_url}/jobs/stream  model={model_slug!r}  prompt={prompt!r}", flush=True)
    started = time.monotonic()
    content_parts: list[str] = []
    meta: dict | None = None
    error: dict | None = None

    try:
        for event in _stream_sse(
            f"{coordinator_url}/jobs/stream",
            body={"prompt": prompt, "model_slug": model_slug},
            timeout=timeout_s,
        ):
            if "error" in event:
                error = event["error"]
                break
            if "meta" in event:
                meta = event["meta"]
                continue
            choices = event.get("choices") or []
            if choices:
                delta = choices[0].get("delta") or {}
                token = delta.get("content")
                if token:
                    content_parts.append(token)
                    sys.stdout.write(token)
                    sys.stdout.flush()
        if content_parts:
            sys.stdout.write("\n")
            sys.stdout.flush()
    except (urllib.error.URLError, urllib.error.HTTPError) as e:
        return SmokeResult(
            ok=False,
            content="",
            output_tokens=0,
            duration_ms=0,
            finish_reason="",
            meta_served_by="",
            failure_reason=f"stream request failed: {e}",
        )

    elapsed_ms = int((time.monotonic() - started) * 1000)
    content = "".join(content_parts)

    if error is not None:
        return SmokeResult(
            ok=False,
            content=content,
            output_tokens=0,
            duration_ms=elapsed_ms,
            finish_reason="",
            meta_served_by="",
            failure_reason=f"peer reported error: {error.get('category')} — {error.get('message')}",
        )

    # ---- Step 3: assertions ---- #
    if not content.strip():
        return SmokeResult(
            ok=False,
            content=content,
            output_tokens=0,
            duration_ms=elapsed_ms,
            finish_reason="",
            meta_served_by="",
            failure_reason="empty response — peer accepted the job but returned no tokens",
        )

    if meta is None:
        return SmokeResult(
            ok=False,
            content=content,
            output_tokens=0,
            duration_ms=elapsed_ms,
            finish_reason="",
            meta_served_by="",
            failure_reason="stream completed but no meta envelope was emitted (frontend would lose served_by attribution)",
        )

    served_by = str(meta.get("served_by") or "")
    if served_by != "contributor":
        return SmokeResult(
            ok=False,
            content=content,
            output_tokens=int(meta.get("output_tokens") or 0),
            duration_ms=int(meta.get("duration_ms") or elapsed_ms),
            finish_reason=str(meta.get("finish_reason") or ""),
            meta_served_by=served_by,
            failure_reason=f"meta.served_by was {served_by!r}, expected 'contributor'",
        )

    # Belt-and-suspenders: meta must carry NO identifying fields.
    forbidden = {"peer_id", "hostname", "member_id", "conversation_id", "ip"}
    leaks = [k for k in meta.keys() if k.lower() in forbidden]
    if leaks:
        return SmokeResult(
            ok=False,
            content=content,
            output_tokens=int(meta.get("output_tokens") or 0),
            duration_ms=int(meta.get("duration_ms") or elapsed_ms),
            finish_reason=str(meta.get("finish_reason") or ""),
            meta_served_by=served_by,
            failure_reason=f"meta envelope contains identifying fields: {leaks}",
        )

    return SmokeResult(
        ok=True,
        content=content,
        output_tokens=int(meta.get("output_tokens") or 0),
        duration_ms=int(meta.get("duration_ms") or elapsed_ms),
        finish_reason=str(meta.get("finish_reason") or "stop"),
        meta_served_by=served_by,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="End-to-end acceptance smoke for Phase G m3 (network-routed inference).",
    )
    parser.add_argument(
        "--coordinator-url",
        default="http://127.0.0.1:8400",
        help="Coordinator base URL (default: %(default)s).",
    )
    parser.add_argument(
        "--model",
        default="qwen2.5:3b-instruct-q4_K_M",
        help="Model slug to request — must be advertised by at least one online peer.",
    )
    parser.add_argument(
        "--prompt",
        default="Say hello in five words.",
        help="Prompt to send. Keep it short — this is a smoke test, not a benchmark.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180.0,
        help="Per-request timeout in seconds (default: %(default)s).",
    )
    args = parser.parse_args()

    result = smoke(
        coordinator_url=args.coordinator_url.rstrip("/"),
        model_slug=args.model,
        prompt=args.prompt,
        timeout_s=args.timeout,
    )

    print()
    if result.ok:
        print("✓ m3 acceptance: PASS")
        print(f"  served_by:     {result.meta_served_by}")
        print(f"  output_tokens: {result.output_tokens}")
        print(f"  duration_ms:   {result.duration_ms}")
        print(f"  finish_reason: {result.finish_reason}")
        return 0
    else:
        print("✗ m3 acceptance: FAIL")
        print(f"  reason: {result.failure_reason}")
        if result.content:
            print(f"  partial content: {result.content[:200]!r}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
