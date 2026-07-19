# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 3 — Core cleaning (silver)

> Phase 1 (Foundation) complete — demo verified 2026-07-12.
> Phase 2 (Ingestion) complete — demo verified 2026-07-18: `pf synth` → fixtures,
> `pf ingest`/`pf watch` → idempotent bronze Parquet (CSV + OFX), source inferred or `--source`.

- [ ] ⏳ IN PROGRESS — Merchant descriptor cleaning and normalization (raw string → merchant entity)
- [ ] Transfer detection: correlate paired movements across accounts (amount negation +
      date window + account pair) and exclude from spend
- [ ] dbt data tests on every silver model (silver_transactions covered; extend to future models)

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 4–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

## Done

- [x] Silver transactions model: `silver_transactions` unions every ingested source via a
      config-free `bronze/*/*.parquet` glob (dbt-duckdb external source, `union_by_name`), so a
      new bank appears automatically. Dedups on `row_hash` (the grain → `transaction_id`),
      normalizes types (amount→`decimal(18,2)`, description trimmed, currency upper-cased) and
      surfaces a derived `flow` (inflow/outflow); the signed convention is already uniform from
      ingest. dbt data tests: unique/not_null on the grain, accepted_values on account_type and
      flow. Also made bronze's `external_id` a stable (always-present, nullable) column via a dlt
      column hint so the single-source union never loses it. `pf transform` now wires
      `DATA_BRONZE_PATH` and guards on "no ingested data" — `transform/models/silver/`, `cli.py`
      (2026-07-19)

- [x] Watch-folder ingestion: `pf watch FOLDER [--source NAME]` ingests exports as they are
      dropped in, via watchdog's OS filesystem observer (created/moved events) — sweeps files
      already present first, then blocks until Ctrl-C. Shared `ingest_file` unifies `pf ingest`
      and the watcher; idempotency makes re-drops safe. `ingest/watch.py`, `pf watch` (2026-07-18).
      **Phase 2 complete.**
- [x] Wire `pf ingest` to the dlt pipelines: `pf ingest FILE... [--source NAME]` lands exports into bronze via `run_ingestion` (dispatches on source.kind). Source is explicit or inferred from the filename stem; reports new-vs-existing row counts so idempotency is visible. Boundary-layer error handling (unknown source / missing file / unparseable → exit 1). Added `DataSettings.bronze_path` (`DATA_BRONZE_PATH`) and `bronze_row_count` helper — `cli.py`, `ingest/dedup.py` (2026-07-18). **Phase 2 ingestion pipeline demoable end-to-end.**
- [x] Idempotent re-ingestion: every bronze row carries a deterministic `row_hash` (keyed on `external_id` when present, else content `source|posted_on|amount|description_raw`); the pipeline reads a source's already-landed hashes and filters them before appending, so re-dropping the same file — or an overlapping export — adds no duplicates. Bronze stays append-only (never mutated/deleted). Works around dlt filesystem having no merge disposition — `ingest/dedup.py`, `pipeline._run` (2026-07-18)
- [x] dlt pipeline: OFX/QFX exports into bronze via ofxtools (1.x SGML / 2.x XML / QFX). TRNAMT already signed so no sign_convention; FITID → external_id (idempotency key). `run_ingestion` now dispatches on source.kind; shared pipeline/unwrap logic. Also fixed synth OFX to be spec-valid (added required LEDGERBAL) so the strict parser accepts the fixture — `ingest/ofx_source.py` (2026-07-18)
- [x] dlt pipeline: CSV bank/CC exports into bronze Parquet, with provenance (source/account/currency/source_file/ingested_at on every row) — `personal_finance.ingest` (csv_source.py, pipeline.py). Config-driven: `SourceConfig` gained `has_header`/`skip_rows`/`columns`/`sign_convention` (signed/inverted/debit_credit) covering the capability matrix in docs/source-schemas.md. Verified end-to-end against real synth fixtures for chase_checking, venmo, wells_fargo (headerless), bofa_checking (skip_rows), capital_one/citi (debit_credit), amex (inverted) (2026-07-12)

- [x] `pf` CLI entrypoint: `synth` / `init-db` / `transform` working end-to-end, `ingest` / `enrich` stubs pointing at their phases — `cli.py`, typer + `[project.scripts]` (2026-07-12). **Phase 1 complete.**

- [x] dbt-duckdb skeleton: `transform/` project with silver/gold models over seeded categories, relationships test replacing the dropped FK, recursive gold_category_paths mart; dbt build runs inside pytest so dbt data tests gate CI with no workflow change; mashumaro override for Python 3.14 (2026-07-12)

- [x] Receipt fixtures: JSON payloads (vision-LLM output shape) + text renderings decomposed from scenario grocery charges, with ground-truth manifest for Phase 5 matching eval — `synth/receipts.py` (2026-07-12). Image rendering deferred to Phase 5 (needs pillow).

- [x] Dummy-data generator `personal_finance.synth`: deterministic scenario + 15 export formats (14 CSV layouts incl. quirks + OFX 1.02), correlated transfer pairs for Phase 3 — `synth/scenario.py`, `synth/writers.py` (2026-07-12)

- [x] Seed taxonomy into DuckDB: deterministic UUIDv5 category IDs, idempotent upsert preserving user notes — `seed.py`; dropped declared FKs due to DuckDB update-as-delete+insert limitation (integrity moves to dbt tests) (2026-07-11)

- [x] YAML configuration system: Pydantic-validated loaders for sources, taxonomy, rules, budgets — `user_config.py`, sample `config/*.yaml` (2026-07-11)
- [x] Add least-privilege `permissions` blocks to CI/CD workflows (code-scanning fix) (2026-07-11)

- [x] Define core schema (accounts, transactions, transaction_splits, categories, merchants, documents, links, budgets, labels) as Pydantic models + DDL — `models.py`, `ddl.py` (2026-07-11)
