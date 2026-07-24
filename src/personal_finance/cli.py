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
    pf enrich      embed merchants for the embedding-similarity categorization stage
    pf classify    ask a local LLM to categorize merchants stages 1-2 missed
    pf review      list the categorization cascade's ambiguous tail and record corrections
"""

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import typer

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.embed import EmbeddingClient, compute_missing_embeddings
from personal_finance.exceptions import ConfigurationError, ExternalServiceError, NotFoundError
from personal_finance.ingest import (
    IngestOutcome,
    IngestStatus,
    deposit_file,
    ingest_file,
    watch_folder,
)
from personal_finance.llm_categorize import (
    LlmCategorizeClient,
    compute_missing_llm_categories,
    fetch_category_paths,
)
from personal_finance.review import fetch_review_queue, record_label
from personal_finance.seed import seed_categories, seed_merchant_aliases, seed_rules

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver
from personal_finance.synth import (
    generate_receipts,
    generate_scenario,
    write_receipts,
    write_scenario,
)
from personal_finance.user_config import UserConfig, load_user_config


def _load_config_or_exit(config_dir: Path | None) -> UserConfig:
    """Load user config, exiting with a clean message on ConfigurationError."""
    try:
        return load_user_config(config_dir)
    except ConfigurationError as exc:
        typer.echo(f"Configuration error: {exc}", err=True)
        raise typer.Exit(code=1) from exc


app = typer.Typer(
    name="pf",
    help="Local-first personal finance pipeline.",
    no_args_is_help=True,
    add_completion=False,
)

review_app = typer.Typer(
    name="review",
    help="List the categorization cascade's ambiguous tail and record human corrections.",
    no_args_is_help=True,
    add_completion=False,
)
app.add_typer(review_app)


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
    """Create the warehouse schema and seed the category taxonomy, rules, and merchant aliases."""
    warehouse = get_settings().data.warehouse_path
    config = _load_config_or_exit(config_dir)
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        categories = seed_categories(conn, config.taxonomy)
        rules = seed_rules(conn, config.rules)
        aliases = seed_merchant_aliases(conn, config.merchant_aliases)
    typer.echo(
        f"Initialized {warehouse}: {len(categories)} categories, {len(rules)} rules, "
        f"{len(aliases)} merchant aliases seeded"
    )


@app.command()
def transform(
    project_dir: Path = typer.Option(
        Path("transform"), help="dbt project directory (run from the repo root)."
    ),
    config_dir: Path | None = typer.Option(
        None, help="User config directory (default: Settings.config_dir)."
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
    config = _load_config_or_exit(config_dir)
    os.environ.setdefault("DATA_WAREHOUSE_PATH", str(warehouse))
    os.environ.setdefault("DATA_BRONZE_PATH", str(bronze))

    from dbt.cli.main import dbtRunner  # slow import; deferred to this command

    result = dbtRunner().invoke(
        [
            "build",
            "--project-dir",
            str(project_dir),
            "--profiles-dir",
            str(project_dir),
            "--vars",
            json.dumps({"known_cities": config.known_cities}),
        ]
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
    config = _load_config_or_exit(config_dir)

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
    config = _load_config_or_exit(config_dir)

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
def enrich(
    base_url: str | None = typer.Option(
        None, help="Ollama server URL (default: Settings.ollama.base_url)."
    ),
    model: str | None = typer.Option(
        None, help="Embedding model (default: Settings.ollama.embedding_model)."
    ),
) -> None:
    """Embed every distinct merchant not yet cached, for the embedding-similarity
    categorization stage.

    Requires `pf transform` to have run at least once (reads
    silver_transactions.merchant_name) and a local Ollama server with the
    embedding model pulled. Re-run `pf transform` afterward to build
    silver_transaction_categories_embedding against the newly cached vectors.
    """
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        result = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main_silver' AND table_name = 'silver_transactions'"
        ).fetchone()
        if not result or not result[0]:
            typer.echo(
                "silver_transactions has not been built yet — run `pf transform` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        with EmbeddingClient(
            base_url or settings.ollama.base_url, model or settings.ollama.embedding_model
        ) as client:
            try:
                count = compute_missing_embeddings(
                    conn, client, model or settings.ollama.embedding_model
                )
            except ExternalServiceError as exc:
                typer.echo(f"Embedding failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    typer.echo(f"Embedded {count} new merchant(s). Run `pf transform` to apply them.")


@app.command()
def classify(
    base_url: str | None = typer.Option(
        None, help="Ollama server URL (default: Settings.ollama.base_url)."
    ),
    model: str | None = typer.Option(
        None, help="Chat model (default: Settings.ollama.chat_model)."
    ),
) -> None:
    """Ask a local LLM to categorize merchants stages 1-2 (rules, embedding
    similarity) missed — stage 3 of the categorization cascade.

    Requires `pf transform` to have run at least once (reads
    silver_transaction_categories/_embedding to see what's still
    uncategorized) and a local Ollama server with the chat model pulled.
    Re-run `pf transform` afterward to build silver_transaction_categories_llm
    against the newly cached classifications.
    """
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        result = conn.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_schema = 'main_silver' AND table_name = 'silver_transactions'"
        ).fetchone()
        if not result or not result[0]:
            typer.echo(
                "silver_transactions has not been built yet — run `pf transform` first.",
                err=True,
            )
            raise typer.Exit(code=1)

        with LlmCategorizeClient(
            base_url or settings.ollama.base_url, model or settings.ollama.chat_model
        ) as client:
            try:
                count = compute_missing_llm_categories(
                    conn, client, model or settings.ollama.chat_model
                )
            except ExternalServiceError as exc:
                typer.echo(f"Classification failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    typer.echo(f"Classified {count} new merchant(s). Run `pf transform` to apply them.")


def _require_transform_built(conn: duckdb.DuckDBPyConnection) -> None:
    result = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main_silver' AND table_name = 'silver_transaction_categories_all'"
    ).fetchone()
    if not result or not result[0]:
        typer.echo(
            "silver_transaction_categories_all has not been built yet — run `pf transform` first.",
            err=True,
        )
        raise typer.Exit(code=1)


@review_app.command("list")
def review_list(
    limit: int = typer.Option(20, help="Max transactions to show."),
) -> None:
    """List transactions no cascade stage could confidently categorize.

    Requires `pf transform` to have run at least once.
    """
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_transform_built(conn)
        items = fetch_review_queue(conn, limit=limit)

    if not items:
        typer.echo("Nothing to review — every transaction is categorized.")
        return
    for item in items:
        label = item.merchant_name or item.description_raw
        typer.echo(
            f"{item.transaction_id}  {item.posted_on}  {item.amount:>10}  {label} ({item.source})"
        )
    typer.echo(f"{len(items)} transaction(s) awaiting review.")


@review_app.command("label")
def review_label(
    transaction_id: str = typer.Argument(..., help="transaction_id from `pf review list`."),
    category_path: str = typer.Argument(
        ..., help="Slash-separated category path, e.g. essentials/groceries."
    ),
    note: str | None = typer.Option(None, help="Optional free-text context for this correction."),
) -> None:
    """Record a human category correction for one transaction.

    Stored as a label; the categorization outranks every automated stage once
    `pf transform` re-runs.
    """
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_transform_built(conn)
        category_paths = fetch_category_paths(conn)
        try:
            record_label(conn, transaction_id, category_path, category_paths, note=note)
        except NotFoundError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    typer.echo(f"Labeled {transaction_id} -> {category_path}. Run `pf transform` to apply it.")
