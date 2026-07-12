# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 1 — Foundation

- [ ] ⏳ IN PROGRESS — Dummy-data generator: fake receipt images/JSON matching real receipt structure (see Costco anatomy in docs/source-schemas.md)
- [ ] DuckDB + dbt-duckdb project skeleton: bronze/silver/gold layers, dbt tests wired into CI
- [ ] `pf` CLI entrypoint (`pf synth`, stubs for `pf ingest` / `pf transform` / `pf enrich`)

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 2–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

## Done

- [x] Dummy-data generator `personal_finance.synth`: deterministic scenario + 15 export formats (14 CSV layouts incl. quirks + OFX 1.02), correlated transfer pairs for Phase 3 — `synth/scenario.py`, `synth/writers.py` (2026-07-12)

- [x] Seed taxonomy into DuckDB: deterministic UUIDv5 category IDs, idempotent upsert preserving user notes — `seed.py`; dropped declared FKs due to DuckDB update-as-delete+insert limitation (integrity moves to dbt tests) (2026-07-11)

- [x] YAML configuration system: Pydantic-validated loaders for sources, taxonomy, rules, budgets — `user_config.py`, sample `config/*.yaml` (2026-07-11)
- [x] Add least-privilege `permissions` blocks to CI/CD workflows (code-scanning fix) (2026-07-11)

- [x] Define core schema (accounts, transactions, transaction_splits, categories, merchants, documents, links, budgets, labels) as Pydantic models + DDL — `models.py`, `ddl.py` (2026-07-11)
