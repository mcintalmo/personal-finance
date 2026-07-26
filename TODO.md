# TODO

> Live task list. **Exactly one task is marked ⏳ IN PROGRESS at any time.**
> Agents: read [docs/PLAN.md](docs/PLAN.md) for phase scope and the working agreement
> before picking up a task. Mark a task in progress before starting, done (`[x]`) when
> `/run-checks` is green.

## Phase 7 — Intelligence

> Phase 1 (Foundation) complete — demo verified 2026-07-12.
> Phase 2 (Ingestion) complete — demo verified 2026-07-18: `pf synth` → fixtures,
> `pf ingest`/`pf watch` → idempotent bronze Parquet (CSV + OFX), source inferred or `--source`.
> Phase 3 (Core cleaning) complete — demo verified 2026-07-19: `pf transform` → silver
> transactions/merchants/transfers; the Venmo −X ↔ bank +X pair is linked and excluded from spend;
> dbt data tests pass on every silver model.
> Phase 4 (Categorization) complete — demo verified 2026-07-22: every dummy transaction
> categorized with confidence + provenance across all four cascade stages (rules → embedding
> similarity → local-LLM fallback → human review), rolled up through the taxonomy at every level.
> Phase 5 (Line items) complete — demo verified 2026-07-25: a fake Amazon order-history export →
> line items attached to the matching card charge, decomposed into splits → categorized through
> the full four-stage cascade (rules → embedding similarity → local-LLM fallback → human review) →
> "spend on apples this year" queryable end-to-end, with a human override changing a spend rollup
> live (Costco/photo receipts deferred to Phase 9).
> Phase 6 (Serving) complete — demo verified 2026-07-25: `pf serve` (FastAPI over the gold marts)
> + `pf dashboard` (Streamlit + Plotly) — drilled from total spend down to individual Amazon line
> items on a live sunburst, watched income flow into accounts and out to top-level categories on a
> Sankey, edited `budgets.yaml` from the Config page and watched the change land in the Budgets
> page's actual-vs-budgeted chart, and approved a categorization from the Review Queue page.

Costco has no order-history export (confirmed against docs/source-schemas.md — in-app digital
receipts only), so Phase 5 targeted Amazon only; Costco (and other photo/PDF-only receipts) stays
in Phase 9 until vision-LLM parsing exists.

- [x] Recurring-expense detection (heuristic dbt model: merchant + amount + cadence): see Done below.
- [x] NL chat agent (Ollama tool-calling over governed gold-mart queries): see Done below.
- [x] Forecasting of spend/income (statsmodels): see Done below.
- [x] Trend and anomaly callouts on the dashboard: see Done below.

## Backlog (later phases)

See [docs/FEATURES.md](docs/FEATURES.md) — Phases 6–9. Tasks are promoted into this file
one phase at a time when the previous phase's demo is complete.

**Phase 3 merchant follow-ups** (deferred — evaluate existing tooling before hand-rolling more):

- [x] Merchant normalization evaluation + config-driven aliases: see Done below.
- [x] Merchant resolution for the outlier tail: see Done below.

**Phase 6 UI follow-ups** (deferred to Phase 8 — Automation & polish, per user feedback after the
Phase 6 demo):

- [ ] Dashboard filters/slicers (month range, category, account) — needs matching optional query
      params on the gold-mart endpoints (`start_month`/`end_month`/`category_id`/`account_name`)
      plus a shared sidebar filter widget threaded through Overview/Sunburst/Sankey/Budgets.
- [ ] Inline review-queue labeling: replace the ID-copy-paste flow with a category dropdown
      (`st.data_editor` + `SelectboxColumn`) populated from the real taxonomy
      (`llm_categorize.fetch_category_paths`), with an "add new category" affordance — no manual
      ID entry.
- [ ] Config editor as structured per-field forms (select/number/list add-delete) instead of raw
      YAML text. Evaluate `streamlit-pydantic` (or similar schema-driven form generation) before
      hand-rolling a bespoke form per config file — the configs are already pydantic models
      (`BudgetConfig` etc.), so a schema-driven renderer avoids 5 one-off forms. Keep
      `write_config_file`'s whole-config re-validation as the backend safety net regardless of
      what renders the input widgets.

**CLI polish** (pre-existing gap, affects both cascades — not scoped to any one phase):

- [ ] `pf transform` hardcodes its dbt `--vars` to only `known_cities`; `embedding_model`/
      `llm_model`/`*_confidence_threshold` always fall back to their defaults
      (`nomic-embed-text`/`phi3:mini`/etc.) regardless of what `--model` a user passed to
      `pf enrich`/`pf classify`. A user who overrides the model on enrich/classify won't see it
      picked up by `pf transform` unless they also override `Settings.ollama.*` to match — found
      while live-verifying the split-categorization cascade with `pf classify --model qwen2.5:3b`
      (had to invoke `dbt build --vars` directly, bypassing `pf transform`, to prove the mechanism).
      Needs `pf transform` to expose matching `--embedding-model`/`--llm-model`/etc. options (or
      read them from `Settings.ollama` instead of dbt defaults).

