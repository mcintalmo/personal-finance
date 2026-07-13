"""End-to-end test of the dbt medallion skeleton.

Seeds a temporary warehouse from the example taxonomy, then runs ``dbt build``
programmatically (models + data tests). Because this runs under pytest, dbt's
tests are wired into CI with no extra workflow step: if a dbt data test fails,
CI fails.
"""

import warnings
from pathlib import Path

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.seed import seed_categories
from personal_finance.user_config import load_user_config

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_CONFIG_DIR = REPO_ROOT / "config" / "examples"


@pytest.fixture(scope="module")
def built_warehouse(tmp_path_factory):
    """A seeded warehouse on which `dbt build` has run once.

    Env-var handling and warning suppression are scoped to the dbt invocation
    only: a developer's own DATA_WAREHOUSE_PATH is restored afterwards, and
    warnings from this project's code (schema creation, seeding) still fail
    the run under the global ``filterwarnings = error`` regime — only dbt's
    dependency-stack noise is silenced.
    """
    warehouse = tmp_path_factory.mktemp("wh") / "warehouse.duckdb"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                ]
            )
    finally:
        monkeypatch.undo()
    return warehouse, config, result


class TestDbtBuild:
    def test_build_succeeds_including_data_tests(self, built_warehouse):
        _, _, result = built_warehouse
        assert result.success, f"dbt build failed: {result.exception}"

    def test_silver_matches_seeded_categories(self, built_warehouse):
        warehouse, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute("select count(*) from main_silver.silver_categories").fetchone()
        assert count == len(config.category_paths())

    def test_gold_paths_match_taxonomy_paths(self, built_warehouse):
        warehouse, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            paths = {
                path
                for (path,) in conn.execute(
                    "select path from main_gold.gold_category_paths"
                ).fetchall()
            }
        assert paths == config.category_paths()

    def test_gold_depth_consistent_with_path(self, built_warehouse):
        warehouse, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute("select path, depth from main_gold.gold_category_paths").fetchall()
        for path, depth in rows:
            assert depth == path.count("/")
