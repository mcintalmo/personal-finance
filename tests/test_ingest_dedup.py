"""Tests for row-level idempotency of bronze ingestion.

Covers the ``dedup`` primitives directly and the end-to-end guarantee that
re-ingesting the same file (CSV or OFX) never duplicates rows in bronze.
"""

from datetime import date
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from personal_finance.ingest.dedup import compute_row_hash, existing_row_hashes
from personal_finance.ingest.pipeline import run_ingestion
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import SourceConfig, load_user_config

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def source_by_name(name: str) -> SourceConfig:
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    return next(s for s in config.sources if s.name == name)


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=2)


@pytest.fixture(scope="module")
def exports(scenario, tmp_path_factory):
    out = tmp_path_factory.mktemp("exports")
    write_scenario(scenario, out)
    return out


def bronze_row_count(bronze_dir: Path, table_name: str) -> int:
    with duckdb.connect() as conn:
        return conn.execute(
            f"select count(*) from read_parquet('{bronze_dir}/bronze/{table_name}/*.parquet')"
        ).fetchone()[0]


class TestComputeRowHash:
    def test_external_id_key_is_stable(self):
        a = compute_row_hash("chase", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", "FIT1")
        b = compute_row_hash("chase", date(2026, 1, 2), Decimal("-9.99"), "OTHER", "FIT1")
        # Same source + external_id ⇒ same hash regardless of other fields.
        assert a == b

    def test_content_key_used_when_no_external_id(self):
        a = compute_row_hash("chase", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", None)
        b = compute_row_hash("chase", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", None)
        c = compute_row_hash("chase", date(2026, 1, 1), Decimal("-1.01"), "COFFEE", None)
        assert a == b
        assert a != c

    def test_source_name_scopes_the_hash(self):
        a = compute_row_hash("chase", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", "FIT1")
        b = compute_row_hash("amex", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", "FIT1")
        assert a != b

    def test_account_id_scopes_the_hash(self):
        """A FITID reused across two accounts in one file must not collide."""
        checking = compute_row_hash(
            "bank", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", "1", account_id="CHK"
        )
        savings = compute_row_hash(
            "bank", date(2026, 1, 1), Decimal("-1.00"), "COFFEE", "1", account_id="SAV"
        )
        assert checking != savings

    def test_account_id_default_preserves_unscoped_key(self):
        """Omitting account_id (the CSV path) keeps the original key, so
        existing CSV hashes are unchanged."""
        with_default = compute_row_hash("s", date(2026, 1, 1), Decimal("-1.00"), "X", "FIT1")
        explicit_none = compute_row_hash(
            "s", date(2026, 1, 1), Decimal("-1.00"), "X", "FIT1", account_id=None
        )
        assert with_default == explicit_none


class TestExistingRowHashes:
    def test_empty_before_any_ingest(self, tmp_path):
        assert existing_row_hashes(tmp_path / "bronze", "chase_checking") == set()

    def test_returns_landed_hashes(self, exports, tmp_path):
        source = source_by_name("chase_checking")
        run_ingestion(source, exports / "chase_checking.csv", tmp_path / "bronze")
        hashes = existing_row_hashes(tmp_path / "bronze", "chase_checking")
        assert hashes
        assert all(isinstance(h, str) for h in hashes)


class TestReingestionIsIdempotent:
    def test_csv_same_file_twice_no_duplicates(self, scenario, exports, tmp_path):
        source = source_by_name("chase_checking")
        bronze = tmp_path / "bronze"
        run_ingestion(source, exports / "chase_checking.csv", bronze)
        first = bronze_row_count(bronze, "chase_checking")
        run_ingestion(source, exports / "chase_checking.csv", bronze)
        second = bronze_row_count(bronze, "chase_checking")
        assert first == len(scenario.checking.transactions)
        assert second == first  # re-ingest added nothing

    def test_ofx_same_file_twice_no_duplicates(self, scenario, exports, tmp_path):
        source = source_by_name("chase_sapphire")
        bronze = tmp_path / "bronze"
        ofx_file = exports / "ofx.ofx"
        run_ingestion(source, ofx_file, bronze)
        first = bronze_row_count(bronze, source.name)
        run_ingestion(source, ofx_file, bronze)
        second = bronze_row_count(bronze, source.name)
        assert first == len(scenario.checking.transactions)
        assert second == first

    def test_new_rows_still_land_after_reingest(self, scenario, exports, tmp_path):
        """A second, disjoint file for the same source still appends its rows —
        dedup only suppresses rows already present, never blocks new ones."""
        source = source_by_name("chase_checking")
        bronze = tmp_path / "bronze"
        run_ingestion(source, exports / "chase_checking.csv", bronze)
        before = bronze_row_count(bronze, "chase_checking")

        # Build a disjoint one-row CSV in the same layout with a unique amount.
        header = "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #\n"
        novel = tmp_path / "extra.csv"
        novel.write_text(
            header + "DEBIT,01/15/2026,NOVEL UNIQUE CHARGE,-123456.78,DEBIT,0.00,\n",
            encoding="utf-8",
        )
        run_ingestion(source, novel, bronze)
        after = bronze_row_count(bronze, "chase_checking")
        assert after == before + 1


class TestConcurrentIngestion:
    def test_same_source_concurrently_no_crash_no_duplicates(self, scenario, exports, tmp_path):
        """Ingestion is serialized, so overlapping runs for one source (as the
        folder watcher's sweep and observer thread can produce) neither collide
        on dlt's pipeline dir nor read stale dedup hashes and duplicate rows."""
        import threading

        source = source_by_name("chase_checking")
        bronze = tmp_path / "bronze"
        file_path = exports / "chase_checking.csv"
        errors: list[BaseException] = []

        def ingest() -> None:
            try:
                run_ingestion(source, file_path, bronze)
            except BaseException as exc:  # surface any thread error to the test
                errors.append(exc)

        threads = [threading.Thread(target=ingest) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors, f"concurrent ingestion raised: {errors}"
        # Idempotent under concurrency: exactly one file's worth of rows.
        assert bronze_row_count(bronze, "chase_checking") == len(scenario.checking.transactions)
