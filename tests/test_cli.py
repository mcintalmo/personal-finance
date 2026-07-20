"""Tests for the pf CLI."""

import duckdb
import pytest
from typer.testing import CliRunner

from personal_finance.cli import app
from personal_finance.config import get_settings

runner = CliRunner()


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch, tmp_path):
    """Point the warehouse and bronze at temp paths and clear the settings cache."""
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(tmp_path / "bronze"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def test_help_lists_commands():
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in ("synth", "init-db", "transform", "ingest", "watch", "deposit", "enrich"):
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
            result = runner.invoke(app, ["synth", "--out", str(tmp_path / name), "--months", "1"])
            assert result.exit_code == 0, result.output
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
            (categories,) = conn.execute("select count(*) from categories").fetchone()
            (rules,) = conn.execute("select count(*) from rules").fetchone()
        assert categories > 0
        assert rules > 0
        assert "categories" in result.output
        assert "rules seeded" in result.output

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

    def test_requires_ingested_bronze(self):
        init = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        assert init.exit_code == 0, init.output
        result = runner.invoke(app, ["transform"])
        assert result.exit_code == 1
        assert "No ingested data" in result.output

    @pytest.mark.filterwarnings("ignore")
    def test_builds_after_init_db_and_ingest(self, tmp_path):
        init = runner.invoke(app, ["init-db", "--config-dir", "config/examples"])
        assert init.exit_code == 0, init.output
        synth = runner.invoke(app, ["synth", "--out", str(tmp_path / "synth"), "--months", "1"])
        assert synth.exit_code == 0, synth.output
        ingest = runner.invoke(
            app,
            [
                "ingest",
                str(tmp_path / "synth" / "exports" / "chase_checking.csv"),
                "--config-dir",
                "config/examples",
            ],
        )
        assert ingest.exit_code == 0, ingest.output
        result = runner.invoke(app, ["transform"])
        assert result.exit_code == 0, result.output
        assert "dbt build succeeded" in result.output
        with duckdb.connect(str(get_settings().data.warehouse_path)) as conn:
            (paths,) = conn.execute("select count(*) from main_gold.gold_category_paths").fetchone()
            (txns,) = conn.execute(
                "select count(*) from main_silver.silver_transactions"
            ).fetchone()
        assert paths > 0
        assert txns > 0


class TestDeposit:
    def test_places_file_into_folder(self, tmp_path):
        src = tmp_path / "download.csv"
        src.write_text("a,b\n1,2\n", encoding="utf-8")
        inbox = tmp_path / "inbox"
        result = runner.invoke(app, ["deposit", str(src), str(inbox)])
        assert result.exit_code == 0, result.output
        assert (inbox / "download.csv").is_file()
        assert "Deposited" in result.output

    def test_missing_source_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app, ["deposit", str(tmp_path / "nope.csv"), str(tmp_path / "inbox")]
        )
        assert result.exit_code == 1
        assert "File not found" in result.output


def test_enrich_stub_exits_with_pointer_to_plan():
    result = runner.invoke(app, ["enrich"])
    assert result.exit_code == 2
    assert "not implemented" in result.output


