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

    # --- Database transport encryption (advisory) ----------------------------
    # WARN, not FAIL: some deployments run Postgres over a private encrypted
    # network (e.g. Fly .internal over WireGuard), where app-level TLS is
    # redundant. We can't tell from here, so surface it rather than block.
    db_url_l = settings.database_url.lower()
    db_tls = any(t in db_url_l for t in ("ssl=require", "sslmode=require", "ssl=true"))
    checks.append(
        Check(
            name="database_tls",
            status="ok" if (db_tls or not prod) else "warn",
            detail=(
                "Database connection requests TLS."
                if db_tls
                else "DATABASE_URL does not request TLS (no ssl/sslmode=require). "
                "Acceptable only if the DB link is already encrypted (private network/VPN)."
            ),
            remedy="Add ssl=require (asyncpg) to DATABASE_URL unless the link is "
            "already encrypted at the network layer.",
            blocking=False,
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

    # --- Member identity (roadmap gap, no config remedy yet) -----------------
    # Every requester is currently the dev fixture STUB_MEMBER_ID; there is no
    # per-session member auth. This is the headline pre-launch blocker, but
    # there is no flag to flip, so it warns loudly rather than blocking boot.
    session_auth = os.environ.get("POTLUCK_SESSION_AUTH_ENABLED", "").strip() == "1"
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
            remedy="Implement signed-session member auth (v0.5), then set "
            "POTLUCK_SESSION_AUTH_ENABLED=1.",
            blocking=False,
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
