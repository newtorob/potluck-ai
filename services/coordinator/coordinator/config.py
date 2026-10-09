from __future__ import annotations

import functools

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Bounds for a direct circle link's lifetime. The mesh sidecar accepts 5..1800
# seconds in a circle_peer_add, so the configured value is clamped into that
# range instead of failing: a typo in an optional setting must not stop the
# coordinator, which would take every household's mesh down with it.
CIRCLE_LINK_TTL_MIN_S = 5
CIRCLE_LINK_TTL_MAX_S = 1800


def _lenient_bool(value: object, default: bool = False) -> bool:
    """Read a switch the way an operator writes it. Anything that is not a
    clear yes or no gives the default, so a stray value cannot stop boot."""
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _lenient_int(value: object, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


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

    # ---- Direct circle links (services/circle_links.py) -------------------
    # Kill switch. OFF by default: every link endpoint answers 503
    # links_disabled, attestation never reports circle_links, the sweeper does
    # not run, and no sidecar is ever sent a circle_peer_* message. Nothing
    # changes for any existing client until this is switched on.
    circle_links_enabled: bool = Field(
        default=False, validation_alias="POTLUCK_CIRCLE_LINKS_ENABLED"
    )
    # Transport policy. OFF (default) is relay-only between members: the
    # circle_peer_add messages carry no public or LAN address and
    # direct_window_ms 0, so the two sidecars meet over the DERP relay and
    # neither member learns the other's address. ON fills the endpoints in
    # (LAN candidates only when both nodes report the same public address).
    circle_link_direct: bool = Field(
        default=False, validation_alias="POTLUCK_CIRCLE_LINK_DIRECT"
    )
    # Lifetime of one link, and so the time between renewals (renew_after_s is
    # half of it). Clamped to CIRCLE_LINK_TTL_MIN_S..CIRCLE_LINK_TTL_MAX_S.
    circle_link_ttl_s: int = Field(
        default=600, validation_alias="POTLUCK_CIRCLE_LINK_TTL_S"
    )
    # How often the sweeper ends lapsed links and removes old rows.
    circle_link_sweep_interval_s: int = Field(
        default=15, validation_alias="POTLUCK_CIRCLE_LINK_SWEEP_INTERVAL_S"
    )
    # Development and the harness only. A shared-token engine names its own
    # member, so its mesh_node_id claim is not trusted unless the peer signed
    # in with a member session. Refused in production by the preflight.
    circle_link_bind_without_session: bool = Field(
        default=False, validation_alias="POTLUCK_CIRCLE_LINK_BIND_WITHOUT_SESSION"
    )

    @field_validator(
        "circle_links_enabled",
        "circle_link_direct",
        "circle_link_bind_without_session",
        mode="before",
    )
    @classmethod
    def _switches_are_lenient(cls, v: object) -> bool:
        return _lenient_bool(v)

    @field_validator("circle_link_ttl_s", mode="before")
    @classmethod
    def _clamp_link_ttl(cls, v: object) -> int:
        ttl = _lenient_int(v, 600)
        return max(CIRCLE_LINK_TTL_MIN_S, min(CIRCLE_LINK_TTL_MAX_S, ttl))

    @field_validator("circle_link_sweep_interval_s", mode="before")
    @classmethod
    def _clamp_sweep_interval(cls, v: object) -> int:
        return max(1, min(300, _lenient_int(v, 15)))

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
