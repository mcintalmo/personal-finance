"""Tests for personal_finance.config."""

from personal_finance.config import Environment, Settings, get_settings


def test_default_settings() -> None:
    """Settings should load with defaults when no .env.local is present."""
    settings = Settings()
    assert settings.app_env == Environment.DEVELOPMENT
    assert settings.app_debug is False


def test_is_development() -> None:
    settings = Settings(app_env="development")
    assert settings.is_development is True
    assert settings.is_production is False


def test_is_production() -> None:
    settings = Settings(app_env="production")
    assert settings.is_production is True
    assert settings.is_development is False


def test_get_settings_returns_cached_instance() -> None:
    """get_settings() should return the same object on repeated calls."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
