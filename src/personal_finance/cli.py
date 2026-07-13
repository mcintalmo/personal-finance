"""``pf`` — the personal-finance command-line entrypoint.

The CLI is the boundary layer: it catches domain exceptions and turns them
into exit codes + messages. Business logic lives in the library modules.

Commands mirror the pipeline stages (docs/ARCHITECTURE.md):

    pf synth       generate dummy export + receipt fixtures
    pf init-db     create the warehouse schema and seed the taxonomy
    pf transform   run the dbt medallion build (silver/gold + data tests)
    pf ingest      (Phase 2 stub)
    pf enrich      (Phase 4 stub)
"""

import os
from pathlib import Path

import duckdb
import typer

from personal_finance.config import get_settings
from personal_finance.ddl import create_schema
from personal_finance.exceptions import ConfigurationError
from personal_finance.seed import seed_categories
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
    warehouse = get_settings().data.warehouse_path
    if not warehouse.exists():
        typer.echo(f"Warehouse {warehouse} does not exist — run `pf init-db` first.", err=True)
        raise typer.Exit(code=1)
    os.environ.setdefault("DATA_WAREHOUSE_PATH", str(warehouse))

    from dbt.cli.main import dbtRunner  # slow import; deferred to this command

    result = dbtRunner().invoke(
        ["build", "--project-dir", str(project_dir), "--profiles-dir", str(project_dir)]
    )
    if not result.success:
        typer.echo("dbt build failed", err=True)
        raise typer.Exit(code=1)
    typer.echo("dbt build succeeded")


@app.command()
def ingest() -> None:
    """Ingest source exports into the bronze layer (Phase 2 — not implemented)."""
    typer.echo(
        "pf ingest is not implemented yet — planned for Phase 2 (see docs/PLAN.md).", err=True
    )
    raise typer.Exit(code=2)


@app.command()
def enrich() -> None:
    """Run the categorization/enrichment cascade (Phase 4 — not implemented)."""
    typer.echo(
        "pf enrich is not implemented yet — planned for Phase 4 (see docs/PLAN.md).", err=True
    )
    raise typer.Exit(code=2)
