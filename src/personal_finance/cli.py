"""``pf`` — the personal-finance command-line entrypoint.

The CLI is the boundary layer: it catches domain exceptions and turns them
into exit codes + messages. Business logic lives in the library modules.

Commands mirror the pipeline stages (docs/ARCHITECTURE.md):

    pf synth       generate dummy export + receipt fixtures
    pf init-db     create the warehouse schema and seed the taxonomy
    pf transform   run the dbt medallion build (silver/gold + data tests)
    pf ingest      load source export files into the bronze layer
    pf watch       watch a folder and ingest exports as they are dropped in
    pf deposit     atomically place a completed file into a watched folder
    pf enrich      (Phase 4 stub)
"""

import os
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import typer

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.exceptions import ConfigurationError
from personal_finance.ingest import (
    IngestOutcome,
    IngestStatus,
    deposit_file,
    ingest_file,
    watch_folder,
)
from personal_finance.seed import seed_categories

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver
from personal_finance.synth import (
    generate_receipts,
    generate_scenario,
    write_receipts,
    write_scenario,
)
from personal_finance.user_config import load_user_config

app = typer.Typer(
    name="pf",
    help="Local-first personal finance pipeline.",
    no_args_is_help=True,
    add_completion=False,
)


@app.command()
def synth(
    out: Path = typer.Option(Path("data/synth"), help="Output directory for fixtures."),
    seed: int = typer.Option(42, help="RNG seed; same seed -> identical fixtures."),
    months: int = typer.Option(6, min=1, help="Months of activity to generate."),
) -> None:
    """Generate dummy bank/card export files and receipt fixtures."""
    scenario = generate_scenario(seed=seed, months=months)
    export_files = write_scenario(scenario, out / "exports")
    receipts = generate_receipts(scenario, seed=seed)
    receipt_files = write_receipts(receipts, out / "receipts")
    typer.echo(
        f"Wrote {len(export_files)} export files and {len(receipt_files)} receipt files "
        f"({len(receipts)} receipts) to {out}"
    )


@app.command("init-db")
def init_db(
    config_dir: Path | None = typer.Option(
        None, help="User config directory (default: Settings.config_dir)."
    ),
) -> None:
    """Create the warehouse schema and seed the category taxonomy."""
    warehouse = get_settings().data.warehouse_path
    try:
        config = load_user_config(config_dir)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        categories = seed_categories(conn, config.taxonomy)
    typer.echo(f"Initialized {warehouse}: {len(categories)} categories seeded")


