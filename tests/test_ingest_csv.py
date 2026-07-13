"""Tests for personal_finance.ingest (CSV bronze ingestion)."""

from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.csv_source import (
    _parse_amount_signed,
    _parse_amount_unsigned,
    _parse_date,
    read_rows,
)
from personal_finance.ingest.pipeline import run_csv_ingestion
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import SignConvention, SourceConfig, load_user_config

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def source_by_name(name: str) -> SourceConfig:
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    return next(s for s in config.sources if s.name == name)


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=2)


@pytest.fixture(scope="module")
def exports(scenario, tmp_path_factory):
    """Real synth export files for every CSV source, generated once per module."""
    out = tmp_path_factory.mktemp("exports")
    write_scenario(scenario, out)
    return out


def bronze_rows(bronze_dir: Path, table_name: str) -> list[tuple]:
    with duckdb.connect() as conn:
        return conn.execute(
            f"select * from read_parquet('{bronze_dir}/bronze/{table_name}/*.parquet') "
            "order by posted_on, description_raw"
        ).fetchall()


def bronze_columns(bronze_dir: Path, table_name: str) -> list[str]:
    with duckdb.connect() as conn:
        return [
            row[0]
            for row in conn.execute(
                f"describe select * from read_parquet('{bronze_dir}/bronze/{table_name}/*.parquet')"
            ).fetchall()
        ]


class TestAmountParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("-42.50", Decimal("-42.50")),
            ("2500.00", Decimal("2500.00")),
            ("+ $32.00", Decimal("32.00")),
            ("- $320.00", Decimal("-320.00")),
            ("-$42.50", Decimal("-42.50")),
            ("$2,500.00", Decimal("2500.00")),
        ],
    )
    def test_parse_amount_signed(self, raw, expected):
        assert _parse_amount_signed(raw) == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("42.50", Decimal("42.50")),
            ("", Decimal("0")),
            ("  ", Decimal("0")),
            ("$1,234.56", Decimal("1234.56")),
        ],
    )
    def test_parse_amount_unsigned(self, raw, expected):
        assert _parse_amount_unsigned(raw) == expected

    def test_parse_date_default_iso_format(self):
        assert _parse_date("2026-07-12", None) == date(2026, 7, 12)

    def test_parse_date_truncates_datetime(self):
        assert _parse_date("2026-01-07T03:33:33", "%Y-%m-%dT%H:%M:%S") == date(2026, 1, 7)


class TestReadRows:
    def test_headered_file(self, exports):
        rows = list(read_rows(source_by_name("chase_checking"), exports / "chase_checking.csv"))
        assert rows[0]["Description"] == "ACME CORP PAYROLL"

    def test_headerless_file_uses_configured_columns(self, exports):
        rows = list(read_rows(source_by_name("wells_fargo"), exports / "wells_fargo.csv"))
        assert set(rows[0]) == {"posted_on", "amount", "status", "check_number", "description_raw"}

    def test_skip_rows_lands_on_real_header(self, exports):
        rows = list(read_rows(source_by_name("bofa_checking"), exports / "bofa_checking.csv"))
        assert set(rows[0]) == {"Date", "Description", "Amount", "Running Bal."}