## Done

- [x] Phase 7 stage 6 — `pf chat`: a Pydantic AI agent over local Ollama, answering from the
      Stage A MCP tools. Stage B of the agent plan.

      **The agent owns no data access.** Every figure it can state comes from the MCP tool
      server over HTTP; `personal_finance.agent` never opens the warehouse. That is what keeps
      Stage A's read-only guarantee meaningful — it is enforced in the *other* process, so
      nothing here could weaken it even if the model asked.

      **The tool server must be out-of-process, and that is a constraint rather than a
      preference.** DuckDB refuses a read-only connection while the same *process* holds a
      read-write one, and the FastAPI app the agent is mounted on opens read-write connections
      for review labeling — so in-process (FastMCP's in-memory transport) the two would race on
      whichever opened first. Worse, `enable_external_access=false` is GLOBAL to the DuckDB
      instance, so an in-process server would apply it to the API's own connections too: the
      same class of global-setting bug that had to be fixed twice in Stage A. Out of process,
      none of it is reachable. `settings.mcp.url` is therefore a *client* address, deliberately
      separate from the `MCP_HTTP_HOST`/`PORT` bind settings — binding `0.0.0.0` says nothing
      about where a client should connect.

      Two surfaces, one agent: `POST /agent` on the existing FastAPI app (AG-UI event stream,
      via `AGUIAdapter.dispatch_request`) for a frontend to drive, and `pf chat` (Pydantic AI's
      built-in web UI) so Phase 7's demo criterion is reachable now rather than waiting on the
      Dash frontend in Stage C.

      `settings.ollama.agent_model` is separate from `chat_model` because it is a different job,
      not a bigger version of one: `chat_model` categorizes a single merchant string thousands
      of times, where 3B is the right trade; this drives a multi-step loop over twelve tools and
      writes SQL, which needs dependable function calling and far more context. Defaults to
      `qwen3:8b`.

      **Both preconditions are checked up front, not at the first question.** Ollama pulls
      nothing implicitly and the tool server may not be running, and either failure would
      otherwise surface halfway through a streamed answer as "All connection attempts failed" —
      naming neither the thing that is down nor the command that starts it. `pf chat` refuses to
      boot and `POST /agent` returns a 503 that names `ollama pull <model>` / `pf mcp --http`.

      Tests use a `FunctionModel`/`TestModel` against the **real** MCP server over FastMCP's
      in-memory transport, so tool calls run genuine SQL against a genuine DuckDB warehouse — no
      Ollama, no listening socket, but a tool whose result shape changed still fails here. That
      covers the retry path too: a deliberately bad `run_sql` must come back carrying DuckDB's
      own message so the model can correct itself, which is why `mcp_server` hands back the real
      error rather than a sanitized one.

      **Live verification against real Ollama found two defects the tests could not.** Driving
      the agent with an actual local model showed it guess `main_silver.transactions`, receive
      DuckDB's catalog error, and then resend the *identical* query — dying on Pydantic AI's
      default of one retry, which buys a blind repeat rather than a correction. Two fixes:
      `retries=3` on the agent, and, more usefully, `run_sql` now answers a guessed name with
      the real ones. On a CatalogException it lists the queryable tables; on a BinderException
      it lists the columns of whichever tables the query actually named. Both come from
      `information_schema`, so there is nothing to keep in sync. This replaced a two-round-trip
      recovery (fail → `list_tables` → retry) with a one-round-trip one, and it matters because
      DuckDB's own hints mislead here: "Did you mean" suggested a name from an attached database
      that this tool cannot query, and "Candidate bindings" offered one unrelated column.
      Confirmed by re-running: with the table list in the error, the model went straight to the
      correct table on its next attempt. Kept off syntax errors deliberately — burying the
      parser's message, the one thing that locates a syntax error, under a wall of names would
      be a regression.

      **Not verified: whether the default `qwen3:8b` can author SQL well enough end to end.**
      Only `qwen2.5:3b` is pulled on this machine, and it answers curated-tool questions
      correctly ($18,876.63 over 255 grocery transactions, matching the mart exactly) but cannot
      reliably write SQL against a schema it has to discover first — it invented table and
      column names past the point where better errors could help. That is the model-capability
      gap `agent_model` exists to close, and it is why the default is not 3B.

      Also fixed comment rot in `pf mcp`'s docstring, which still described the bronze-directory
      allowlist that Stage A's final design removed.

- [x] Phase 7 stage 5 — `pf mcp`: a governed MCP tool server over the warehouse. Stage A of the
      agent plan: expose the data as tools first, so the agent (and Claude Desktop, and a future
      React/Dash frontend) all plug into one governed surface rather than each growing its own.

      **Sibling of `api.py`, not a layer on it.** Both are thin adapters over the same library
      modules. Deliberately NOT `FastMCP.from_fastapi(app)` despite it being a one-liner: that
      produces tools shaped like HTTP routes rather than like questions, and — more seriously —
      would expose `PUT /config/{name}`, handing an agent the ability to rewrite the user's YAML.

      Twelve tools: schema discovery (`list_tables`, `describe_table`, also published as MCP
      resources), nine curated marts tools, and `run_sql` for open-ended analysis. The curated
      tools alone would have made an agent that can only read the same tables the dashboard
      already shows; `run_sql` is what makes real analysis possible.

      **The read-only guarantee is enforced by DuckDB, not by inspecting the model's SQL** — there
      is no keyword blocklist to talk past. Three settings, each verified by attacking it:
      `read_only=True` (INSERT/UPDATE/DROP/CREATE/ATTACH all refused at the engine);
      `enable_external_access=false`, because a read-only connection will otherwise happily
      `read_csv('/etc/passwd')` straight into the model's context, and DuckDB refuses every
      attempt to switch it back on (`SET`, `SET GLOBAL`, `PRAGMA`, `RESET`); and
      `allowed_directories` scoped to the bronze landing zone.
      **That last one came out of a real bug the mart-level tests caught.** The silver layer is
      dbt *views over bronze Parquet*, so banning external access outright made every
      transaction-level query fail with a permission error — the guard looked like it worked
      while silently removing half the warehouse. A synthetic all-tables fixture cannot catch
      that, which is exactly why the tool SQL is also exercised against a real dbt-built
      warehouse. The allowlist cannot be widened from SQL, and `COPY ... TO` stays refused, so it
      is not a hole.
      `run_sql` is additionally bounded by a row cap (reported via a `truncated` flag rather than
      silently) and a timeout implemented by interrupting the connection from a timer thread — a
      cartesian join costs a model nothing to write and would otherwise hang the chat forever.

      Also fixed a confusing failure mode: DuckDB refuses a read-only connection while the same
      *process* holds a read-write one (dbt-duckdb keeps its connection open after a build), and
      its own error message does not hint at why. Now caught and explained.

      **A pre-merge review found a real hole in that guarantee, and it was the central claim of
      the PR.** `allowed_directories` confers WRITE as well as read, and DuckDB has no read-only
      variant (`allowed_paths` blocks writes but also blocks the directory listing a glob needs,
      so it cannot serve the views). So `COPY ... TO '<bronze>/x.parquet'` succeeded through
      `run_sql` — and because the silver views glob that directory, the injected rows appeared on
      the very next query with no `pf transform`. Reproduced end to end: a mismatched schema
      instead throws a permanent ConversionException that breaks every transaction-level query
      until the file is deleted by hand. The docstring's claim that "COPY ... TO stays refused"
      was simply false.
      A first fix scoped the allowlist per connection — granted to our SQL, withheld from the
      model's. **A second review finding killed that too:** `allowed_directories` and
      `enable_external_access` are **GLOBAL to the shared DuckDB instance**, not per connection,
      so the grant leaked to any connection open at the same moment (verified: the write
      succeeded again), and the reverse interleaving crashed with `Cannot change
      allowed_directories when enable_external_access is disabled`. MCP hosts call tools in
      parallel, so this was not hypothetical.
      **The real fix was to remove the need for the grant: silver is now materialized as tables**
      (`transform/dbt_project.yml`, `+materialized: table`). Nothing in the warehouse reads from
      disk, so every connection runs with no filesystem access at all — no allowlist to leak, no
      GLOBAL-setting conflict, and, as a bonus, `run_sql` reaches the *whole* warehouse including
      silver rather than gold-only. Measured cost: warehouse 6.5 MB -> 11 MB on 18 months of
      synth data. Strictly better on every axis than the design it replaced.
      The same review found the toggle test was **vacuous** (DuckDB refuses to change
      `enable_external_access` on a running database regardless of our config, so it passed with
      every guard removed — it now asserts a follow-up read still fails), the ATTACH test
      attached a *nonexistent* database (so it failed on IO, not on the guard), and `run_sql`
      **silently dropped duplicate column names** — `SELECT a.*, b.*` returned half the columns
      with `row_count` unaffected, which is exactly the shape of wrongness that reads as data.
      Curated tools now return the same `{row_count, truncated, rows}` envelope as `run_sql`
      rather than a bare list, so a capped page can no longer read as a complete answer.

      **Prompt injection is a live concern, not a theoretical one** — merchant names come from
      ingested bank exports and land directly in the model's context. The mitigation is
      structural rather than textual: there is no write path to reach, so a successful injection
      buys a wrong answer, not a changed ledger.

- [x] Phase 7 stages 3-4 — Trend/anomaly callouts, and recurring detection extended to inflows.

      **Recurring detection now covers both directions.** `gold_recurring_expenses` became
      `gold_recurring_flows`, with a `flow` column and a positive `amount` magnitude. The
      detection heuristic was already direction-agnostic apart from a `where amount < 0`, so this
      is one generalized model rather than a second near-duplicate one — a model named
      `..._expenses` that contains salary would be comment rot by construction. Grouping is on the
      *signed* amount so a merchant that both charges and refunds $40 stays two distinct groups.
      A **biweekly** cadence bucket (`[12, 16]` days) was added specifically for income: a
      fortnightly or semi-monthly paycheck averages ~14-15 days, which falls in the gap between
      the weekly and monthly buckets — without it the whole inflow extension would have been a
      no-op for the commonest salary cadence, silently. Verified end-to-end: the synth scenario's
      $2,500 semi-monthly payroll is now detected (biweekly, 12 occurrences over 6 months) and
      `gold_forecasts` carries $5,000/month of *committed* income where it previously carried
      $0.00 and asked a statistical model to re-derive a known salary. The forecaster's
      committed/variable join now matches on flow as well as magnitude, and budget series still
      take outflow groups only — a paycheck landing in a budgeted subtree must not be projected
      as committed spend.

      **Callouts** — new `personal_finance.callouts` (`pf callouts`, `GET /callouts`, a
      `6_Callouts.py` page plus a top-3 band on Overview). Three kinds: SPIKE/DIP (a recent month
      far from that series' own typical month), TREND (from `gold_forecasts.trend`, compared
      against the history average), and BUDGET_RISK (next month's forecast against the budget).
      Deliberately **not persisted**, unlike forecasts: there is no expensive fit to cache, and a
      callouts table would be a derivation of a derivation with its own staleness window between
      `pf forecast` and the next `pf transform`. It reads the `forecasts` app table rather than
      `gold_forecasts` for the same reason — a callout is a claim about right now.
      Anomalies use the **modified z-score** (median + MAD, Iglewicz & Hoaglin, cutoff 3.5) rather
      than mean/stddev: on a personal ledger the outlier is often several times the typical month
      and would drag a mean and inflate a stddev enough to mask itself. Guards that exist to keep
      the feed worth reading: a $50 absolute-deviation floor (a scale-free z-score makes $4 against
      a $1 median look enormous and uninteresting), a 3-month recency window, a minimum of six
      months of history, and a mean-absolute-deviation fallback for the mostly-zero categories
      where MAD is exactly 0 — the very series where one big month matters most.
      Severity is not a pure function of magnitude: rising spend and falling income are both
      WARNING, rising income and falling spend are both INFO, and a budget overrun that survives
      the low end of the forecast interval escalates to CRITICAL.
      A test caught a real inverted conversion in the budget comparison — `_MONTHS_PER_PERIOD`
      says how many months a period spans, so the monthly-equivalent cap is the budget *divided*
      by it; multiplying turned a $6,000/year cap into $72,000 and would have made every yearly
      budget read as permanently, silently under budget.

      **A six-agent pre-merge review found one user-facing bug and several vacuous tests.**
      The callout prose said "next month is projected at $X" about the month *currently in
      progress*: horizon 1 is `trained_through + 1`, and `trained_through` is the last COMPLETE
      month, so it was off by one every single time. Worse, `pf forecast` is run by hand, so its
      rows can be months old — selecting `horizon = 1` blindly would nudge the user about a month
      that had already ended. Both fixed: the callout names the month, and the query now selects
      the nearest forecast month at-or-after the current one, so a fully stale forecast yields no
      rows and the UI asks for a re-run.
      Also fixed: `_MAD_TO_SIGMA`'s docstring had the operation inverted (said "MAD * this" where
      the code correctly divides) — dangerous because a maintainer would "fix" the code to match
      and shrink the scale ~2.2x, flagging almost every month; `get_optional` swallowed *every*
      non-2xx rather than the 503 its docstring reasoned about, so a 500 rendered identically to
      "no callouts"; timeouts weren't caught at all, and `/callouts` is the likeliest endpoint to
      hit one; and `ForecastRow`'s positional unpacking was guarded for arity but not order, so
      swapping `lower_bound`/`upper_bound` would silently invert the CRITICAL/WARNING split.
      A new `Flow` enum replaces the bare inflow/outflow strings: the partition dropped an
      unrecognized value from *both* halves, and money vanishing that way is invisible to
      `predicted == committed + variable`, which still holds perfectly.
      **Three tests were vacuous and one was tautological.** Nothing would have caught income
      double-counting if the committed/variable join regressed (the projection and the history
      split are independent paths, and the sum invariant holds fine at twice the right value);
      nothing verified the budgets LEFT JOIN, so `BUDGET_RISK` could have been dead code;
      `test_limit_keeps_the_most_notable` used a one-callout feed with `limit=1`; and the rank
      test asserted the list was sorted by its own sort key, which passes if every rank is 0.
      Biweekly projection had no unit test at all despite being the reason `_CADENCE_MONTHS`
      omits it — there is now one proving three fortnightly paychecks really do land in one month.
      **One finding was rejected after checking:** a reviewer argued `_BUDGET_SQL` front-pads
      budget history with fake zeros because it ignores `budgets.starts_on`, fabricating an
      anomaly for every new budget. Budget history is the *category's* real spend, not
      budget-scoped — verified directly (Groceries: 18 months of genuine spend, zero zero-months).
      A new budget over an established category inherits that real history, so there was nothing
      to fix.

      **Test suite sped up ~4x along the way** (measured, not estimated). `test_api.py` was
      207s, of which 203s was fixture setup: `built_warehouse` was function-scoped, so ten tests
      each paid a full init-db + synth + 3 ingests + dbt build (~20s) to exercise under three
      seconds of assertions. It now builds once per session and each test gets a *copy* of the
      file, keeping full isolation (one test writes labels) for the price of a file copy — 207s
      to 24s. That copy needed an explicit `CHECKPOINT`: closing a DuckDB connection does not
      fold the WAL into the database file, so copying `warehouse.duckdb` alone silently produced
      a warehouse with the app tables but no silver views or gold tables, surfacing as a
      baffling 503 from every endpoint.
      Added `pytest-xdist` with `-n auto --dist loadfile`. `loadfile` is deliberate: the 13
      module-scoped warehouse fixtures in `test_dbt.py` each cost a dbt build, and the default
      `load` or `loadscope` would split a file across workers and make each one rebuild the
      fixtures its share of the tests needs — slower than serial. That in turn required
      isolating dbt's artifacts per worker (`DBT_TARGET_PATH`/`DBT_LOG_PATH` in
      `tests/conftest.py`): dbt defaults both to `transform/target` and `transform/logs`, so
      concurrent invocations from different workers would race on `partial_parse.msgpack` — a
      corruption whose symptom is an unreproducible parse failure on an unrelated test.
      Not done, and worth knowing: `dbt build` is 9.0s where `dbt run` is 3.5s, so data tests are
      ~60% of every fixture's cost. Switching the fixtures that don't assert on data-test results
      would save ~70s, but today those 13 builds re-validate all 215 data tests against 13
      different data scenarios; only `TestDbtBuild` covers one. That is a real loss of coverage,
      not a free win, so it was left alone.

