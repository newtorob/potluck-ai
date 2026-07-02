from __future__ import annotations

import functools

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_MAGIC_LINK_SECRET = "change-me-generate-a-random-64-char-hex"
_DEFAULT_DISCOURSE_SSO_SECRET = "change-me-generate-another-random-64-char-hex"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"

    # Server
    port: int = 8080
    env: str = "development"

    # Auth
    magic_link_secret: str = _DEFAULT_MAGIC_LINK_SECRET
    magic_link_expiry_minutes: int = 15
    session_expiry_days: int = 30
    app_url: str = "http://localhost:5173"
    # Name of the HTTP-only session cookie set on /auth/callback.
    session_cookie_name: str = "potluck_session"
    # Path appended to app_url to build the magic-link URL. Kept configurable so
    # the delivery transport (a web verify page vs a potluck:// deep link) can
    # change without a code edit.
    magic_link_path: str = "/auth/verify"

    # Email (Resend)
    resend_api_key: str = ""
    from_email: str = "hello@trypotluck.ai"

    # Stripe
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_founding_price_id: str = ""
    stripe_dues_price_id: str = ""

    # Discourse SSO
    discourse_url: str = "https://discourse.trypotluck.ai"
    discourse_sso_secret: str = _DEFAULT_DISCOURSE_SSO_SECRET
    discourse_api_key: str = ""

    # Admin — comma-separated list of email addresses
    admin_emails: str = ""

    # Sentry — empty string disables SDK init
    sentry_dsn: str = ""

    @field_validator("env")
    @classmethod
    def validate_env(cls, v: str) -> str:
        allowed = {"development", "staging", "production"}
        if v not in allowed:
            raise ValueError(f"env must be one of {allowed}, got {v!r}")
        return v

    @model_validator(mode="after")
    def reject_default_secrets_in_production(self) -> "Settings":
        """Hard-fail startup when placeholder secrets are present outside development.

        Both magic_link_secret and discourse_sso_secret are used for HMAC
        signing.  If either matches the known-public placeholder, tokens are
        forgeable by anyone who has read this source file.  We refuse to start
        in staging or production to prevent silent misconfiguration.

        To fix: set MAGIC_LINK_SECRET and DISCOURSE_SSO_SECRET env vars to
        cryptographically random 64-char hex strings before deploying.
        """
        if self.env == "development":
            return self
        insecure: list[str] = []
        if self.magic_link_secret == _DEFAULT_MAGIC_LINK_SECRET:
            insecure.append("MAGIC_LINK_SECRET")
        if self.discourse_sso_secret == _DEFAULT_DISCOURSE_SSO_SECRET:
            insecure.append("DISCOURSE_SSO_SECRET")
        if insecure:
            raise ValueError(
                f"Refusing to start in env={self.env!r} with default placeholder "
                f"secret(s): {', '.join(insecure)}.  "
                "Set each env var to a cryptographically random 64-char hex string."
            )
        return self

    def admin_email_list(self) -> list[str]:
        """Return admin emails as a parsed list, stripping whitespace."""
        return [e.strip() for e in self.admin_emails.split(",") if e.strip()]

    def cookie_secure(self) -> bool:
        """Session cookie Secure flag — off in development so http localhost works."""
        return self.env != "development"

    def cookie_samesite(self) -> str:
        """Session cookie SameSite policy.

        The desktop app is a different site from api.trypotluck.ai, so production
        needs SameSite=None (which requires Secure). Local http dev can't use
        Secure, so fall back to Lax.
        """
        return "none" if self.env != "development" else "lax"


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Call once per process."""
    return Settings()
