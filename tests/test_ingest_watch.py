"""Tests for personal_finance.ingest.watch (folder sweep + watchdog watcher)."""

import shutil
import threading
from pathlib import Path

import duckdb
import pytest
from watchdog.events import (
    DirCreatedEvent,
    FileCreatedEvent,
    FileMovedEvent,
)

from personal_finance.ingest.watch import (
    IngestStatus,
    _ExportEventHandler,
    deposit_file,
    ingest_file,
    sweep_folder,
    watch_folder,
)
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import SourceConfig, load_user_config

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def sources_map() -> dict[str, SourceConfig]:
    return {s.name: s for s in load_user_config(EXAMPLES_CONFIG_DIR).sources}


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=2)


@pytest.fixture(scope="module")
def exports(scenario, tmp_path_factory):
    out = tmp_path_factory.mktemp("exports")
    write_scenario(scenario, out)
    return out


def bronze_count(bronze_dir: Path, table_name: str) -> int:
    with duckdb.connect() as conn:
        try:
            (count,) = conn.execute(
                f"select count(*) from read_parquet('{bronze_dir}/bronze/{table_name}/*.parquet')"
            ).fetchone()
        except duckdb.IOException:
            return 0
    return count


class TestIngestFile:
    def test_matched_file_ingests_and_counts(self, scenario, exports, tmp_path):
        bronze = tmp_path / "bronze"
        outcome = ingest_file(exports / "chase_checking.csv", sources_map(), bronze)
        assert outcome.status is IngestStatus.INGESTED
        assert outcome.source == "chase_checking"
        assert outcome.new_rows == len(scenario.checking.transactions)
        assert outcome.total_rows == outcome.new_rows

    def test_reingest_reports_zero_new(self, exports, tmp_path):
        bronze = tmp_path / "bronze"
        first = ingest_file(exports / "chase_checking.csv", sources_map(), bronze)
        second = ingest_file(exports / "chase_checking.csv", sources_map(), bronze)
        assert first.new_rows > 0
        assert second.new_rows == 0
        assert second.total_rows == first.total_rows

    def test_explicit_source_overrides_stem(self, exports, tmp_path):
        outcome = ingest_file(
            exports / "ofx.ofx", sources_map(), tmp_path / "bronze", source_name="chase_sapphire"
        )
        assert outcome.status is IngestStatus.INGESTED
        assert outcome.source == "chase_sapphire"

    def test_unmatched_stem_returns_unmatched(self, exports, tmp_path):
        # ofx.ofx has no source named "ofx".
        outcome = ingest_file(exports / "ofx.ofx", sources_map(), tmp_path / "bronze")
        assert outcome.status is IngestStatus.UNMATCHED
        assert outcome.source == "ofx"

    def test_unparseable_file_returns_failed(self, tmp_path):
        bad = tmp_path / "chase_checking.csv"
        bad.write_text("not,a,valid,export\nfoo,bar,baz,qux\n", encoding="utf-8")
        outcome = ingest_file(bad, sources_map(), tmp_path / "bronze")
        assert outcome.status is IngestStatus.FAILED
        assert outcome.detail