- [x] Phase 7 stage 2 — Spend/income forecasting: new `personal_finance.forecast` (`pf forecast`)
      + `gold_forecasts`. Forecasts total income, total spend, and every configured budget's
      category subtree. Each month is **decomposed**: the committed part (recurring charges from
      `gold_recurring_expenses`) is projected forward deterministically, and only the variable
      remainder is statistically modelled — so the prediction interval covers the variable
      component alone. A 100%-subscription category therefore gets a zero-width band while a
      discretionary one gets an honest wide one (verified live: the Streaming budget forecasts
      $27.48 committed / $0.00 variable / lower == upper).
      **Library choice changed from the planned statsforecast to statsmodels**: statsforecast
      caps `pandas<3.0.0` (as does its utilsforecast dep) and the user wants to stay on pandas 3,
      and — more decisively — its conformal intervals need >= 7 samples and `AutoETS` >= 16, so it
      fails in exactly this project's 6-24-month regime. statsmodels has no pandas cap and fits
      at n=6. Candidate panel (naive / mean-of-3 / Theta / ETS) is gated by history length and
      selected per-series by rolling-origin MASE; intervals are conformal quantiles of the
      backtest residuals, widened by sqrt(h). Trend uses a Theil-Sen slope, not OLS — a test
      caught that a single expensive month tilts an OLS line enough to report FALLING, which is
      exactly the "up-up-up vs. one pricey month" distinction this is meant to answer.
      Deliberate guards: the partial current month is excluded from training (otherwise every
      model forecasts a spurious decline), series under six complete months are skipped rather
      than guessed at, and the history window starts at the ledger's first month — live
      verification caught that a fixed 36-month lookback padded the front with 30 fake zero
      months and corrupted MASE and the trend slope.
      Also fixed a recurring supply-chain issue: `uv add` had silently walked gitpython back to
      3.1.52 (five advisories) for the third time this session; `[tool.uv]`
      constraint-dependencies + exclude-newer-package now floor it durably.
      **A five-agent pre-merge review found eight real correctness bugs that the unit tests, the
      dbt data tests and CI had all passed over** — worth recording because several share one
      root cause. `_theta` was handed a plain list where statsmodels needs an array; the bare
      `except Exception` in `_safe_forecast` swallowed it, so Theta silently dropped out of every
      forecast ever produced. The existing test asserted Theta was *listed*, never that it *ran*.
      The same suppression habit hid an `invalid value encountered in divide` from ETS behind a
      blanket `warnings.simplefilter("ignore")`. Lesson carried forward: any swallow-path needs a
      test proving it is not being taken. Also fixed: `_project_committed` averaged committed
      totals instead of stepping each group's cadence, so an annual premium was subtracted from
      the modelled series and never projected back (money silently vanished) while a quarterly
      charge was projected into all three months; near-linear histories published multi-month
      extrapolations as zero-width "80%" intervals; the conformal quantile used `round()` where
      nearest-rank needs `ceil()`; negative forecasts inverted the interval bounds; and an
      unconditional `DELETE` outside a transaction let a transient empty upstream destroy good
      forecasts while exiting 0. Invariants are now enforced by a pydantic validator at
      construction rather than only by a dbt test after the rows are written.
      **Known limitation, deliberately left:** income never gets a committed component, because
      `gold_recurring_expenses` detects outflows only — a salary is the most predictable flow in
      a personal ledger and would benefit from the same decomposition if recurring detection is
      extended to inflows. *(Lifted in Phase 7 stage 3 — see above.)*

