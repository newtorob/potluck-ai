from __future__ import annotations

import functools

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", case_sensitive=False, extra="ignore")

    # Database
    database_url: str = "postgresql+asyncpg://potluck:potluck@localhost:5432/potluck"

    # Server
    port: int = 8080
    env: str = "development"

    # Auth
    magic_link_secret: str = "change-me-generate-a-random-64-char-hex"
    magic_link_expiry_minutes: int = 15
    session_expiry_days: int = 30
    app_url: str = "http://localhost:5173"

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
    discourse_sso_secret: str = "change-me-generate-another-random-64-char-hex"
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

    def admin_email_list(self) -> list[str]:
        """Return admin emails as a parsed list, stripping whitespace."""
        return [e.strip() for e in self.admin_emails.split(",") if e.strip()]


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings instance. Call once per process."""
    return Settings()
