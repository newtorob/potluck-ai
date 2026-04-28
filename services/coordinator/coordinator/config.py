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