@app.command()
def transform(
    project_dir: Path = typer.Option(
        Path("transform"), help="dbt project directory (run from the repo root)."
    ),
) -> None:
    """Run the dbt medallion build: silver/gold models plus data tests."""
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)
    bronze = settings.data.bronze_path
    if not any((bronze / "bronze").glob("*/*.parquet")):
        typer.echo(
            f"No ingested data under {bronze} — run `pf ingest` (or `pf watch`) first.", err=True
        )
        raise typer.Exit(code=1)
    os.environ.setdefault("DATA_WAREHOUSE_PATH", str(warehouse))
    os.environ.setdefault("DATA_BRONZE_PATH", str(bronze))

    from dbt.cli.main import dbtRunner  # slow import; deferred to this command

    result = dbtRunner().invoke(
        ["build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    )
    if not result.success:
        typer.echo("dbt build failed", err=True)
        raise typer.Exit(code=1)
    typer.echo("dbt build succeeded")


@app.command()
def ingest(
    files: list[Path] = typer.Argument(..., help="Export file(s) to ingest into bronze."),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Source config name for every file. If omitted, each file's source "
        "is inferred from its filename stem (e.g. chase_checking.csv -> chase_checking).",
    ),
    config_dir: Path | None = typer.Option(
        None, help="User config directory (default: Settings.config_dir)."
    ),
    bronze_dir: Path | None = typer.Option(
        None, "--bronze", help="Bronze output directory (default: Settings.data.bronze_path)."
    ),
) -> None:
    """Ingest source export files into the append-only bronze layer.

    Re-ingesting a file (or an overlapping export) is idempotent — rows already
    landed are skipped, so only genuinely-new rows are reported.
    """
    try:
        config = load_user_config(config_dir)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sources = {s.name: s for s in config.sources}
    if source is not None and source not in sources:
        typer.echo(f"Unknown source {source!r}. Configured sources: {sorted(sources)}", err=True)
        raise typer.Exit(code=1)

    bronze = bronze_dir or get_settings().data.bronze_path

    total_new = 0
    for file_path in files:
        if not file_path.is_file():
            typer.echo(f"File not found: {file_path}", err=True)
            raise typer.Exit(code=1)
        outcome = ingest_file(file_path, sources, bronze, source_name=source)
        if outcome.status is IngestStatus.UNMATCHED:
            typer.echo(
                f"No source config matches {file_path} (looked for {outcome.source!r}); "
                f"pass --source. Configured sources: {sorted(sources)}",
                err=True,
            )
            raise typer.Exit(code=1)
        if outcome.status is IngestStatus.FAILED:
            typer.echo(f"Ingestion failed for {file_path}: {outcome.detail}", err=True)
            raise typer.Exit(code=1)
        total_new += outcome.new_rows
        typer.echo(
            f"{file_path} -> {outcome.source}: {outcome.new_rows} new row(s) "
            f"({outcome.total_rows} total)"
        )

    typer.echo(f"Ingested {len(files)} file(s), {total_new} new row(s) into {bronze}")


def _report_outcome(outcome: IngestOutcome) -> None:
    """Print a one-line summary of a watched file's ingestion."""
    if outcome.status is IngestStatus.INGESTED:
        typer.echo(
            f"{outcome.file} -> {outcome.source}: {outcome.new_rows} new row(s) "
            f"({outcome.total_rows} total)"
        )
    elif outcome.status is IngestStatus.UNMATCHED:
        typer.echo(f"{outcome.file}: skipped — {outcome.detail}", err=True)
    else:  # FAILED
        typer.echo(f"{outcome.file}: ingestion failed — {outcome.detail}", err=True)


def _block_until_interrupt(observer: BaseObserver) -> None:  # pragma: no cover - blocking loop
    """Block the main thread until Ctrl-C, then stop the observer cleanly."""
    try:
        while observer.is_alive():
            observer.join(timeout=1)
    except KeyboardInterrupt:
        typer.echo("Stopping…")
    finally:
        observer.stop()
        observer.join()


@app.command()
def watch(
    folder: Path = typer.Argument(..., help="Folder to watch for dropped export files."),
    source: str | None = typer.Option(
        None,
        "--source",
        "-s",
        help="Source config name for every file. If omitted, each file's source "
        "is inferred from its filename stem.",
    ),
    config_dir: Path | None = typer.Option(
        None, help="User config directory (default: Settings.config_dir)."
    ),
    bronze_dir: Path | None = typer.Option(
        None, "--bronze", help="Bronze output directory (default: Settings.data.bronze_path)."
    ),
) -> None:
    """Watch a folder and ingest export files as they are dropped in.

    Ingests any files already present, then blocks watching for new ones until
    interrupted (Ctrl-C). Re-drops are idempotent.
    """
    if not folder.is_dir():
        typer.echo(f"Not a directory: {folder}", err=True)
        raise typer.Exit(code=1)
    try:
        config = load_user_config(config_dir)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    sources = {s.name: s for s in config.sources}
    if source is not None and source not in sources:
        typer.echo(f"Unknown source {source!r}. Configured sources: {sorted(sources)}", err=True)
        raise typer.Exit(code=1)

    bronze = bronze_dir or get_settings().data.bronze_path
    observer = watch_folder(folder, sources, bronze, source_name=source, on_outcome=_report_outcome)
    typer.echo(f"Watching {folder}/ for exports — Ctrl-C to stop.")
    _block_until_interrupt(observer)


@app.command()
def deposit(
    src: Path = typer.Argument(..., help="Completed file to place into the watched folder."),
    folder: Path = typer.Argument(..., help="Watched folder to deposit into."),
    name: str | None = typer.Option(
        None, help="Rename the file on arrival (default: keep its current name)."
    ),
) -> None:
    """Atomically place a completed file into a watched folder.

    Use as the last step of a download pipeline so that `pf watch` only ever
    sees complete files: download into a staging area, then `pf deposit` the
    finished file into the watched folder (a `.part` staging file makes the
    final appearance atomic).
    """
    if not src.is_file():
        typer.echo(f"File not found: {src}", err=True)
        raise typer.Exit(code=1)
    dest = deposit_file(src, folder, name=name)
    typer.echo(f"Deposited {src} -> {dest}")


@app.command()
def enrich() -> None:
    """Run the categorization/enrichment cascade (Phase 4 — not implemented)."""
    typer.echo(
        "pf enrich is not implemented yet — planned for Phase 4 (see docs/PLAN.md).", err=True
    )
    raise typer.Exit(code=2)
