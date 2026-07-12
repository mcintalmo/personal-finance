"""Tests for personal_finance.config."""

from pathlib import Path

from personal_finance.config import AppSettings, Environment, Settings, get_settings


def test_default_settings() -> None:
    """Settings should load with defaults when no .env.local is present."""
    settings = Settings()
    assert settings.app.env == Environment.DEVELOPMENT
    assert settings.app.debug is False
    assert settings.config_dir == Path("config")


def test_is_development() -> None:
    settings = Settings(app=AppSettings(env=Environment.DEVELOPMENT))
    assert settings.is_development is True
    assert settings.is_production is False


def test_is_production() -> None:
    settings = Settings(app=AppSettings(env=Environment.PRODUCTION))
    assert settings.is_production is True
    assert settings.is_development is False


def test_nested_env_var_overrides(monkeypatch) -> None:
    """APP_* environment variables reach the nested app group."""
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_DEBUG", "true")
    monkeypatch.setenv("APP_LOG_LEVEL", "WARNING")
    settings = Settings()
    assert settings.app.env == Environment.PRODUCTION
    assert settings.app.debug is True
    assert settings.app.log_level == "WARNING"


def test_get_settings_returns_cached_instance() -> None:
    """get_settings() should return the same object on repeated calls."""
    s1 = get_settings()
    s2 = get_settings()
    assert s1 is s2
