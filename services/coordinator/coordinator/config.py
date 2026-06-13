from __future__ import annotations

import functools

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"

    # Server
    port: int = 8400
    env: str = "development"

    # Sentry — empty string disables SDK init
    sentry_dsn: str = ""

    # Background verification probe loop (canary -> reputation -> escrow
    # release). Set false to stop the coordinator initiating probes (e.g. in
    # CI / single-purpose deployments).
    verification_probe_enabled: bool = True

    # Peer attestation: when true, a device key must already be registered in
    # member_keys to attest (strict allowlist — for multi-member / production).
    # When false (default), an unregistered key is bound trust-on-first-use to
    # whatever member_id it claims. Either way a known key is pinned to its
    # member and can't later claim a different one.
    require_registered_keys: bool = False

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"env must be one of {allowed}, got {v!r}")
        return v


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Call once per process."""
    return Settings()
