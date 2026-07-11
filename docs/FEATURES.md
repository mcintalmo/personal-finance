# Feature List

> Grouped by build phase (see [PLAN.md](./PLAN.md)). Each feature should be demoable on
> dummy data when complete. Architecture rationale lives in [ARCHITECTURE.md](./ARCHITECTURE.md).

## 1. Foundation
- [ ] Core schema: accounts, transactions, transaction_splits, categories, merchants, documents, links, budgets, labels
- [ ] YAML configuration system (Pydantic-validated): sources, taxonomy, rules, budgets
- [ ] Hierarchical category taxonomy seeded from YAML (arbitrary depth)
- [ ] Dummy-data generator (`personal_finance.synth`): realistic OFX/CSV/receipt fixtures matching real export schemas
- [ ] DuckDB + dbt-duckdb project skeleton with medallion layers and CI-enforced dbt tests
- [ ] `pf` CLI entrypoint

## 2. Ingestion
- [ ] dlt pipeline: CSV bank/CC exports (per-source YAML column mapping, custom source names)
- [ ] dlt pipeline: OFX/QFX exports
- [ ] Bronze layer: immutable Parquet landings with source/file provenance
- [ ] Watch-folder ingestion (drop a file, it gets picked up)
- [ ] Idempotent re-ingestion (same file twice ≠ duplicate transactions)

## 3. Core cleaning (silver)
- [ ] Deduplication, type normalization, sign conventions
- [ ] Merchant descriptor cleaning and normalization (raw string → merchant entity)
- [ ] Transfer detection: correlate paired movements across accounts (amount negation + date window + account pair) and exclude from spend
- [ ] dbt data tests on every silver model

## 4. Categorization
- [ ] Rules engine: user-editable YAML merchant/pattern → category rules
- [ ] Embedding-similarity classifier vs. labeled history (nomic-embed-text via Ollama)
- [ ] Local LLM fallback for the ambiguous tail
- [ ] Human review queue for low-confidence assignments; corrections stored as labels and fed back to the classifier
- [ ] Category rollups through the hierarchy at every level

## 5. Receipts & line items
- [ ] Receipt upload (photo/PDF) → vision LLM via Ollama → structured JSON (merchant, date, total, line items)
- [ ] Receipt ↔ transaction matching; transaction decomposition into splits
- [ ] Amazon order-history CSV ingestion and order ↔ charge matching
- [ ] Line-item categorization through the same cascade (enables "spend on apples this year")

## 6. Serving & visualization
- [ ] FastAPI API layer over gold marts
- [ ] Streamlit app shell: overview dashboard (net flow, spend over time, top movers)
- [ ] Sunburst drill-down of the category hierarchy
- [ ] Sankey of money flow (income → accounts → category subtrees)
- [ ] Budget buckets: define in YAML/UI, budget vs. actual views
- [ ] Review-queue UI (approve/correct categorizations and matches)
- [ ] Config editing from within the app

## 7. Intelligence
- [ ] NL chat agent (Ollama tool-calling over governed gold-mart queries)
- [ ] Recurring-expense detection (heuristic dbt model: merchant + amount + cadence)
- [ ] Forecasting of spend/income (statsforecast)
- [ ] Trend and anomaly callouts on the dashboard

## 8. Automation & polish
- [ ] Dagster orchestration: asset graph over dlt sources + dbt models, scheduled runs
- [ ] Email receipt ingestion (IMAP, local parsing)
- [ ] Optional: SimpleFIN Bridge for automatic bank sync
- [ ] Optional: dockerized Apache Superset over the gold layer ("power analyst" mode)
- [ ] DuckDB at-rest encryption; security hardening pass
- [ ] AI-generated visuals in chat (stretch)
- [ ] Real-data onboarding guide (local-only, never in repo)
