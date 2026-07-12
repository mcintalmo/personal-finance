"""Application configuration via pydantic-settings.

Settings are loaded from environment variables and .env.local (development only).
See .env.example for the full list of supported variables.

Settings are organized into nested groups (``settings.app.debug``, not
``settings.app_debug``). Only the top-level :class:`Settings` reads the
environment; groups are plain models, addressed in env vars by prefix:
``APP_DEBUG=true`` sets ``settings.app.debug``.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AppSettings(BaseModel):
    """Core application settings (``settings.app.*``)."""

    env: Environment = Environment.DEVELOPMENT
    debug: bool = False
    log_level: str = "INFO"


class DataSettings(BaseModel):
    """Data platform settings (``settings.data.*``).

    ``warehouse_path`` is shared with dbt: transform/profiles.yml reads the
    same DATA_WAREHOUSE_PATH environment variable.
    """

    warehouse_path: Path = Path("data/warehouse.duckdb")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # ignore unknown env vars
        env_nested_delimiter="_",
        env_nested_max_split=1,  # APP_LOG_LEVEL -> app.log_level (split once, keep field underscores)
    )

    # ── Application ───────────────────────────────────────
    app: AppSettings = Field(default_factory=AppSettings)

    # ── Data platform ─────────────────────────────────────
    data: DataSettings = Field(default_factory=DataSettings)

    # ── Paths ─────────────────────────────────────────────
    config_dir: Path = Path("config")  # user-editable YAML config (see user_config.py)

    @property
    def is_production(self) -> bool:
        return self.app.env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.app.env == Environment.DEVELOPMENT


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Usage:
        from personal_finance.config import get_settings
        settings = get_settings()
    """
    return Settings()
