# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 4 — Categorization

> Phase 1 (Foundation) complete — demo verified 2026-07-12.
> Phase 2 (Ingestion) complete — demo verified 2026-07-18: `pf synth` → fixtures,
> `pf ingest`/`pf watch` → idempotent bronze Parquet (CSV + OFX), source inferred or `--source`.
> Phase 3 (Core cleaning) complete — demo verified 2026-07-19: `pf transform` → silver
> transactions/merchants/transfers; the Venmo −X ↔ bank +X pair is linked and excluded from spend;
> dbt data tests pass on every silver model.

- [ ] ⏳ IN PROGRESS — Local LLM fallback for the ambiguous tail
- [ ] Human review queue for low-confidence assignments; corrections stored as labels and fed
      back to the classifier
- [ ] Category rollups through the hierarchy at every level (gold mart over
      silver_transaction_categories + gold_category_paths)

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 4–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

**Phase 3 merchant follow-ups** (deferred — evaluate existing tooling before hand-rolling more):

- [ ] Merchant normalization — leverage existing data, don't hand-roll: evaluate Python
      libraries (e.g. cleanco) and public merchant/brand datasets (MCC lists, OpenCorporates,
      merchant-name normalization corpora) to replace/augment the regex macro. **Do this before**
      the config-driven aliases below.
- [ ] Config-driven merchant aliases: `merchants.yaml` regex→canonical name + place list to
      resolve city-only suffixes and brand variants the generic macro can't
- [ ] Merchant resolution for the outlier tail: fuzzy match / semantic (embedding) search /
      local-LLM classifier to map descriptors the deterministic cleaner can't resolve to a
      canonical merchant — feeds the Phase 4 categorizer

## Done

