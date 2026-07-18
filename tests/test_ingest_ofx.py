"""Tests for personal_finance.ingest.ofx_source and OFX pipeline dispatch."""

from pathlib import Path

import duckdb
import pytest

from personal_finance.exceptions import IngestionError
from personal_finance.ingest import (
    read_ofx_transactions,
    run_ingestion,
    run_ofx_ingestion,
)
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import SourceConfig, load_user_config

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def ofx_source() -> SourceConfig:
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    return next(s for s in config.sources if s.kind.value == "ofx")


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=2)


@pytest.fixture(scope="module")
def ofx_file(scenario, tmp_path_factory):
    """The synth OFX export (checking activity) generated once per module."""
    out = tmp_path_factory.mktemp("exports")
    write_scenario(scenario, out)
    return out / "ofx.ofx"


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


class TestReadOfxTransactions:
    def test_parses_all_transactions(self, scenario, ofx_file):
        rows = list(read_ofx_transactions(ofx_file))
        assert len(rows) == len(scenario.checking.transactions)

    def test_signs_match_ofx_convention(self, scenario, ofx_file):
        """OFX TRNAMT is already signed negative=outflow — must round-trip
        exactly to the scenario amounts, both inflow and outflow."""
        parsed = sorted((r["posted_on"], r["amount"]) for r in read_ofx_transactions(ofx_file))
        expected = sorted((t.posted_on, t.amount) for t in scenario.checking.transactions)
        assert parsed == expected
        amounts = [a for _, a in parsed]
        assert any(a > 0 for a in amounts) and any(a < 0 for a in amounts)

    def test_fitid_becomes_external_id(self, ofx_file):
        rows = list(read_ofx_transactions(ofx_file))
        assert all(r["external_id"] and r["external_id"].startswith("CHK") for r in rows)

    def test_malformed_file_raises_ingestion_error(self, tmp_path):
        bad = tmp_path / "bad.ofx"
        bad.write_text("this is not OFX at all", encoding="utf-8")
        with pytest.raises(IngestionError, match=r"bad\.ofx"):
            list(read_ofx_transactions(bad))

    def test_spec_incomplete_file_raises_ingestion_error(self, tmp_path):
        """A file missing a required aggregate (LEDGERBAL) is rejected by the
        strict parser — surfaced as our IngestionError, not ofxtools'."""
        incomplete = tmp_path / "no_ledgerbal.ofx"
        incomplete.write_text(
            "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
            "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
            "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<DTSERVER>20260711120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>"
            "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
            "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>1<ACCTID>1<ACCTTYPE>CHECKING"
            "</BANKACCTFROM><BANKTRANLIST><DTSTART>20260101<DTEND>20260102"
            "<STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20260101<TRNAMT>-1.00<FITID>X1<NAME>Y"
            "</STMTTRN></BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n",
            encoding="utf-8",
        )
        with pytest.raises(IngestionError, match="OFX"):
            list(read_ofx_transactions(incomplete))


class TestRunOfxIngestion:
    def test_lands_provenance_and_rows(self, scenario, ofx_file, tmp_path):
        source = ofx_source()
        info = run_ofx_ingestion(source, ofx_file, tmp_path / "bronze")
        assert not info.has_failed_jobs
        columns = bronze_columns(tmp_path / "bronze", source.name)
        for expected in ("source", "account_name", "account_type", "currency", "external_id"):
            assert expected in columns
        rows = bronze_rows(tmp_path / "bronze", source.name)
        assert len(rows) == len(scenario.checking.transactions)

    def test_account_labels_come_from_config_not_file(self, ofx_file, tmp_path):
        source = ofx_source()
        run_ofx_ingestion(source, ofx_file, tmp_path / "bronze")
        columns = bronze_columns(tmp_path / "bronze", source.name)
        rows = bronze_rows(tmp_path / "bronze", source.name)
        account_names = {row[columns.index("account_name")] for row in rows}
        assert account_names == {source.account_name}


class TestRunIngestionDispatch:
    def test_dispatches_ofx_by_kind(self, scenario, ofx_file, tmp_path):
        source = ofx_source()
        run_ingestion(source, ofx_file, tmp_path / "bronze")
        rows = bronze_rows(tmp_path / "bronze", source.name)
        assert len(rows) == len(scenario.checking.transactions)

    def test_dispatches_csv_by_kind(self, scenario, tmp_path):
        out = tmp_path / "exports"
        write_scenario(scenario, out)
        config = load_user_config(EXAMPLES_CONFIG_DIR)
        csv_source = next(s for s in config.sources if s.name == "chase_checking")
        run_ingestion(csv_source, out / "chase_checking.csv", tmp_path / "bronze")
        rows = bronze_rows(tmp_path / "bronze", "chase_checking")
        assert len(rows) == len(scenario.checking.transactions)