class TestWatch:
    def test_not_a_directory_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app,
            ["watch", str(tmp_path / "nope"), "--config-dir", "config/examples"],
        )
        assert result.exit_code == 1
        assert "Not a directory" in result.output

    def test_unknown_source_exits_nonzero(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        result = runner.invoke(
            app,
            ["watch", str(inbox), "--source", "nope", "--config-dir", "config/examples"],
        )
        assert result.exit_code == 1
        assert "Unknown source" in result.output

    def test_starts_watching_then_stops(self, monkeypatch, tmp_path):
        """Patch the blocking loop so the command returns after starting the
        observer, exercising the happy path without hanging."""
        import personal_finance.cli as cli_module

        def stop_immediately(observer):
            observer.stop()
            observer.join()

        monkeypatch.setattr(cli_module, "_block_until_interrupt", stop_immediately)
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        result = runner.invoke(
            app,
            [
                "watch",
                str(inbox),
                "--config-dir",
                "config/examples",
                "--bronze",
                str(tmp_path / "bronze"),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "Watching" in result.output


class TestIngest:
    @pytest.fixture
    def exports(self, tmp_path):
        """Synth export files written once for the ingest tests."""
        from personal_finance.synth import generate_scenario, write_scenario

        out = tmp_path / "exports"
        write_scenario(generate_scenario(seed=42, months=2), out)
        return out

    def _bronze_count(self, bronze, table):
        from personal_finance.ingest import bronze_row_count

        return bronze_row_count(bronze, table)

    def test_ingests_csv_with_explicit_source(self, exports, tmp_path):
        bronze = tmp_path / "bronze"
        result = runner.invoke(
            app,
            [
                "ingest",
                str(exports / "chase_checking.csv"),
                "--source",
                "chase_checking",
                "--config-dir",
                "config/examples",
                "--bronze",
                str(bronze),
            ],
        )
        assert result.exit_code == 0, result.output
        assert "new row(s)" in result.output
        assert self._bronze_count(bronze, "chase_checking") > 0

    def test_infers_source_from_filename(self, exports, tmp_path):
        bronze = tmp_path / "bronze"
        result = runner.invoke(
            app,
            [
                "ingest",
                str(exports / "chase_checking.csv"),
                "--config-dir",
                "config/examples",
                "--bronze",
                str(bronze),
            ],
        )
        assert result.exit_code == 0, result.output
        assert self._bronze_count(bronze, "chase_checking") > 0

    def test_ingests_ofx_with_explicit_source(self, exports, tmp_path):
        bronze = tmp_path / "bronze"
        result = runner.invoke(
            app,
            [
                "ingest",
                str(exports / "ofx.ofx"),
                "--source",
                "chase_sapphire",
                "--config-dir",
                "config/examples",
                "--bronze",
                str(bronze),
            ],
        )
        assert result.exit_code == 0, result.output
        assert self._bronze_count(bronze, "chase_sapphire") > 0

    def test_reingest_is_idempotent(self, exports, tmp_path):
        bronze = tmp_path / "bronze"
        args = [
            "ingest",
            str(exports / "chase_checking.csv"),
            "--config-dir",
            "config/examples",
            "--bronze",
            str(bronze),
        ]
        first = runner.invoke(app, args)
        assert first.exit_code == 0, first.output
        count_after_first = self._bronze_count(bronze, "chase_checking")
        second = runner.invoke(app, args)
        assert second.exit_code == 0, second.output
        assert "0 new row(s)" in second.output
        assert self._bronze_count(bronze, "chase_checking") == count_after_first

    def test_unknown_source_exits_nonzero(self, exports, tmp_path):
        result = runner.invoke(
            app,
            [
                "ingest",
                str(exports / "chase_checking.csv"),
                "--source",
                "nope",
                "--config-dir",
                "config/examples",
                "--bronze",
                str(tmp_path / "bronze"),
            ],
        )
        assert result.exit_code == 1
        assert "Unknown source" in result.output

    def test_uninferable_filename_exits_nonzero(self, exports, tmp_path):
        # ofx.ofx has no source named "ofx"; without --source it can't be matched.
        result = runner.invoke(
            app,
            [
                "ingest",
                str(exports / "ofx.ofx"),
                "--config-dir",
                "config/examples",
                "--bronze",
                str(tmp_path / "bronze"),
            ],
        )
        assert result.exit_code == 1
        assert "No source config matches" in result.output

    def test_missing_file_exits_nonzero(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "ingest",
                str(tmp_path / "does_not_exist.csv"),
                "--source",
                "chase_checking",
                "--config-dir",
                "config/examples",
                "--bronze",
                str(tmp_path / "bronze"),
            ],
        )
        assert result.exit_code == 1
        assert "File not found" in result.output

    def test_unparseable_file_exits_nonzero(self, tmp_path):
        bad = tmp_path / "chase_checking.csv"
        bad.write_text("not,a,valid,export\nfoo,bar,baz,qux\n", encoding="utf-8")
        result = runner.invoke(
            app,
            [
                "ingest",
                str(bad),
                "--source",
                "chase_checking",
                "--config-dir",
                "config/examples",
                "--bronze",
                str(tmp_path / "bronze"),
            ],
        )
        assert result.exit_code == 1
        assert "Ingestion failed" in result.output
