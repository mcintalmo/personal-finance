"""Tests for the pf CLI."""

import duckdb
import pytest
from typer.testing import CliRunner

from personal_finance.cli import app
from personal_finance.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch, tmp_path):
    """Point the warehouse at a temp path and clear the settings cache."""
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("synth", "init-db", "transform", "ingest", "enrich"):
        assert command in result.output


class TestSynth:
    def test_writes_exports_and_receipts(self, tmp_path):
        out = tmp_path / "synth"
        result = runner.invoke(app, ["synth", "--out", str(out), "--months", "2"])
        assert result.exit_code == 0, result.output
        assert len(list((out / "exports").iterdir())) == 15
        receipts = list((out / "receipts").iterdir())
        assert (out / "receipts" / "manifest.json") in receipts
        assert "export files" in result.output

    def test_seed_determinism(self, tmp_path):
        for name in ("a", "b"):
            runner.invoke(app, ["synth", "--out", str(tmp_path / name), "--months", "1"])
        a = (tmp_path / "a" / "exports" / "chase_checking.csv").read_text()
        b = (tmp_path / "b" / "exports" / "chase_checking.csv").read_text()
        assert a == b


class TestInitDb:
    def test_creates_and_seeds_warehouse(self, tmp_path):
        result = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        assert result.exit_code == 0, result.output
        warehouse = get_settings().data.warehouse_path
        assert warehouse.exists()
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute("select count(*) from categories").fetchone()
        assert count > 0
        assert "categories seeded" in result.output

    def test_is_idempotent(self):
        first = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        second = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        assert first.exit_code == 0 and second.exit_code == 0

    def test_invalid_config_exits_nonzero(self, tmp_path):
        bad = tmp_path / "badcfg"
        bad.mkdir()
        (bad / "taxonomy.yaml").write_text("- name: [unclosed", encoding="utf-8")
        result = runner.invoke(app, ["init-db", "--config-dir", str(bad)])
        assert result.exit_code == 1
        assert "Configuration error" in result.output


class TestTransform:
    def test_requires_initialized_warehouse(self):
        result = runner.invoke(app, ["transform"])
        assert result.exit_code == 1
        assert "pf init-db" in result.output

    @pytest.mark.filterwarnings("ignore")
    def test_builds_after_init_db(self):
        init = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        assert init.exit_code == 0, init.output
        result = runner.invoke(app, ["transform"])
        assert result.exit_code == 0, result.output
        assert "dbt build succeeded" in result.output
        with duckdb.connect(str(get_settings().data.warehouse_path)) as conn:
            (count,) = conn.execute("select count(*) from main_gold.gold_category_paths").fetchone()
        assert count > 0


@pytest.mark.parametrize("command", ["ingest", "enrich"])
def test_stubs_exit_with_pointer_to_plan(command):
    result = runner.invoke(app, [command])
    assert result.exit_code == 2
    assert "not implemented" in result.output
