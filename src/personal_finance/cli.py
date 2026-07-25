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
    pf enrich      embed merchants/split products for the embedding-similarity categorization stage
    pf classify    ask a local LLM to categorize merchants/split products stages 1-2 missed
    pf review      list the categorization cascade's ambiguous tail (--kind transaction|split)
                   and record corrections
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import duckdb
import typer

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.embed import (
    EmbeddingClient,
    compute_missing_embeddings,
    compute_missing_product_embeddings,
)
from personal_finance.exceptions import (
    ConfigurationError,
    ExternalServiceError,
    NotFoundError,
    ValidationError,
)
from personal_finance.forecast import (
    DEFAULT_HORIZON,
    DEFAULT_INTERVAL_LEVEL,
    MAX_HORIZON,
    MIN_HISTORY_MONTHS,
    compute_forecasts,
)
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
    compute_missing_product_llm_categories,
    fetch_category_paths,
)
from personal_finance.merchant_merge import (
    fetch_merge_candidates,
    fetch_similarity,
    record_merge_decision,
)
from personal_finance.models import EntityKind, MergeStatus
from personal_finance.review import fetch_review_queue, fetch_split_review_queue, record_label
from personal_finance.seed import (
    seed_budgets,
    seed_categories,
    seed_merchant_aliases,
    seed_rules,
)

if TYPE_CHECKING:
    from watchdog.observers.api import BaseObserver
from personal_finance.synth import (
    generate_amazon_orders,
    generate_receipts,
    generate_scenario,
    write_amazon_orders,
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
    help=(
        "List the categorization cascade's ambiguous tail and record human "
        "corrections; list/confirm candidate merchant-identity merges."
    ),
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
    """Generate dummy bank/card export files, receipt fixtures, and an Amazon
    order-history export."""
    scenario = generate_scenario(seed=seed, months=months)
    export_files = write_scenario(scenario, out / "exports")
    receipts = generate_receipts(scenario, seed=seed)
    receipt_files = write_receipts(receipts, out / "receipts")
    amazon_orders = generate_amazon_orders(scenario, seed=seed)
    amazon_files = write_amazon_orders(amazon_orders, out / "amazon")
    typer.echo(
        f"Wrote {len(export_files)} export files, {len(receipt_files)} receipt files "
        f"({len(receipts)} receipts), and {len(amazon_files)} Amazon order-history file(s) "
        f"({len(amazon_orders)} line items) to {out}"
    )


@app.command("init-db")
def init_db(
    config_dir: Path | None = typer.Option(
        None, help="User config directory (default: Settings.config_dir)."
    ),
) -> None:
    """Create the warehouse schema and seed the category taxonomy, rules, merchant aliases, and budgets."""
    warehouse = get_settings().data.warehouse_path
    config = _load_config_or_exit(config_dir)
    warehouse.parent.mkdir(parents=True, exist_ok=True)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        categories = seed_categories(conn, config.taxonomy)
        rules = seed_rules(conn, config.rules)
        aliases = seed_merchant_aliases(conn, config.merchant_aliases)
        budgets = seed_budgets(conn, config.budgets)
    typer.echo(
        f"Initialized {warehouse}: {len(categories)} categories, {len(rules)} rules, "
        f"{len(budgets)} budgets, "
        f"{len(aliases)} merchant aliases seeded"
    )


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address for the API server."),
    port: int = typer.Option(8000, help="Bind port for the API server."),
    reload: bool = typer.Option(False, help="Auto-reload on source changes (development only)."),
) -> None:
    """Run the FastAPI server (personal_finance.api) over the gold marts."""
    import uvicorn

    uvicorn.run("personal_finance.api:app", host=host, port=port, reload=reload)


