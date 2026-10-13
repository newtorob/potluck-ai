"""Application-level configuration via pydantic-settings.

Reads from environment variables with POTLUCK_ prefix.
"""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LocalSettings(BaseSettings):
    """Runtime configuration for the local sidecar.

    All values can be overridden via environment variables:
      POTLUCK_PORT - TCP port uvicorn listens on (default 8321)
      POTLUCK_DEV - Enable debug mode / Swagger UI (set to "1")
      POTLUCK_DATA_DIR - Base data directory (default ~/.potluck)
      POTLUCK_CLOUD_API_BASE - Base URL of the Potluck cloud API, used by
                                 catalog_feed's background model-catalog
                                 refresh (default https://api.trypotluck.ai)
    """

    model_config = SettingsConfigDict(
        env_prefix="POTLUCK_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    port: int = Field(default=8321, description="Port for the local API server")
    dev: bool = Field(default=False, description="Enable development mode (Swagger UI, debug logging)")
    data_dir: Path = Field(
        default=Path.home() / ".potluck",
        description="Root directory for all Potluck data",
    )
    cloud_api_base: str = Field(
        default="https://api.trypotluck.ai",
        description="Base URL of the Potluck cloud API (model-catalog feed, etc.)",
    )

    @property
    def models_dir(self) -> Path:
        return self.data_dir / "models"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.json"


def get_local_settings() -> LocalSettings:
    return LocalSettings()
