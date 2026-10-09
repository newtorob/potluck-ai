"""Release-readiness preflight.

One place that answers "is this coordinator safe to expose?" Every check that
distinguishes a dev convenience from a production-safe posture lives here, so the
answer isn't scattered across middleware warnings and tribal knowledge.

Behaviour:
  * Always runs at startup and logs the full checklist (ok / warn / fail).
  * In `development`, it's advisory only — never blocks the process.
  * In `staging` / `production`, a FAIL on any operator-fixable check makes the
    coordinator refuse to start (fail-fast). The operator controls these via
    env / config, so blocking is actionable, not a footgun.

FAIL vs WARN:
  * FAIL  — an insecure default the operator can fix right now (no auth token,
            trust-on-first-use keys, default DB credentials). Blocks prod boot.
  * WARN  — a known roadmap gap with no config remedy yet (member identity is
            still the dev stub; per-session auth is v0.5). Surfaced loudly on
            every boot but does NOT block, because there is nothing to flip.

Scope: the network-facing coordinator only. Desktop-side readiness (sidecar
token, code-signed sidecar binaries, gateway exposure) is a separate surface
and a tracked follow-on.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

import structlog

from coordinator.config import Settings
from coordinator.middleware.session_auth import session_auth_enabled

logger = structlog.get_logger(__name__)

Status = Literal["ok", "warn", "fail"]

# Default dev credentials baked into config.py's database_url default. If the
# running DB URL still carries these, it's the throwaway docker-compose database.
_DEFAULT_DB_CREDENTIALS = "potluck:potluck@localhost"

# Minimum length for the coordinator shared secret to count as "strong". Below
# this it's set (so it doesn't block) but flagged — the remedy text promises a
# strong secret, so a 1-char token shouldn't read as fully ok.
_MIN_TOKEN_LEN = 16


@dataclass(frozen=True)
class Check:
    """One readiness check. `status` is the assessment; `blocking` marks whether
    a FAIL should refuse prod startup (True) or only warn (False)."""

    name: str
    status: Status
    detail: str
    remedy: str
    blocking: bool = True


def run_preflight(settings: Settings) -> list[Check]:
    """Evaluate every readiness check against the current config + environment.

    Pure: takes settings, reads env, returns the checklist. No logging or
    raising here so it's trivially testable; app startup decides what to do
    with the result (see enforce_preflight)."""
    checks: list[Check] = []
    # Several checks only matter once the coordinator faces a network.
    prod = settings.env in {"staging", "production"}

    # --- Coordinator auth token (operator-fixable) ---------------------------
    # Read from os.environ (not Settings) deliberately: this matches exactly what
    # middleware/auth.coordinator_token_matches enforces, so the check can't
    # disagree with the gate it's reporting on. Covers HTTP + both WS handshakes
    # (they share this one secret).
    token = os.environ.get("POTLUCK_COORDINATOR_TOKEN", "").strip()
    if not token:
        token_status: Status = "fail"
        token_detail = (
            "POTLUCK_COORDINATOR_TOKEN is unset — the coordinator is OPEN "
            "(any caller can submit jobs and connect peers)."
        )
    elif len(token) < _MIN_TOKEN_LEN:
        token_status = "warn"
        token_detail = (
            f"POTLUCK_COORDINATOR_TOKEN is set but short (<{_MIN_TOKEN_LEN} chars); "
            "a weak shared secret is easy to guess or brute-force."
        )
    else:
        token_status = "ok"
        token_detail = "POTLUCK_COORDINATOR_TOKEN is set; HTTP + WS callers must present it."
    checks.append(
        Check(
            name="coordinator_auth_token",
            status=token_status,
            detail=token_detail,
            remedy="Set POTLUCK_COORDINATOR_TOKEN to a strong shared secret (>=16 chars).",
        )
    )

    # --- Peer key allowlist (operator-fixable) -------------------------------
    checks.append(
        Check(
            name="require_registered_keys",
            status="ok" if settings.require_registered_keys else "fail",
            detail=(
                "Device keys must be pre-registered to attest."
                if settings.require_registered_keys
                else "require_registered_keys is off — an unregistered key is "
                "trust-on-first-use bound to whatever member_id it claims."
            ),
            remedy="Set require_registered_keys=true (REQUIRE_REGISTERED_KEYS=true).",
        )
    )

    # --- Release allowlist (advisory until releases are signed) --------------
    checks.append(
        Check(
            name="require_registered_release",
            status="ok" if settings.require_registered_release else "warn",
            detail=(
                "Unknown binary_hash values are rejected at attestation."
                if settings.require_registered_release
                else "require_registered_release is off — a peer with an "
                "unregistered binary_hash is accepted and logged "
                "(peer_release_unregistered). Expected until releases are signed."
            ),
            remedy=(
                "Sign the shipped build (scripts/dev/sign-contributor-release.py) "
                "and set REQUIRE_REGISTERED_RELEASE=true."
            ),
            blocking=False,
        )
    )

    # --- Database credentials (operator-fixable) -----------------------------
    using_default_db = _DEFAULT_DB_CREDENTIALS in settings.database_url
    checks.append(
        Check(
            name="database_credentials",
            status="fail" if using_default_db else "ok",
            detail=(
                "DATABASE_URL still uses the default dev credentials "
                "(potluck:potluck@localhost)."
                if using_default_db
                else "DATABASE_URL is not the default dev credentials."
            ),
            remedy="Point DATABASE_URL at the production database with unique credentials.",
        )
    )

    # --- Database transport encryption (operator-fixable, fail-closed) -------
    # The DB carries the credit ledger and member keys, so in a network env the
    # link must be encrypted. Fail-closed: in staging/production, block unless
    # either the URL requests TLS OR the operator explicitly acknowledges that
    # the transport is already encrypted at the network layer (private network /
    # VPN / WireGuard, e.g. Fly .internal) via POTLUCK_DB_LINK_ENCRYPTED=1 — we
    # can't detect that from the URL, so we require an auditable acknowledgement
    # rather than silently accepting a plaintext link.
    #
    # Detection is asyncpg-accurate: this dialect (postgresql+asyncpg) honors the
    # `ssl=` query arg, NOT libpq's `sslmode=`. A URL carrying only `sslmode=` is
    # a trap — the operator thinks TLS is on but asyncpg ignores it — so it does
    # NOT count as TLS and gets a pointed message.
    db_url_l = settings.database_url.lower()
    tls_requested = any(
        t in db_url_l
        for t in ("ssl=require", "ssl=true", "ssl=verify-full", "ssl=verify-ca", "ssl=prefer")
    )
    link_encrypted_ack = os.environ.get("POTLUCK_DB_LINK_ENCRYPTED", "").strip() == "1"
    sslmode_trap = ("sslmode=" in db_url_l) and not tls_requested

    if tls_requested:
        db_tls_status: Status = "ok"
        db_tls_detail = "DATABASE_URL requests TLS via ssl= (honored by asyncpg)."
    elif link_encrypted_ack:
        db_tls_status = "ok"
        db_tls_detail = (
            "DATABASE_URL does not request app-level TLS, but "
            "POTLUCK_DB_LINK_ENCRYPTED=1 asserts the transport is encrypted at the "
            "network layer."
        )
    elif not prod:
        # Advisory only in development — keep local logs clean (nobody runs a
        # local dev Postgres over TLS).
        db_tls_status = "ok"
        db_tls_detail = "Local development DB link; TLS not required in this env."
    else:
        db_tls_status = "fail"
        db_tls_detail = (
            "DATABASE_URL sets sslmode= but this coordinator uses asyncpg, which "
            "ignores sslmode (use ssl=require). The DB link is NOT encrypted."
            if sslmode_trap
            else "DATABASE_URL does not request TLS (no ssl=require) — the credit "
            "ledger and member keys would travel unencrypted."
        )
    checks.append(
        Check(
            name="database_tls",
            status=db_tls_status,
            detail=db_tls_detail,
            remedy="Add ssl=require to DATABASE_URL (asyncpg honors ssl=, not "
            "sslmode=). If the link is already encrypted at the network layer, set "
            "POTLUCK_DB_LINK_ENCRYPTED=1 to acknowledge that instead.",
            blocking=True,
        )
    )

    # --- CORS allowlist -----------------------------------------------------
    # The resolved allowlist drops the Vite dev origins outside development, so
    # the clean prod posture passes. This still warns if an operator re-introduces
    # a localhost origin via CORS_ORIGINS in a network env.
    try:
        from coordinator.app import resolve_cors_origins

        origins = resolve_cors_origins(settings)
        dev_origins = [o for o in origins if "localhost" in o or "127.0.0.1" in o]
    except Exception:  # pragma: no cover - app import should always succeed at runtime
        dev_origins = []
    checks.append(
        Check(
            name="cors_dev_origins",
            status="warn" if (prod and dev_origins) else "ok",
            detail=(
                f"CORS allowlist includes localhost dev origins {dev_origins} in a "
                "network env (configured via CORS_ORIGINS)."
                if (prod and dev_origins)
                else "CORS allowlist carries no localhost dev origins for this env."
            ),
            remedy="Remove localhost origins from CORS_ORIGINS in staging/production "
            "(app://. and file:// remain for the packaged app).",
            blocking=False,
        )
    )

    # --- Error reporting (advisory) ------------------------------------------
    checks.append(
        Check(
            name="error_reporting",
            status="ok" if (settings.sentry_dsn or not prod) else "warn",
            detail=(
                "Sentry DSN configured; crashes are reported."
                if settings.sentry_dsn
                else "No Sentry DSN — a network deployment has no crash/error visibility."
            ),
            remedy="Set SENTRY_DSN for staging/production observability.",
            blocking=False,
        )
    )

    # --- Credit verification loop (advisory) ---------------------------------
    checks.append(
        Check(
            name="verification_probe",
            status="ok" if (settings.verification_probe_enabled or not prod) else "warn",
            detail=(
                "Credit-verification probe loop is enabled."
                if settings.verification_probe_enabled
                else "verification_probe_enabled is off — escrowed credits are never "
                "released (canary -> reputation -> escrow loop is not running)."
            ),
            remedy="Set verification_probe_enabled=true in staging/production.",
            blocking=False,
        )
    )

    # --- API docs exposure (already env-gated) -------------------------------
    docs_open = settings.env == "development"
    checks.append(
        Check(
            name="api_docs_exposed",
            status="ok" if not docs_open else "warn",
            detail=(
                "/docs and /redoc are disabled outside development."
                if not docs_open
                else "/docs and /redoc are served (development only)."
            ),
            remedy="No action; docs are auto-disabled when env is staging/production.",
            blocking=False,
        )
    )

    # --- Member identity -----------------------------------------------------
    # Per-session member auth exists (POTLUCK_SESSION_AUTH_ENABLED); when it is
    # off, every requester resolves to the dev fixture STUB_MEMBER_ID, so credits
    # and jobs are not bound to real members. We warn rather than block so a
    # single-tenant / household coordinator can still run without the flag.
    session_auth = session_auth_enabled()
    checks.append(
        Check(
            name="member_session_auth",
            status="ok" if session_auth else "warn",
            detail=(
                "Per-session member authentication is enabled."
                if session_auth
                else "Member identity is the dev STUB_MEMBER_ID — there is no "
                "per-session auth, so credits and jobs are not bound to real members."
            ),
            remedy="Set POTLUCK_SESSION_AUTH_ENABLED=1 (needs DATABASE_URL "
            "pointed at the cloud API's Postgres so the sessions table is "
            "readable).",
            blocking=False,
        )
    )

    # --- Direct circle links: development-only binding flag ------------------
    # circle_link_bind_without_session trusts a shared-token engine's claim of
    # which mesh node it is. The claim is only as good as the shared token, so
    # in a network env the flag must be off; the node is then bound only for
    # engines that signed in with a member session.
    unsafe_bind = settings.circle_links_enabled and settings.circle_link_bind_without_session
    checks.append(
        Check(
            name="circle_link_bind_without_session",
            status="fail" if (prod and unsafe_bind) else "ok",
            detail=(
                "circle_link_bind_without_session is on with direct circle links "
                "enabled: an engine's mesh node claim is trusted without a member session."
                if (prod and unsafe_bind)
                else "Direct circle links bind an engine to a mesh node only for "
                "member-session engines (or links are off)."
                if not unsafe_bind
                else "circle_link_bind_without_session is on (development only)."
            ),
            remedy="Unset POTLUCK_CIRCLE_LINK_BIND_WITHOUT_SESSION in staging/production.",
        )
    )

    return checks


def enforce_preflight(settings: Settings) -> list[Check]:
    """Run the checklist, log every line, and in staging/production refuse to
    start when a blocking check fails. Returns the checks (for tests / callers).

    Raises RuntimeError in staging/production if any blocking check FAILs.
    """
    checks = run_preflight(settings)

    for c in checks:
        log = logger.info if c.status == "ok" else (
            logger.warning if c.status == "warn" else logger.error
        )
        log(
            "preflight_check",
            check=c.name,
            status=c.status,
            env=settings.env,
            detail=c.detail,
            remedy=c.remedy,
        )

    blocking_failures = [c for c in checks if c.status == "fail" and c.blocking]
    warnings = [c for c in checks if c.status == "warn"]

    if settings.env in {"staging", "production"} and blocking_failures:
        names = ", ".join(c.name for c in blocking_failures)
        logger.error(
            "preflight_failed",
            env=settings.env,
            blocking_failures=[c.name for c in blocking_failures],
        )
        raise RuntimeError(
            f"Release-readiness preflight failed in env={settings.env}: {names}. "
            f"Refusing to start. Fix the failing checks (see logs) or run in "
            f"env=development for local work."
        )

    if blocking_failures or warnings:
        logger.warning(
            "preflight_passed_with_findings",
            env=settings.env,
            failures=[c.name for c in blocking_failures],
            warnings=[c.name for c in warnings],
        )
    else:
        logger.info("preflight_clean", env=settings.env)

    return checks
