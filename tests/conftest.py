"""Shared pytest fixtures for personal-finance.

Add fixtures here that are used across multiple test modules.
Fixtures that are only used in one module should live in that module's file.
"""

import pytest

from personal_finance.config import Settings


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Return test settings with safe defaults."""
    return Settings(
        app_env="development",
        app_debug=True,
        app_log_level="WARNING",
    )