@app.command()
def dashboard(
    api_url: str = typer.Option(
        None, help="Base URL of a running `pf serve` (default: Settings.serving.api_url)."
    ),
    port: int = typer.Option(8501, help="Bind port for the Streamlit app."),
) -> None:
    """Run the Streamlit dashboard (personal_finance.webapp) against a running `pf serve`."""
    webapp_main = Path(__file__).parent / "webapp" / "Overview.py"
    env = os.environ.copy()
    env["PF_API_URL"] = api_url or get_settings().serving.api_url
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(webapp_main),
            "--server.port",
            str(port),
        ],
        env=env,
        check=True,
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
    # bronze.transactions (sources.yml) reads bronze/*/*.parquet with a plain
    # read_parquet(), which throws on zero files — unlike Amazon's tolerant
    # read_parquet_or_empty — so at least one bank/card source must already be
    # ingested; Amazon-only data can't satisfy that on its own.
    if not any((bronze / "bronze").glob("*/*.parquet")):
        if any(bronze.glob("bronze_*/*/*.parquet")):
            typer.echo(
                f"Only enrichment data (e.g. Amazon order-history) found under {bronze} — "
                "`pf transform` also needs at least one ingested bank/card source. Run "
                "`pf ingest` (or `pf watch`) for one first.",
                err=True,
            )
        else:
            typer.echo(
                f"No ingested data under {bronze} — run `pf ingest` (or `pf watch`) first.",
                err=True,
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
    """Embed every distinct merchant and split product not yet cached, for the
    embedding-similarity categorization stage (transactions and line items).

    Requires `pf transform` to have run at least once (reads
    silver_transactions.merchant_name / silver_amazon_splits.product_name) and
    a local Ollama server with the embedding model pulled. Re-run
    `pf transform` afterward to build silver_transaction_categories_embedding /
    silver_split_categories_embedding against the newly cached vectors.
    """
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_silver_transactions_built(conn)

        with EmbeddingClient(
            base_url or settings.ollama.base_url, model or settings.ollama.embedding_model
        ) as client:
            try:
                merchant_count = compute_missing_embeddings(
                    conn, client, model or settings.ollama.embedding_model
                )
                product_count = compute_missing_product_embeddings(
                    conn, client, model or settings.ollama.embedding_model
                )
            except ExternalServiceError as exc:
                typer.echo(f"Embedding failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    typer.echo(
        f"Embedded {merchant_count} new merchant(s), {product_count} new product(s). "
        "Run `pf transform` to apply them."
    )


@app.command()
def forecast(
    horizon: int = typer.Option(
        DEFAULT_HORIZON, min=1, max=MAX_HORIZON, help="Months ahead to forecast."
    ),
    interval: int = typer.Option(
        DEFAULT_INTERVAL_LEVEL, min=1, max=99, help="Prediction-interval coverage, in percent."
    ),
) -> None:
    """Forecast spend and income for the next few months.

    Forecasts total income, total spend, and each configured budget's category
    subtree. Each month is split into its committed part (recurring charges
    projected forward from `gold_recurring_expenses`) and its variable part
    (statistically modelled) — only the variable part carries uncertainty.

    Requires `pf transform` to have run at least once. Series with fewer than
    six complete months of history are skipped rather than guessed at. Re-run
    `pf transform` afterward to publish the results as `gold_forecasts`.
    """
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_transform_built(conn)
        written = compute_forecasts(conn, horizon=horizon, interval_level=interval)

    if not written:
        typer.echo(
            f"No forecasts written — every series has fewer than {MIN_HISTORY_MONTHS} "
            "complete months of history. Ingest more data and re-run."
        )
        return
    typer.echo(
        f"Wrote {written} forecast row(s) at {interval}% interval. "
        "Run `pf transform` to publish gold_forecasts."
    )


@app.command()
def classify(
    base_url: str | None = typer.Option(
        None, help="Ollama server URL (default: Settings.ollama.base_url)."
    ),
    model: str | None = typer.Option(
        None, help="Chat model (default: Settings.ollama.chat_model)."
    ),
) -> None:
    """Ask a local LLM to categorize merchants and split products stages 1-2
    (rules, embedding similarity) missed — stage 3 of the categorization
    cascade, for both transactions and line items.

    Requires `pf transform` to have run at least once (reads
    silver_transaction_categories/_embedding and
    silver_split_categories/_embedding to see what's still uncategorized) and
    a local Ollama server with the chat model pulled. Re-run `pf transform`
    afterward to build silver_transaction_categories_llm /
    silver_split_categories_llm against the newly cached classifications.
    """
    settings = get_settings()
    warehouse = settings.data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_silver_transactions_built(conn)

        with LlmCategorizeClient(
            base_url or settings.ollama.base_url, model or settings.ollama.chat_model
        ) as client:
            try:
                merchant_count = compute_missing_llm_categories(
                    conn, client, model or settings.ollama.chat_model
                )
                product_count = compute_missing_product_llm_categories(
                    conn, client, model or settings.ollama.chat_model
                )
            except ExternalServiceError as exc:
                typer.echo(f"Classification failed: {exc}", err=True)
                raise typer.Exit(code=1) from exc

    typer.echo(
        f"Classified {merchant_count} new merchant(s), {product_count} new product(s). "
        "Run `pf transform` to apply them."
    )


def _require_silver_transactions_built(conn: duckdb.DuckDBPyConnection) -> None:
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


_REVIEW_ALL_TABLES: dict[str, str] = {
    "transaction": "silver_transaction_categories_all",
    "split": "silver_split_categories_all",
}


def _require_transform_built(conn: duckdb.DuckDBPyConnection, kind: str = "transaction") -> None:
    table = _REVIEW_ALL_TABLES[kind]
    result = conn.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_schema = 'main_silver' AND table_name = $table",
        {"table": table},
    ).fetchone()
    if not result or not result[0]:
        typer.echo(f"{table} has not been built yet — run `pf transform` first.", err=True)
        raise typer.Exit(code=1)


