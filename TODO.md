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
> **Phase 4 (Categorization) complete** — demo verified 2026-07-22: every dummy transaction
> categorized with confidence + provenance across all four cascade stages (rules → embedding
> similarity → local-LLM fallback → human review), rolled up through the taxonomy at every level.

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 4–8. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

**Phase 3 merchant follow-ups** (deferred — evaluate existing tooling before hand-rolling more):

- [x] Merchant normalization evaluation + config-driven aliases: see Done below.
- [x] Merchant resolution for the outlier tail: see Done below.

## Done

- [x] Merchant resolution for the outlier tail (Phase 3 follow-up): embedding-similarity
      merge-candidate review queue, human-confirmed only — mis-merging two distinct real
      merchants silently corrupts spend history in a way a wrong category doesn't, so
      candidates are never auto-applied. Scoped to embedding similarity only (not a local-LLM
      pass) — reuses the existing `merchant_embeddings` cache (`pf enrich`) rather than adding
      a second mechanism. New `personal_finance.merchant_merge` module: `fetch_merge_candidates`
      self-joins cached embeddings by cosine similarity (default threshold 0.90), direction
      picked so the more-common (higher transaction-count) spelling is the suggested canonical
      name; `record_merge_decision` stores an accept/reject verdict in the new `merchant_merges`
      table (`personal_finance.models.MerchantMerge`) — same "human decision in its own table"
      shape as `Label` for categorization corrections, not written back into any YAML config. A
      decided merchant_name never resurfaces as a candidate, but a merchant that's only ever
      been a merge *target* stays eligible to absorb further distinct variants later. New CLI:
      `pf review merge-candidates`, `pf review merge <name> <canonical>`,
      `pf review reject-merge <name> <canonical>`. Applied in `silver_transactions.sql` after
      `merchant_aliases` resolution (exact-match, not regex; single-hop only — a merge target
      that's itself later merged elsewhere isn't chased further). **Live-verified end-to-end**
      against real Ollama (`nomic-embed-text`) on synth data: `pf review merge "SHELL OIL"
      "CHEVRON"` then `pf transform` collapsed `SHELL OIL` into `CHEVRON` in
      `silver_transactions.merchant_name`, confirmed absent from `merge-candidates` afterward
      while `CHEVRON` itself stayed eligible — `src/personal_finance/merchant_merge.py`,
      `src/personal_finance/models.py`, `src/personal_finance/ddl.py`, `src/personal_finance/cli.py`,
      `transform/models/silver/silver_transactions.sql` (2026-07-23).
- [x] Merchant normalization evaluation + config-driven aliases (Phase 3 follow-up). Evaluated
      the two tools TODO.md called out before hand-rolling more: `cleanco` strips legal-entity
      suffixes (Inc/LLC/GmbH) — a different problem from bank-statement descriptor noise, which
      `normalize_merchant` already targets; public datasets (MCC codes, OpenCorporates) don't fit
      either — MCC isn't present in consumer CSV/OFX exports, OpenCorporates is legal-entity
      registry data, and a live merchant-lookup API would leak real transaction descriptors off
      the local-first pipeline. Concluded: proceed directly to config-driven aliases. Two new
      YAML files: `merchants.yaml` (regex → canonical name, first match wins by file order, same
      seeded-table + cross-join pattern as `rules.yaml`) and `places.yaml` (known city names
      `normalize_merchant` can strip as a trailing locality with no state code to anchor on — the
      generic macro only strips "CITY ST" when a two-letter state follows). New
      `personal_finance.models.MerchantAlias` + `merchant_aliases` table (`seed_merchant_aliases`,
      wired into `pf init-db`); `known_cities` flows from config into `pf transform` as a dbt var
      (`--vars`), extending the macro with a new conditional stripping step. `merchants.yaml`
      resolution applied in `silver_transactions.sql` itself (not a separate model) so every
      downstream consumer — rules, embedding/LLM cascade, rollups — sees the canonicalized name.
      **Live-verified end-to-end**: `THAI GINGER BELLEVUE` (no state suffix, unlike the already-
      handled `CHEVRON 0093 BELLEVUE WA`) now normalizes to `THAI GINGER` with `places.yaml`
      listing "Bellevue" — a real synth-data merchant, not just an isolated fixture —
      `src/personal_finance/user_config.py`, `src/personal_finance/seed.py`,
      `transform/macros/normalize_merchant.sql`, `transform/models/silver/silver_transactions.sql`
      (2026-07-22).