- [x] Phase 7 stage 1 — Recurring-expense detection: new `gold_recurring_expenses` dbt model
      *(renamed to `gold_recurring_flows` in stage 3 when it grew to cover inflows too)*
      groups outflows by `(merchant_name, amount)`, requires >= 3 occurrences, and classifies the
      average gap between charges into a weekly/monthly/quarterly/yearly cadence bucket, dropping
      groups whose gaps are irregular (stddev > `recurring_regularity_threshold` of the average).
      Reads `silver_transactions` directly (not `gold_line_items`) — a subscription charge is a
      whole-transaction concept tied to merchant_name, same rationale as `gold_monthly_flow`.
      Cadence day-ranges and the regularity threshold are dbt vars (`transform/dbt_project.yml`),
      matching the project's established tunable-heuristic convention (`transfer_window_days`,
      `embedding_confidence_threshold`). Verified against a 6-month synth warehouse: correctly
      detects the fixture's monthly rent/Netflix/Spotify charges (6 occurrences each) and correctly
      excludes random-amount grocery/gas/dining/Amazon spend, payroll (inflow), and the
      checking↔credit-card autopay transfer legs. Code review (8-angle) found and fixed a missing
      `'|'` separator in the `recurring_expense_id` hash (two angles converged on it independently)
      and a test-coverage gap on two derived columns; two lower-severity heuristic-fragility
      findings (weak evidence at the 3-occurrence floor, no per-account partitioning) were reported
      and left as-is — real but out of scope for this heuristic's first cut.