class TestRunCsvIngestion:
    def test_non_ingestion_failure_propagates_unwrapped(self, tmp_path):
        """A failure with no IngestionError in its cause chain (e.g. the source
        file doesn't exist) should surface as dlt's own exception, not ours —
        the unwrap in run_csv_ingestion only rewrites IngestionError chains."""
        from dlt.pipeline.exceptions import PipelineStepFailed

        source = source_by_name("chase_checking")
        with pytest.raises(PipelineStepFailed):
            run_csv_ingestion(source, tmp_path / "does-not-exist.csv", tmp_path / "bronze")

    @pytest.mark.parametrize(
        "name",
        ["chase_checking", "venmo", "wells_fargo", "bofa_checking", "capital_one", "citi", "amex"],
    )
    def test_ingests_every_example_source_without_error(self, exports, tmp_path, name):
        source = source_by_name(name)
        info = run_csv_ingestion(source, exports / f"{name}.csv", tmp_path / "bronze")
        assert not info.has_failed_jobs
        rows = bronze_rows(tmp_path / "bronze", name)
        assert len(rows) > 0

    def test_provenance_columns_present(self, exports, tmp_path):
        source = source_by_name("chase_checking")
        file_path = exports / "chase_checking.csv"
        run_csv_ingestion(source, file_path, tmp_path / "bronze")
        columns = bronze_columns(tmp_path / "bronze", "chase_checking")
        for expected in (
            "source",
            "account_name",
            "account_type",
            "currency",
            "source_file",
            "ingested_at",
        ):
            assert expected in columns

    def test_unconfigured_external_id_omitted_not_nulled(self, exports, tmp_path):
        """Sources without an external_id column shouldn't get an all-null one."""
        source = source_by_name("chase_checking")
        run_csv_ingestion(source, exports / "chase_checking.csv", tmp_path / "bronze")
        assert "external_id" not in bronze_columns(tmp_path / "bronze", "chase_checking")

    def test_external_id_captured_when_configured(self, exports, tmp_path):
        source = source_by_name("venmo")
        run_csv_ingestion(source, exports / "venmo.csv", tmp_path / "bronze")
        rows = bronze_rows(tmp_path / "bronze", "venmo")
        columns = bronze_columns(tmp_path / "bronze", "venmo")
        external_ids = [row[columns.index("external_id")] for row in rows]
        assert all(eid and eid.startswith("VEN") for eid in external_ids)

    @pytest.mark.parametrize("name", ["capital_one", "citi", "amex"])
    def test_recovers_exact_scenario_amounts(self, scenario, exports, tmp_path, name):
        """debit_credit (capital_one/citi) and inverted (amex) conventions must
        round-trip to the exact signed amounts the scenario generated —
        purchases negative AND card payments positive, same as the source
        (this exercises both sides of the sign convention, not just outflow)."""
        source = source_by_name(name)
        run_csv_ingestion(source, exports / f"{name}.csv", tmp_path / "bronze")
        columns = bronze_columns(tmp_path / "bronze", name)
        rows = bronze_rows(tmp_path / "bronze", name)
        ingested = sorted(
            (row[columns.index("posted_on")], row[columns.index("amount")]) for row in rows
        )
        expected = sorted((t.posted_on, t.amount) for t in scenario.credit.transactions)
        assert ingested == expected
        assert any(amount > 0 for _, amount in ingested)  # card payments included
        assert any(amount < 0 for _, amount in ingested)  # purchases included

    def test_headerless_and_skip_rows_produce_same_row_count_as_signed(self, exports, tmp_path):
        chase = source_by_name("chase_checking")
        wells_fargo = source_by_name("wells_fargo")
        bofa = source_by_name("bofa_checking")
        run_csv_ingestion(chase, exports / "chase_checking.csv", tmp_path / "bronze")
        run_csv_ingestion(wells_fargo, exports / "wells_fargo.csv", tmp_path / "bronze")
        run_csv_ingestion(bofa, exports / "bofa_checking.csv", tmp_path / "bronze")
        n_chase = len(bronze_rows(tmp_path / "bronze", "chase_checking"))
        n_wf = len(bronze_rows(tmp_path / "bronze", "wells_fargo"))
        n_bofa = len(bronze_rows(tmp_path / "bronze", "bofa_checking"))
        assert n_chase == n_wf == n_bofa > 0  # same underlying checking activity

    def test_malformed_row_raises_ingestion_error(self, tmp_path):
        source = source_by_name("chase_checking")
        bad_file = tmp_path / "bad.csv"
        bad_file.write_text(
            "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
            "DEBIT,not-a-date,BAD ROW,-1.00,DEBIT_CARD,0,\n",
            encoding="utf-8",
        )
        with pytest.raises(IngestionError, match=r"bad\.csv"):
            run_csv_ingestion(source, bad_file, tmp_path / "bronze")

    def test_reingesting_same_file_appends(self, exports, tmp_path):
        """Documents current append-only behaviour — dedup is a later task."""
        source = source_by_name("chase_checking")
        file_path = exports / "chase_checking.csv"
        run_csv_ingestion(source, file_path, tmp_path / "bronze")
        first_count = len(bronze_rows(tmp_path / "bronze", "chase_checking"))
        run_csv_ingestion(source, file_path, tmp_path / "bronze")
        second_count = len(bronze_rows(tmp_path / "bronze", "chase_checking"))
        assert second_count == first_count * 2


class TestSourceConfigCsvValidation:
    def test_missing_required_key_rejected(self):
        with pytest.raises(ValueError, match="missing required keys"):
            SourceConfig(
                name="s",
                kind="csv",
                account_name="A",
                account_type="checking",
                column_map={"posted_on": "Date"},
            )

    def test_debit_credit_requires_debit_and_credit_keys(self):
        with pytest.raises(ValueError, match="missing required keys"):
            SourceConfig(
                name="s",
                kind="csv",
                account_name="A",
                account_type="checking",
                sign_convention=SignConvention.DEBIT_CREDIT,
                column_map={"posted_on": "Date", "description_raw": "Desc", "amount": "Amount"},
            )

    def test_headerless_without_columns_rejected(self):
        with pytest.raises(ValueError, match="requires 'columns'"):
            SourceConfig(
                name="s",
                kind="csv",
                account_name="A",
                account_type="checking",
                has_header=False,
                column_map={"posted_on": "a", "description_raw": "b", "amount": "c"},
            )

    def test_column_map_referencing_unknown_positional_column_rejected(self):
        with pytest.raises(ValueError, match="not in 'columns'"):
            SourceConfig(
                name="s",
                kind="csv",
                account_name="A",
                account_type="checking",
                has_header=False,
                columns=["a", "b", "c"],
                column_map={"posted_on": "a", "description_raw": "b", "amount": "nope"},
            )

    def test_ofx_source_skips_csv_validation(self):
        source = SourceConfig(name="s", kind="ofx", account_name="A", account_type="credit_card")
        assert source.column_map == {}
