"""Release-readiness preflight tests.

Covers the three behaviours that matter: development never blocks, production
refuses to start on operator-fixable insecure defaults, and a fully remediated
production config starts cleanly. Also pins FAIL-vs-WARN classification so the
roadmap gap (stub member auth) can't silently become a hard block.
"""
from __future__ import annotations

import pytest

from coordinator.config import Settings
from coordinator.services.preflight import enforce_preflight, run_preflight

# The default dev DB URL (mirrors config.py's default). Hardcoded so a stray
# local .env with DATABASE_URL set can't change what "insecure" means here.
_DEFAULT_DB = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"
# A production-grade DB URL (not the default dev creds).
_PROD_DB = "postgresql+asyncpg://app:s3cret@db.internal:5432/potluck"
# A token that clears the strength floor (>=16 chars).
_STRONG_TOKEN = "coordinator-secret-0123456789"


def _settings(env: str, *, registered_keys: bool, db_url: str) -> Settings:
    return Settings(env=env, require_registered_keys=registered_keys, database_url=db_url)


def _insecure(env: str) -> Settings:
    # Default dev posture: default DB creds + TOFU keys.
    return _settings(env, registered_keys=False, db_url=_DEFAULT_DB)


def _remediated(env: str) -> Settings:
    return _settings(env, registered_keys=True, db_url=_PROD_DB)


def _by_name(checks):
    return {c.name: c for c in checks}


def test_development_never_blocks_even_when_insecure(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    # Must not raise in development regardless of insecure defaults.
    checks = enforce_preflight(_insecure("development"))
    names = _by_name(checks)
    assert names["coordinator_auth_token"].status == "fail"
    assert names["require_registered_keys"].status == "fail"
    assert names["database_credentials"].status == "fail"


@pytest.mark.parametrize("env", ["staging", "production"])
def test_production_refuses_to_start_on_open_coordinator(monkeypatch, env):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    with pytest.raises(RuntimeError) as exc:
        enforce_preflight(_insecure(env))
    msg = str(exc.value)
    assert "coordinator_auth_token" in msg
    assert "require_registered_keys" in msg
    assert "database_credentials" in msg


@pytest.mark.parametrize("env", ["staging", "production"])
def test_production_starts_clean_when_remediated(monkeypatch, env):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _STRONG_TOKEN)
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    # Should not raise: all BLOCKING checks pass. Non-blocking WARNs (db TLS,
    # CORS dev origins, no Sentry, stub auth) are allowed and don't block.
    checks = enforce_preflight(_remediated(env))
    names = _by_name(checks)
    assert names["coordinator_auth_token"].status == "ok"
    assert names["require_registered_keys"].status == "ok"
    assert names["database_credentials"].status == "ok"


def test_weak_token_warns_but_does_not_block(monkeypatch):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "short")  # < 16 chars
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    # Token is set (so not a blocking FAIL) but weak -> warn; prod still starts.
    checks = enforce_preflight(_remediated("production"))
    tok = _by_name(checks)["coordinator_auth_token"]
    assert tok.status == "warn"


def test_permissive_cors_and_advisories_warn_in_prod_not_block(monkeypatch):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", _STRONG_TOKEN)
    names = _by_name(run_preflight(_remediated("production")))
    # CORS dev origins present in the hardcoded allowlist -> warn in prod.
    assert names["cors_dev_origins"].status == "warn"
    # _PROD_DB has no TLS param, _remediated has no Sentry DSN -> advisory warns.
    assert names["database_tls"].status == "warn"
    assert names["error_reporting"].status == "warn"
    # All advisories are non-blocking so they never refuse startup.
    for n in ("cors_dev_origins", "database_tls", "error_reporting", "verification_probe"):
        assert names[n].blocking is False


def test_advisories_are_ok_in_development(monkeypatch):
    monkeypatch.delenv("POTLUCK_COORDINATOR_TOKEN", raising=False)
    names = _by_name(run_preflight(_insecure("development")))
    # Network-only advisories don't fire in development.
    for n in ("cors_dev_origins", "database_tls", "error_reporting", "verification_probe"):
        assert names[n].status == "ok"


def test_member_auth_is_warn_not_blocking(monkeypatch):
    """The stub-member-auth gap must stay a non-blocking WARN — blocking it would
    make staging/production unstartable with no config remedy."""
    monkeypatch.delenv("POTLUCK_SESSION_AUTH_ENABLED", raising=False)
    check = _by_name(run_preflight(_remediated("production")))["member_session_auth"]
    assert check.status == "warn"
    assert check.blocking is False


def test_member_auth_ok_when_session_auth_enabled(monkeypatch):
    monkeypatch.setenv("POTLUCK_SESSION_AUTH_ENABLED", "1")
    check = _by_name(run_preflight(_remediated("production")))["member_session_auth"]
    assert check.status == "ok"


def test_docs_exposure_is_warn_in_dev_ok_in_prod(monkeypatch):
    monkeypatch.setenv("POTLUCK_COORDINATOR_TOKEN", "x")
    dev = _by_name(run_preflight(_remediated("development")))["api_docs_exposed"]
    prod = _by_name(run_preflight(_remediated("production")))["api_docs_exposed"]
    assert dev.status == "warn" and dev.blocking is False
    assert prod.status == "ok"
