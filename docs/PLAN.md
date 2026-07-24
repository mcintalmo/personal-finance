# Build Plan

> One phase per milestone. **Each phase ends in a demoable state on dummy data** and keeps
> agent sessions scoped to one milestone at a time. The live task list with the in-progress
> marker is [TODO.md](../TODO.md) at the repo root; feature detail is in
> [FEATURES.md](./FEATURES.md); rationale in [ARCHITECTURE.md](./ARCHITECTURE.md).

## Phase sequence

| Phase | Milestone | Demo at the end |
|---|---|---|
| 1 | **Foundation** — schema, YAML config, taxonomy, dummy-data generator, DuckDB/dbt skeleton, `pf` CLI | `pf synth` writes realistic fake exports; dbt builds empty medallion layers; all checks green |
| 2 | **Ingestion** — dlt pipelines (CSV, OFX), bronze Parquet, watch-folder, idempotency | Drop a fake bank export in a folder → rows appear in bronze with provenance; re-drop → no dupes |
| 3 | **Core cleaning** — dedup, normalization, merchant cleaning, transfer detection | Silver tables with clean transactions; the Venmo +320 / bank −320 pair is linked and excluded from spend; dbt tests pass |
| 4 | **Categorization** — rules → embeddings → LLM cascade, review queue backend, labels | Every dummy transaction categorized with confidence + provenance (which cascade stage); corrections persist |
| 5 | **Line items (order history)** — Amazon/Costco order-history ingestion, order↔charge matching, splits | A fake Amazon order-history export → line items attached to the matching card charge → "apples" queryable |
| 6 | **Serving** — FastAPI, Streamlit dashboards (sunburst, Sankey), budgets, review-queue UI, config editing | Working local web app over dummy data: drill from total spend to line items; edit a budget; approve a categorization |
| 7 | **Intelligence** — NL chat, recurring detection, forecasting, trend callouts | Ask "how much did I spend on groceries last month?" in chat and get a correct, mart-backed answer |
| 8 | **Automation & polish** — Dagster, email ingestion, optional SimpleFIN/Superset, encryption, hardening | End-to-end hands-off: new file → scheduled pipeline → dashboard updates; security pass complete |
| 9 | **Visual receipt parsing** — vision LLM parsing of photo/PDF receipts, receipt↔charge matching | Photo of a fake grocery receipt → line items attached to the matching card charge → "apples" queryable |

## Working agreement (applies every phase)

1. Update `TODO.md` before starting work: exactly one task marked `⏳ IN PROGRESS`.
2. Tests accompany the implementation (pytest for Python, dbt tests for models); `/run-checks` green before done.
3. New patterns → YAML config, not hardcoded. New agent-behavior rules → `AGENTS.md`.
4. Spikes are allowed for experimentation but live in `scratch/` (gitignored) or are deleted; final code follows AGENTS.md conventions.
5. Dummy data only. No real financial data in the repo, tests, fixtures, or screenshots.
6. Dependencies added only via `uv add`, confirmed against the phase's scope.

## Sequencing rationale

- Dummy data comes first because everything downstream needs fixtures.
- Cleaning precedes categorization: classifiers should see normalized merchants, not raw descriptors.
- Order-history line items precede the UI so the UI can be built against full-granularity data from day one; visual (photo/PDF) receipt parsing is deferred past automation since it's an additional ingestion channel onto the same splits model, not a prerequisite for it.
- Chat comes after marts: the agent is only as good as the gold layer beneath it.
- Orchestration comes last: CLI-first avoids paying Dagster's ceremony before there is a pipeline worth scheduling.
