"""Shared pytest fixtures for personal-finance.

Add fixtures here that are used across multiple test modules.
Fixtures that are only used in one module should live in that module's file.
"""

import os

import pytest

from personal_finance.config import AppSettings, Environment, Settings


@pytest.fixture(scope="session", autouse=True)
def _isolated_dbt_artifacts(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Give each xdist worker its own dbt target and log directory.

    dbt defaults both to `transform/target` and `transform/logs`, shared by
    every invocation in the repo. That is fine serially, but the suite now
    runs with `-n auto --dist loadfile`, so test_dbt.py, test_api.py and
    test_cli.py each drive dbt from a *different process at the same time* —
    all writing the same compiled SQL, run_results.json, and (worst) the
    partial_parse.msgpack manifest that dbt reads back on the next run. That
    is a corruption race whose symptom would be an occasional inexplicable
    parse failure on an unrelated test, which is exactly the kind of flake
    nobody can reproduce.

    Per *worker* rather than per test is enough: within one worker dbt runs
    serially. Running without xdist, there is one worker and this is a no-op
    beyond relocating the artifacts out of the repo.
    """
    worker = os.environ.get("PYTEST_XDIST_WORKER", "main")
    root = tmp_path_factory.getbasetemp() / f"dbt-{worker}"
    os.environ["DBT_TARGET_PATH"] = str(root / "target")
    os.environ["DBT_LOG_PATH"] = str(root / "logs")


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Return test settings with safe defaults."""
    return Settings(
        app=AppSettings(
            env=Environment.DEVELOPMENT,
            debug=True,
            log_level="WARNING",
        )
    )