class TestSweepFolder:
    def test_sweeps_all_matching_files(self, tmp_path):
        # Drop two known CSVs (named for their sources) into a folder.
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        src_exports = tmp_path / "src"
        write_scenario(generate_scenario(seed=42, months=2), src_exports)
        for name in ("chase_checking.csv", "amex.csv"):
            shutil.copy(src_exports / name, inbox / name)

        outcomes = sweep_folder(inbox, sources_map(), tmp_path / "bronze")
        by_source = {o.source: o for o in outcomes}
        assert by_source["chase_checking"].status is IngestStatus.INGESTED
        assert by_source["amex"].status is IngestStatus.INGESTED
        assert all(o.new_rows > 0 for o in outcomes)

    def test_unmatched_files_reported_not_ingested(self, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        (inbox / "mystery.csv").write_text("a,b\n1,2\n", encoding="utf-8")
        outcomes = sweep_folder(inbox, sources_map(), tmp_path / "bronze")
        assert len(outcomes) == 1
        assert outcomes[0].status is IngestStatus.UNMATCHED


class TestDepositFile:
    def test_places_complete_file_and_cleans_staging(self, exports, tmp_path):
        inbox = tmp_path / "inbox"
        dest = deposit_file(exports / "chase_checking.csv", inbox)
        assert dest == inbox / "chase_checking.csv"
        assert dest.read_text() == (exports / "chase_checking.csv").read_text()
        # No .part staging file is left behind.
        assert list(inbox.glob("*.part")) == []

    def test_creates_folder_and_honours_name_override(self, exports, tmp_path):
        inbox = tmp_path / "nested" / "inbox"
        dest = deposit_file(exports / "chase_checking.csv", inbox, name="renamed.csv")
        assert dest == inbox / "renamed.csv"
        assert dest.is_file()

    def test_staging_name_is_ignored_by_export_patterns(self):
        # The .part staging file must not match the watcher's patterns, so the
        # watcher ignores it until the atomic rename completes.
        from personal_finance.ingest.watch import DEFAULT_PATTERNS, _matches

        assert _matches("chase_checking.csv.part", DEFAULT_PATTERNS) is False
        assert _matches("chase_checking.csv", DEFAULT_PATTERNS) is True


class TestExportEventHandler:
    def test_on_created_matching_file_invokes_callback(self, tmp_path):
        seen: list[Path] = []
        handler = _ExportEventHandler(("*.csv",), seen.append)
        handler.on_created(FileCreatedEvent(str(tmp_path / "chase_checking.csv")))
        assert seen == [tmp_path / "chase_checking.csv"]

    def test_on_moved_uses_destination_path(self, tmp_path):
        seen: list[Path] = []
        handler = _ExportEventHandler(("*.csv",), seen.append)
        handler.on_moved(
            FileMovedEvent(str(tmp_path / "download.part"), str(tmp_path / "amex.csv"))
        )
        assert seen == [tmp_path / "amex.csv"]

    def test_non_matching_extension_ignored(self, tmp_path):
        seen: list[Path] = []
        handler = _ExportEventHandler(("*.csv",), seen.append)
        handler.on_created(FileCreatedEvent(str(tmp_path / "notes.txt")))
        assert seen == []

    def test_directory_events_ignored(self, tmp_path):
        seen: list[Path] = []
        handler = _ExportEventHandler(("*.csv",), seen.append)
        handler.on_created(DirCreatedEvent(str(tmp_path / "subdir")))
        assert seen == []


class TestWatchFolder:
    def test_initial_sweep_ingests_existing_files(self, exports, tmp_path):
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        shutil.copy(exports / "chase_checking.csv", inbox / "chase_checking.csv")
        outcomes: list = []
        observer = watch_folder(
            inbox, sources_map(), tmp_path / "bronze", on_outcome=outcomes.append
        )
        try:
            assert any(
                o.source == "chase_checking" and o.status is IngestStatus.INGESTED for o in outcomes
            )
        finally:
            observer.stop()
            observer.join()

    def test_deposited_file_is_ingested(self, exports, tmp_path):
        """End-to-end: start the observer, deposit a file, and confirm it lands.

        Uses the supported delivery path (deposit_file → atomic rename) so the
        observer only ever sees a complete file.
        """
        inbox = tmp_path / "inbox"
        inbox.mkdir()
        bronze = tmp_path / "bronze"
        got = threading.Event()
        outcomes: list = []

        def record(outcome) -> None:
            outcomes.append(outcome)
            got.set()

        observer = watch_folder(
            inbox, sources_map(), bronze, on_outcome=record, sweep_existing=False
        )
        try:
            deposit_file(exports / "chase_checking.csv", inbox)

            assert got.wait(timeout=15), "watcher did not ingest the deposited file in time"
            assert any(
                o.source == "chase_checking" and o.status is IngestStatus.INGESTED for o in outcomes
            )
            assert bronze_count(bronze, "chase_checking") > 0
        finally:
            observer.stop()
            observer.join()