def _validate_kind(kind: str) -> None:
    if kind not in _REVIEW_ALL_TABLES:
        typer.echo(f"--kind must be one of {sorted(_REVIEW_ALL_TABLES)}, not {kind!r}.", err=True)
        raise typer.Exit(code=1)


@review_app.command("list")
def review_list(
    limit: int = typer.Option(20, help="Max transactions/splits to show."),
    kind: str = typer.Option("transaction", help='"transaction" or "split".'),
) -> None:
    """List transactions (or splits) no cascade stage could confidently categorize.

    Requires `pf transform` to have run at least once.
    """
    _validate_kind(kind)
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_transform_built(conn, kind)
        if kind == "split":
            split_items = fetch_split_review_queue(conn, limit=limit)
            if not split_items:
                typer.echo("Nothing to review — every split is categorized.")
                return
            for split_item in split_items:
                typer.echo(
                    f"{split_item.split_id}  {split_item.amount:>10}  "
                    f"{split_item.product_name} (txn {split_item.transaction_id})"
                )
            typer.echo(f"{len(split_items)} split(s) awaiting review.")
            return
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
    subject_id: str = typer.Argument(..., help="transaction_id or split_id from `pf review list`."),
    category_path: str = typer.Argument(
        ..., help="Slash-separated category path, e.g. essentials/groceries."
    ),
    note: str | None = typer.Option(None, help="Optional free-text context for this correction."),
    kind: str = typer.Option("transaction", help='"transaction" or "split".'),
) -> None:
    """Record a human category correction for one transaction or split.

    Stored as a label; the categorization outranks every automated stage once
    `pf transform` re-runs.
    """
    _validate_kind(kind)
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_transform_built(conn, kind)
        category_paths = fetch_category_paths(conn)
        entity_kind = EntityKind.SPLIT if kind == "split" else EntityKind.TRANSACTION
        try:
            record_label(
                conn, subject_id, category_path, category_paths, subject_kind=entity_kind, note=note
            )
        except NotFoundError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    typer.echo(f"Labeled {subject_id} -> {category_path}. Run `pf transform` to apply it.")