- [x] Phase 6 — Serving: `personal_finance.api` (FastAPI) over the gold marts, and
      `personal_finance.webapp` (Streamlit + Plotly), wired together by two new CLI commands
      (`pf serve`, `pf dashboard`). New gold models close a real gap the Phase 5 backlog had
      flagged ("the implicit-split union view is a gold-layer concern, not built yet"):
      `gold_line_items` unions each Amazon-decomposed transaction's splits (not the transaction
      itself) with every other transaction as its own single line item, so "spend on apples"
      rolls up through the same mechanism as every other category instead of a one-off query.
      `gold_category_rollups` now sources from `gold_line_items` (gained `parent_id`, ready for a
      Plotly sunburst's ids/parents/values shape) instead of joining transactions directly —
      verified backward-compatible: all pre-existing rollup tests pass unchanged against fixtures
      with no Amazon data. New `gold_monthly_flow` (net flow/spend-over-time), `gold_sankey_flow`
      (income → account → top-level category edges), and `gold_budget_actuals` (budget vs. actual,
      bucketed by each budget's own cadence via `date_trunc`). Budgets are now actually seeded:
      new `personal_finance.seed.seed_budgets` (`pf init-db`), `BudgetConfig` gained an optional
      `starts_on` (defaults to 2000-01-01 — "always active" — so existing `budgets.yaml` files
      don't need updating). New `personal_finance.user_config.read_config_file`/`write_config_file`
      power the Config page's editor — a write re-validates the **whole** configuration
      (cross-file referential integrity, not just the edited file's own schema) before touching
      disk. FastAPI endpoints reuse existing modules end to end (`personal_finance.review`,
      `personal_finance.llm_categorize.fetch_category_paths`) rather than duplicating logic — the
      API is a thin projection over gold/silver dbt models plus the CLI's own backend functions.
      **Live-verified end-to-end**: ran `pf serve` and `pf dashboard` against a real 6-month synth
      warehouse (chase_checking/amex/venmo/wells_fargo/bofa_checking/capital_one/citi + Amazon,
      210/210 dbt checks green) — drilled the sunburst from `essentials` down to `apples`
      ($32.03), watched the Sankey show real per-account income and essentials/non-essentials
      spend, edited `budgets.yaml` from the Config page and confirmed the write round-tripped and
      re-validated referential integrity (a bad category path is rejected, nothing is written),
      and exercised the Review Queue page's label flow against real uncategorized splits. Caught
      and fixed a real bug this way: `GET /review/queue` 500'd on a Venmo transfer leg with a NULL
      `description_raw` (no `not_null` dbt test on that column) — `TransactionReviewItem` had it
      typed as required `str`; fixed to `str | None`, pinned with a regression test. Every
      Streamlit page also smoke-tested exception-free via `streamlit.testing.v1.AppTest` against
      the live API. `src/personal_finance/api.py`, `src/personal_finance/webapp/`,
      `transform/models/gold/gold_line_items.sql`, `gold_monthly_flow.sql`, `gold_sankey_flow.sql`,
      `gold_budget_actuals.sql` (2026-07-25).
- [x] Amazon order-history CSV ingestion (Phase 5): lands Retail.OrderHistory.1.csv
      (Privacy Central export) into its own `bronze_amazon` dataset — not `bronze/`, which the
      transactions source's `bronze/*/*.parquet` wildcard would otherwise also sweep up and
      corrupt with NULL-heavy rows, since an order-history row (a shipment-item line, not a
      statement line) shares no columns with the generic transaction schema. New `SourceKind.AMAZON`
      (account_name/account_type now optional on `SourceConfig` — enrichment data isn't tied to
      one financial account); `personal_finance.ingest.amazon_source` (fixed external schema,
      hardcoded columns like OFX, no `column_map`); `personal_finance.ingest.dedup.compute_amazon_row_hash`
      (keyed on order + ASIN + ship date + occurrence, since the same ASIN can legitimately repeat
      across shipments or even within one on a split shipment). `pipeline._run`/`existing_row_hashes`/
      `bronze_row_count` gained a `dataset_name` parameter (default `"bronze"`, preserving every
      existing CSV/OFX source's behavior unchanged) so a non-transaction-shaped source can land
      under a different dataset — `dataset_name_for(source)` centralizes the kind → dataset mapping
      for `pipeline.py` and `watch.ingest_file`'s row-count reporting alike. New dbt models:
      `stg_amazon_order_items` (typed/deduped staging) and `silver_amazon_shipments` (grouped by
      order + ship date to match card-charge granularity — a multi-item order shipped in two boxes
      is two charges; `total_owed`/`shipping_charge`/`total_discounts` are shipment-level values
      Amazon repeats on every item row, taken once via `any_value`, never summed). New macro
      `read_parquet_or_empty`: most builds never ingest an Amazon file at all, and a plain
      `read_parquet()` on a non-matching glob throws immediately (even just to `CREATE VIEW`) —
      this checks file existence via `glob()` (compile-time, tolerant of zero matches) and falls
      back to a correctly-typed empty relation, the same "resolves to zero rows when its upstream
      hasn't run" contract every other cascade stage follows; called directly in
      `stg_amazon_order_items.sql` rather than through `source()`, since dbt's sources.yml `meta`
      properties render without custom project macros in scope. `personal_finance.synth.amazon_orders`
      generates a correlated Amazon order-history fixture (Amazon-category card charges added to
      `scenario.py` via an independent `amazon_rng` substream — critically NOT the shared `rng`,
      since inserting a new draw anywhere in that shared sequence would have shifted every other
      merchant category's amounts/dates for seed=42 and silently broken dozens of unrelated
      existing fixtures; caught and fixed during this task). New singular dbt test
      `assert_amazon_shipment_totals_reconcile`. **Live-verified end-to-end**: `pf synth` → real
      Amazon CSV with comma-containing product names (correctly quoted, round-trips through the
      `csv` module) → `pf ingest --source amazon` → `pf transform` (119/119 checks green,
      including with real ingested Amazon data, not just the empty-glob path) → confirmed
      `silver_amazon_shipments` aggregates the right item counts/totals and `silver_transactions`
      has zero NULL `account_name` rows (the bronze-glob-collision bug this task found and fixed
      via the `dataset_name` parameterization) (2026-07-24).
- [x] Amazon order ↔ card-charge matching (Phase 5): new `silver_amazon_order_matches` model —
      deterministic amount + date matching between `silver_amazon_shipments` and
      `silver_transactions`, same 1:1 ranking shape as `silver_transfers` (a shipment and its card
      charge are two records of one real-world event, like a transfer's two legs, not a fuzzy-match
      problem). A shipment's `total_owed` must negate a transaction's `amount`, currencies must
      match, and dates must fall within `amazon_match_window_days` (default 5) of each other;
      `row_number()` partitioned by shipment and by transaction keeps only mutually-closest pairs,
      so a shipment can't double-claim a charge or vice versa. New `schema.yml` entry with
      `not_null`/`unique`/`relationships` tests (uniqueness is on the (website_order_id, ship_date)
      pair, not website_order_id alone — an order that ships in two boxes produces two shipments).
      **Live-verified end-to-end** against the real scratch warehouse from the ingestion stage:
      ingested `chase_checking` + `amex` (the credit card Amazon charges post to) + `amazon`, ran
      `pf transform` (129/129 checks green), confirmed all 5 generated shipments matched 1:1 to
      their real card charges with `day_gap = 0` (2026-07-24).
- [x] Transaction decomposition into splits (Phase 5): new `silver_amazon_splits` model —
      docs/ARCHITECTURE.md's `transaction_splits` concept (a receipt/order decomposes one
      transaction into N splits), keyed off `silver_amazon_order_matches` (only matched shipments
      decompose; an unmatched shipment has no charge to attach to, and the "unsplit transactions
      get an implicit split" union-everything view is a gold-layer concern, not built yet). Amazon's
      per-item subtotal + tax doesn't naturally sum to the charge (shipping/discounts fold in, and
      Amazon rounds each item independently), so each item's split is allocated proportionally to
      its (subtotal + tax) share of the transaction amount, with the last item (by `split_id`, a
      stable tiebreak) absorbing the rounding remainder — this makes splits a true decomposition
      (`sum(amount) = transaction amount` exactly, not just approximately), enforced by new singular
      test `assert_amazon_splits_sum_to_transaction_amount`. New `schema.yml` entry with
      `not_null`/`unique`/`relationships` tests. **Live-verified end-to-end**: `pf transform`
      (138/138 checks green), confirmed every shipment's splits sum to exactly its transaction's
      amount to the cent (e.g. a 3-item shipment's `-18.27 + -37.03 + -20.04 = -75.34`) (2026-07-25).
- [x] Line-item categorization, rules stage (Phase 5): new `silver_split_categories` model —
      the same rules-based cascade stage 1 already used for transactions
      (`silver_transaction_categories`), applied to `silver_amazon_splits.product_name` instead of
      `merchant_name`. New `RuleApplyField.PRODUCT_NAME` ("product_name") — the two `applies_to`
      universes (transaction fields vs. this one split field) are mutually exclusive by name, so
      one `rules` table/config serves both without ambiguity; no per-field `UNION ALL` branching
      needed here (unlike the transaction model) since product_name is the only split-level field
      today. Added an `"Organic Gala Apples, 3 lb Bag"` catalog entry to
      `personal_finance.synth.amazon_orders.CATALOG` and a matching `applies_to: product_name` rule
      to `config/examples/rules.yaml`, to prove out this phase's stated demo goal
      (docs/PLAN.md: "'apples' queryable") concretely rather than just structurally. Embedding-
      similarity / local-LLM / human-review stages for splits, mirroring the transaction cascade's
      stages 2-4, are explicit follow-up work, not required for this phase's demo bar. New
      `schema.yml` entry with `not_null`/`unique`/`relationships` tests. **Live-verified
      end-to-end**: regenerated a fresh 6-month Amazon fixture (confirming the new catalog entry
      surfaces, since seed=42/months=2's fixture only includes 2 apple line items), ingested all
      three sources, ran `pf transform` (148/148 checks green), and queried
      `sum(-amount) from silver_split_categories join silver_amazon_splits ... where category =
      'apples'` → `$32.03` across 3 matched line items — the phase's exact target query works
      (2026-07-25).
- [x] Line-item categorization, embedding/LLM/human-review parity (Phase 5): the remaining three
      cascade stages for splits, mirroring the transaction cascade exactly. New app tables
      `product_embeddings`/`product_llm_categories` (own tables, not merged into
      `merchant_embeddings`/`merchant_llm_categories` — a product name and a merchant name are
      different vocabularies; comparing one's embedding against the other's reference set would be
      a nonsensical nearest-neighbor match) plus `ProductEmbedding`/`ProductLlmCategory` Pydantic
      models. `personal_finance.embed.compute_missing_product_embeddings`,
      `personal_finance.llm_categorize.compute_missing_product_llm_categories`
      (`LlmCategorizeClient.classify` gained a `subject_kind` param so one prompt-builder serves
      both merchants and products). New dbt models `silver_split_categories_embedding/_llm/_human/_all`,
      structurally identical to their transaction counterparts. Generalized
      `personal_finance.review.record_label`/added `fetch_split_review_queue` — `record_label` now
      takes a `subject_kind: EntityKind` (default `TRANSACTION`, so every existing positional call
      site kept working unchanged) instead of hardcoding transaction, resolving the gap this file's
      own backlog flagged after Phase 5 stage 4. `pf enrich`/`pf classify` now process merchants and
      split products in one invocation; `pf review list`/`pf review label` gained `--kind
      transaction|split`. **Live-verified end-to-end** against a real local Ollama
      (`qwen2.5:3b` — the only model pulled locally; `phi3:mini`/`nomic-embed-text` aren't, so the
      embedding stage was verified via the dbt-level test fixture's synthetic vectors instead of a
      real embed call): built a 3-month warehouse (183/183 dbt checks green), `pf review list
      --kind split` showed 12 uncategorized line items, `pf classify --model qwen2.5:3b` classified
      all 12 for real, `dbt build --vars '{"llm_model": "qwen2.5:3b"}'` (see the CLI-polish backlog
      note below — `pf transform` itself doesn't expose this override yet) brought uncategorized
      splits to 0, then `pf review label ... --kind split` overrode one item to `essentials/groceries
      /apples` and confirmed both the `categorization_source` flipped to `human` and the "spend on
      apples" rollup grew from $91.65-equivalent to $112.57 to include it (2026-07-25).
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