- [x] Embedding-similarity classifier: stage 2 of the categorization cascade. `pf enrich` embeds
      every distinct merchant not yet cached via a local Ollama call (new `personal_finance.embed`
      module — `httpx`-based `EmbeddingClient`, `settings.ollama.*`), caching vectors in a new
      `merchant_embeddings` table (keyed by (merchant_name, model), so re-running never re-embeds
      what's already cached). A new dbt model, `silver_transaction_categories_embedding`, matches
      each merchant stage 1 missed against the nearest rule-categorized merchant by
      `list_cosine_similarity`, assigning its category when the score clears
      `embedding_confidence_threshold` (dbt var, default 0.80) — confidence is the real similarity
      score, unlike stage 1's flat 1.0. `silver_transaction_categories_all` unions every stage so
      far (disjoint by construction) — the "every transaction categorized with confidence +
      provenance" view PLAN.md's Phase 4 demo checks. Requires `pf transform` (builds
      silver_transactions) → `pf enrich` (embeds) → `pf transform` again (builds the
      embedding-stage model against the now-cached vectors). Live-verified end-to-end against a
      real local Ollama server; dbt-side matching logic also covered by tests using hand-crafted
      synthetic vectors (known-exact cosine similarities), independent of any specific embedding
      model's behavior — `src/personal_finance/embed.py`,
      `transform/models/silver/silver_transaction_categories_embedding.sql` (2026-07-21).
      **Note:** hit a real bug in a stale, long-running local Ollama server (client v0.31.1
      installed vs. server v0.24.0 actually running) where `nomic-embed-text` collapsed unrelated
      short merchant names to byte-identical vectors; confirmed via a second model
      (`embeddinggemma`) on the same server, which embedded correctly. **Resolved** by restarting
      the Ollama app (now v0.32.1) — reran the full pipeline against the fixed server and confirmed
      properly differentiated, semantically sane embeddings (e.g. NETFLIX↔SPOTIFY scored highest at
      0.586, both streaming). With the real model working, the 20-merchant demo's genuine best
      cross-merchant matches top out around 0.54 (STARBUCKS↔TRADER JOE'S) — below the conservative
      0.80 default, so stage 2 correctly declines to guess rather than assign a shaky category; this
      is the threshold working as designed (wrong auto-categorization is worse than leaving a
      transaction for the LLM-fallback/human-review stages), not a bug. Confirmed the mechanism
      itself is sound by sweeping `embedding_confidence_threshold` down and inspecting real
      (sub-threshold) similarity scores.
- [x] Rules engine: `silver_transaction_categories` (stage 1 of the categorization cascade)
      applies config-driven pattern→category rules over `silver_transactions`. Rules are seeded
      from `rules.yaml` into a new `rules` table (`seed_rules`, wired into `pf init-db`; full
      replace on reseed — unlike categories, rules have no user-editable state to preserve).
      `category_id` is resolved via the existing deterministic `category_id_for_path` (no need for
      a gold-layer join). First match wins by file order (`priority`). `RuleConfig.applies_to` is
      now a validated enum (`description_raw`/`merchant_name`/`source`/`account_name`, default
      `merchant_name` — the cleaned, less-noisy target) instead of a free string, and its pattern
      is validated against **DuckDB's own RE2 engine**, not Python's `re` — they differ (no
      backreferences/lookaround; a mid-pattern `(?i)` doesn't apply globally), so a bad pattern now
      fails at config load instead of deep in a dbt build. Grain: at most one row per
      transaction_id (matched only); absent = not yet categorized, ready for the embedding/LLM
      stages to pick up. Hit and fixed a real DuckDB 1.5.4 engine bug along the way: a `CASE`
      picking one of several text columns, then `regexp_matches`-ed inside a cross join, could
      **segfault** (SIGSEGV) on a value containing a multi-byte character (an emoji in a Venmo
      note) — reproduced via real `dbt build` runs (not just isolated queries), fixed by
      restructuring to one `UNION ALL` branch per `applies_to` value instead of a `CASE`, and
      stress-tested crash-free across 18+ real builds with the emoji fixture intact — `ddl.py`,
      `models.py` (new `Rule` entity), `seed.py`, `user_config.py`,
      `transform/models/silver/silver_transaction_categories.sql` (2026-07-19)
- [x] Transfer detection: `silver_transfers` correlates paired inter-account movements — an
      outflow and inflow that negate (equal magnitude, opposite sign), same currency, different
      accounts, within `transfer_window_days` (dbt var, default 3). Matched 1:1 via mutually-best
      ranking so a repeated amount can't double-count. Corroborated by a name signal — when a
      leg's descriptor names the counterparty account (checking "VENMO CASHOUT" ↔ the Venmo
      account), `name_match`/`confidence=high` and the pair wins ranking ties (amount+date-only
      pairs are `medium`). `silver_transactions` gains `is_transfer` (both legs flagged) so
      spend/income can exclude money moved between your own accounts.
      Cleanly split `stg_transactions` (ephemeral grain) → `silver_transfers` → `silver_transactions`
      to avoid a ref cycle. dbt tests: unique/not_null + relationships on both legs; Python tests
      assert the 4 scenario pairs (card payment + Venmo cash-out × 2 months), 1:1 legs, and that
      excluding transfers reduces spend — `transform/models/silver/` (2026-07-19). **Phase 3 core
      cleaning complete** (silver_transactions/merchants/transfers, each dbt-tested).
- [x] Merchant descriptor cleaning: `normalize_merchant` dbt macro deterministically cleans a
      raw descriptor (upper-case; strip ACH/Venmo reference tails, processor prefixes like
      `SQ *`/`PP*`/`PAYPAL *`, store/reference numbers, domain suffixes, and a trailing `CITY ST`
      locality) into an UPPERCASE key. `silver_transactions` gains `merchant_name`; new
      `silver_merchants` dimension rolls it up (deterministic md5 `merchant_id`, transaction_count,
      total_outflow, first/last seen). A singular dbt test unit-tests the macro on curated cases
      (incl. processor prefixes absent from synth); relationships test ties transactions to the
      dimension. City-only suffixes and brand aliases deferred to the config-driven follow-up —
      `transform/macros/`, `transform/models/silver/` (2026-07-19)

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