- [x] Human review queue: the final stage of the categorization cascade, and the highest
      priority — unlike stages 1-3 (additive: each only covers what prior stages missed
      entirely), a human correction can **override** an earlier stage's wrong assignment, not
      just fill a gap. `pf review list [--limit N]` surfaces transactions no automated stage
      could confidently place (most recent first); `pf review label TRANSACTION_ID
      CATEGORY_PATH [--note TEXT]` records a correction as a `Label` (the existing
      `subject_kind=transaction` entity, previously defined but unused) — new
      `personal_finance.review` module (`fetch_review_queue`, `record_label`), reusing
      `llm_categorize.fetch_category_paths` to validate/resolve the category path rather than
      duplicating the recursive taxonomy query. A new dbt model,
      `silver_transaction_categories_human`, keeps only the latest label per transaction (a
      transaction can be corrected more than once) with a flat 1.0 confidence.
      `silver_transaction_categories_all` now unions the human stage **first**, with every
      automated stage's branch excluding what it covers — the one structural change other stages
      needed; stages 1-3's own models are untouched, still reporting their original (possibly
      since-overridden) assignment on their own. Requires `pf transform` → `pf review label` →
      `pf transform` again — `src/personal_finance/review.py`,
      `transform/models/silver/silver_transaction_categories_human.sql` (2026-07-22).
      **Live-verified end-to-end** on the real demo pipeline: reviewed the tail `pf classify`
      left (Venmo cash-outs, emoji-containing notes, ambiguous card-payment/autopay pairs),
      labeled one gap-filling correction (a THAI GINGER charge → `non-essentials/dining`) and one
      override of an existing rule match (a KROGER transaction, originally `essentials/groceries`
      by rule, relabeled `non-essentials/groceries`) — confirmed the combined view shows `human`
      for both while `silver_transaction_categories` (stage 1) still reports its own original,
      unmodified `rule` assignment underneath. **Phase 4 categorization cascade complete**: rules
      → embedding similarity → local-LLM fallback → human review, each with its own dbt model
      plus a combined view, all live-verified against real local services.
- [x] Local-LLM fallback: stage 3 of the categorization cascade. `pf classify` asks a local
      Ollama chat model (new `settings.ollama.chat_model`, default `phi3:mini` — already pulled
      on this dev machine) to pick a category for every merchant stages 1-2 (rules, embedding
      similarity) missed entirely, using structured JSON output (Ollama's `format` schema, no
      free-text parsing) so the response is `{category, confidence}`. New
      `personal_finance.llm_categorize` module — `LlmCategorizeClient` wraps `/api/chat`;
      `compute_missing_llm_categories` reads what's still uncategorized from
      `main_silver.silver_transaction_categories`/`_embedding`, asks once per distinct merchant,
      and caches into a new `merchant_llm_categories` table (keyed by (merchant_name, model), same
      idempotent-cache pattern as `merchant_embeddings`). Crucially, a merchant the model
      classifies into a category **outside the given list** (a real, observed failure mode of a
      small local model — see below) is left **uncached** rather than raising or trusting a
      hallucinated category — same "decline to guess" contract as stage 2's confidence gate, just
      enforced by membership-in-list instead of a numeric threshold. A new dbt model,
      `silver_transaction_categories_llm`, gates cached classifications by self-reported
      `confidence` clearing `llm_confidence_threshold` (dbt var, default 0.50).
      `silver_transaction_categories_all` now unions all three stages (still disjoint by
      construction). Requires `pf transform` → `pf classify` (asks + caches) → `pf transform` again
      (builds the LLM-stage model against the newly cached classifications) —
      `src/personal_finance/llm_categorize.py`,
      `transform/models/silver/silver_transaction_categories_llm.sql` (2026-07-22). **Live-verified
      end-to-end** against a real local `phi3:mini` on the full demo pipeline: of ~21 merchants
      stages 1-2 left uncategorized, only CHIPOTLE (a clean, unambiguous name) was confidently
      classified (`non-essentials/dining`, confidence 0.95) — the harder/noisier remainder
      (raw-ish descriptors, emoji, ambiguous strings like "PAYMENT THANK YOU -") were **declined**
      because the model's response named a category outside the given list, not cached, left for
      human review. This is the safety mechanism working as designed on a small, imperfect local
      model — no bad categorizations were ever written — not a defect; a stronger chat model
      (swappable via `settings.ollama.chat_model` / `pf classify --model`) should confidently cover
      more of the tail. The dbt-side gating logic is also covered by tests using a hand-crafted
      synthetic classification (independent of any specific chat model's behavior).
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
