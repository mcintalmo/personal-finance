# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 1 — Foundation

- [ ] ⏳ IN PROGRESS — Seed hierarchical category taxonomy from YAML into DuckDB (flattening via `taxonomy_to_categories` is done; remaining: idempotent insert into the `categories` table)
- [ ] Dummy-data generator `personal_finance.synth`: realistic CSV/OFX bank + credit card exports
- [ ] Dummy-data generator: fake receipt images/JSON matching real receipt structure
- [ ] DuckDB + dbt-duckdb project skeleton: bronze/silver/gold layers, dbt tests wired into CI
- [ ] `pf` CLI entrypoint (`pf synth`, stubs for `pf ingest` / `pf transform` / `pf enrich`)

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 2–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

## Done

- [x] YAML configuration system: Pydantic-validated loaders for sources, taxonomy, rules, budgets — `user_config.py`, sample `config/*.yaml` (2026-07-11)
- [x] Add least-privilege `permissions` blocks to CI/CD workflows (code-scanning fix) (2026-07-11)

- [x] Define core schema (accounts, transactions, transaction_splits, categories, merchants, documents, links, budgets, labels) as Pydantic models + DDL — `models.py`, `ddl.py` (2026-07-11)
