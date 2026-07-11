"""Application configuration via pydantic-settings.

Settings are loaded from environment variables and .env.local (development only).
See .env.example for the full list of supported variables.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env.local",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",  # ignore unknown env vars
    )

    # ── Application ───────────────────────────────────────
    app_env: Environment = Environment.DEVELOPMENT
    app_debug: bool = False
    app_log_level: str = "INFO"

    @property
    def is_production(self) -> bool:
        return self.app_env == Environment.PRODUCTION

    @property
    def is_development(self) -> bool:
        return self.app_env == Environment.DEVELOPMENT


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance.

    Usage:
        from personal_finance.config import get_settings
        settings = get_settings()
    """
    return Settings()
