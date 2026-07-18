# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 2 — Ingestion

> Phase 1 (Foundation) is complete — demo verified 2026-07-12: `pf synth` → fixtures,
> `pf init-db` → seeded warehouse, `pf transform` → dbt build PASS=11.

- [ ] ⏳ IN PROGRESS — Idempotent re-ingestion (same file twice ≠ duplicate transactions) — bronze is currently append-only; dlt's filesystem destination doesn't support merge
- [ ] Watch-folder ingestion (drop a file, it gets picked up)
- [ ] Wire `pf ingest` to the dlt pipelines (`run_ingestion` already dispatches on source.kind)

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 3–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

## Done

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