@review_app.command("merge-candidates")
def review_merge_candidates(
    model: str | None = typer.Option(
        None, help="Embedding model (default: Settings.ollama.embedding_model)."
    ),
    threshold: float = typer.Option(
        0.90, min=0.0, max=1.0, help="Minimum cosine similarity to suggest a merge."
    ),
    limit: int = typer.Option(20, help="Max candidates to show."),
) -> None:
    """List candidate merchant-identity merges (embedding-similarity) — the
    descriptor tail merchants.yaml aliases weren't written for.

    Requires `pf transform` and `pf enrich` to have run at least once.
    """
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_silver_transactions_built(conn)
        candidates = fetch_merge_candidates(
            conn,
            model=model or get_settings().ollama.embedding_model,
            threshold=threshold,
            limit=limit,
        )

    if not candidates:
        typer.echo("No merge candidates — run `pf enrich` first if you haven't yet.")
        return
    for candidate in candidates:
        typer.echo(
            f"{candidate.merchant_name!r} -> {candidate.canonical_name!r} "
            f"(similarity {candidate.similarity:.3f})"
        )
    typer.echo(f"{len(candidates)} candidate(s). `pf review merge <name> <canonical>` to accept.")


_MERGE_MERCHANT_NAME_ARG = typer.Argument(
    ..., help="merchant_name from `pf review merge-candidates`."
)
_MERGE_MODEL_OPTION = typer.Option(
    None,
    help="Embedding model to look up the similarity score for (default: Settings.ollama.embedding_model).",
)
_MERGE_NOTE_OPTION = typer.Option(None, help="Optional free-text context for this decision.")


@review_app.command("merge")
def review_merge(
    merchant_name: str = _MERGE_MERCHANT_NAME_ARG,
    canonical_name: str = typer.Argument(..., help="The merchant_name to merge it into."),
    model: str | None = _MERGE_MODEL_OPTION,
    note: str | None = _MERGE_NOTE_OPTION,
) -> None:
    """Confirm a candidate merchant-identity merge.

    Applied after merchant_aliases; the merge outranks nothing but fills a
    gap regex aliases didn't cover — re-run `pf transform` to apply it.
    """
    _record_merge_command(merchant_name, canonical_name, MergeStatus.ACCEPTED, model, note)


@review_app.command("reject-merge")
def review_reject_merge(
    merchant_name: str = _MERGE_MERCHANT_NAME_ARG,
    canonical_name: str = typer.Argument(
        ..., help="The merchant_name it was suggested to merge into."
    ),
    model: str | None = _MERGE_MODEL_OPTION,
    note: str | None = _MERGE_NOTE_OPTION,
) -> None:
    """Reject a candidate merchant-identity merge so it stops resurfacing."""
    _record_merge_command(merchant_name, canonical_name, MergeStatus.REJECTED, model, note)


def _record_merge_command(
    merchant_name: str,
    canonical_name: str,
    status: MergeStatus,
    model: str | None,
    note: str | None,
) -> None:
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)

    with duckdb.connect(str(warehouse)) as conn:
        _require_silver_transactions_built(conn)
        similarity = fetch_similarity(
            conn,
            merchant_name,
            canonical_name,
            model=model or get_settings().ollama.embedding_model,
        )
        try:
            record_merge_decision(
                conn, merchant_name, canonical_name, status, similarity=similarity, note=note
            )
        except (NotFoundError, ValidationError) as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1) from exc

    if status is MergeStatus.ACCEPTED:
        typer.echo(
            f"Merged {merchant_name!r} -> {canonical_name!r}. Run `pf transform` to apply it."
        )
    else:
        typer.echo(f"Rejected {merchant_name!r} -> {canonical_name!r}.")
