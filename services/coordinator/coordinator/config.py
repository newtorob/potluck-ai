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

    # Extra CORS origins (comma-separated) the coordinator should accept beyond
    # the always-on packaged-app origins. The Vite dev origins are added
    # automatically in development; set this for any production web client.
    cors_origins: str = ""

    # Sentry — empty string disables SDK init
    sentry_dsn: str = ""

    # Background verification probe loop (canary -> reputation -> escrow
    # release). Set false to stop the coordinator initiating probes (e.g. in
    # CI / single-purpose deployments).
    verification_probe_enabled: bool = True

    # Background peer-liveness reaper (services/peer_reaper.py): periodically
    # drains + unregisters peers whose heartbeat has gone stale (no ping on
    # /peers/connect means a black-holed peer never fires WebSocketDisconnect
    # on its own). Set false only for tests/tooling that want full manual
    # control of the in-memory peer registry.
    peer_reaper_enabled: bool = True

    # Peer attestation: when true, a device key must already be registered in
    # member_keys to attest (strict allowlist — for multi-member / production).
    # When false (default), an unregistered key is bound trust-on-first-use to
    # whatever member_id it claims. Either way a known key is pinned to its
    # member and can't later claim a different one.
    require_registered_keys: bool = False

    # Peer attestation: when true, a peer whose binary_hash is not in
    # contributor_releases is rejected (the original behaviour). When false
    # (default), an unknown hash is accepted, logged as
    # peer_release_unregistered, and recorded on the peer's heartbeat row as
    # before. Advisory by default for dev and tests. Production sets
    # REQUIRE_REGISTERED_RELEASE=true in fly.toml, keeping the original
    # strict gate until a signed desktop build is registered; preflight warns
    # in production if it is ever off.
    require_registered_release: bool = False

    # DERP-token proof-of-possession (POST /mesh/derp-token). When a caller
    # presents the OPTIONAL `nonce` + `signature` fields, the coordinator
    # always verifies them (mismatch -> 401) regardless of this flag. This
    # flag only controls what happens when they're ABSENT:
    #   * False (default) — legacy behaviour: possession of the (non-secret)
    #     wg_public_key alone is accepted, same as before this hardening
    #     landed. Required while the Go mesh client (coordapi/client.go)
    #     doesn't yet sign the challenge — flipping this on before that
    #     ships would lock out every currently-enrolled node.
    #   * True — PoP becomes mandatory: a request with no signature is
    #     rejected. Only safe to enable once (a) the Go client signs by
    #     default and (b) every enrolled mesh_nodes row has a
    #     signing_public_key (fresh enroll, or a backfill/re-enroll pass).
    derp_pop_required: bool = False

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
