"""End-to-end test of the dbt medallion skeleton.

Seeds a temporary warehouse from the example taxonomy, then runs ``dbt build``
programmatically (models + data tests). Because this runs under pytest, dbt's
tests are wired into CI with no extra workflow step: if a dbt data test fails,
CI fails.
"""

import json
import warnings
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from personal_finance.callouts import (
    _NEXT_FORECAST_SQL,
    CalloutKind,
    ForecastRow,
    detect_callouts,
)
from personal_finance.ddl import create_schema
from personal_finance.embed import merchant_embedding_id, product_embedding_id
from personal_finance.forecast import compute_forecasts, load_series
from personal_finance.ingest import run_ingestion
from personal_finance.llm_categorize import merchant_llm_category_id, product_llm_category_id
from personal_finance.models import ForecastSeriesKind
from personal_finance.seed import seed_budgets, seed_categories, seed_merchant_aliases, seed_rules
from personal_finance.synth import (
    generate_amazon_orders,
    generate_scenario,
    write_amazon_orders,
    write_scenario,
)
from personal_finance.user_config import (
    MerchantAliasConfig,
    RuleApplyField,
    RuleConfig,
    category_id_for_path,
    load_user_config,
)

REPO_ROOT = Path(__file__).parent.parent
EXAMPLES_CONFIG_DIR = REPO_ROOT / "config" / "examples"

# A realistic three-account mix that also spans the ingestion surface: checking
# (CSV, no external_id), a credit card (CSV, debit/credit columns), and Venmo
# (CSV, external_id). These are the accounts whose synth activity contains the
# correlated transfer pairs (card payment: checking↔credit; cash-out:
# venmo↔checking), so transfer detection is exercised on real fixtures. OFX
# ingestion into bronze is covered separately in test_ingest_ofx.py.
_BRONZE_SOURCES = [
    ("chase_checking", "chase_checking.csv"),
    ("capital_one", "capital_one.csv"),
    ("venmo", "venmo.csv"),
]


@pytest.fixture(scope="module")
def built_warehouse(tmp_path_factory):
    """A seeded warehouse, with a bronze layer ingested, on which `dbt build`
    has run once.

    Env-var handling and warning suppression are scoped to the dbt invocation
    only: a developer's own DATA_WAREHOUSE_PATH is restored afterwards, and
    warnings from this project's code (schema creation, seeding) still fail
    the run under the global ``filterwarnings = error`` regime — only dbt's
    dependency-stack noise is silenced.
    """
    root = tmp_path_factory.mktemp("wh")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, config.rules)
        seed_merchant_aliases(conn, config.merchant_aliases)

    exports = root / "exports"
    write_scenario(generate_scenario(seed=42, months=2), exports)
    sources = {s.name: s for s in config.sources}
    for name, filename in _BRONZE_SOURCES:
        run_ingestion(sources[name], exports / filename, bronze)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps({"known_cities": config.known_cities}),
                ]
            )
    finally:
        monkeypatch.undo()
    return warehouse, bronze, config, result


# Hand-crafted vectors (not real Ollama output) so expected cosine similarities
# are known exactly, independent of any specific embedding model's behavior.
# KROGER is a real stage-1-categorized merchant in this fixture
# (essentials/groceries); STARBUCKS and CHIPOTLE are real stage-1-uncategorized
# merchants — one deliberately a near-duplicate of KROGER (should match), one
# orthogonal (should not).
_TEST_EMBEDDING_MODEL = "test-embedding-model"
_TEST_CONFIDENCE_THRESHOLD = 0.80
_SYNTHETIC_EMBEDDINGS = {
    "KROGER": [1.0, 0.0, 0.0],
    "STARBUCKS": [0.99, 0.01, 0.0],  # cos with KROGER ≈ 0.9999 — clears threshold
    "CHIPOTLE": [0.0, 1.0, 0.0],  # cos with KROGER = 0 — stays unmatched
}


@pytest.fixture(scope="module")
def embedding_warehouse(built_warehouse):
    """``built_warehouse`` plus synthetic ``merchant_embeddings``, with dbt
    re-run (overriding the embedding vars) so the embedding-stage model picks
    them up. Views are idempotently recreated, so rebuilding on top of the
    already-built warehouse is safe.
    """
    warehouse, bronze, config, _ = built_warehouse
    with duckdb.connect(str(warehouse)) as conn:
        for name, vector in _SYNTHETIC_EMBEDDINGS.items():
            conn.execute(
                "INSERT INTO merchant_embeddings (id, created_at, merchant_name, model, embedding) "
                "VALUES ($id, now(), $merchant_name, $model, $embedding)",
                {
                    "id": merchant_embedding_id(name, _TEST_EMBEDDING_MODEL),
                    "merchant_name": name,
                    "model": _TEST_EMBEDDING_MODEL,
                    "embedding": vector,
                },
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


_PARTIAL_MATCH_MERCHANT = "WIDGET SHOP"


@pytest.fixture(scope="module")
def partial_merchant_match_warehouse(tmp_path_factory):
    """A small, self-contained warehouse: one merchant transacting on two
    different accounts (Chase Checking, Capital One Card), plus one extra
    rule targeting account_name rather than merchant_name — so only the
    Capital-One-side transaction is rule-matched, leaving its Chase-side
    sibling (same merchant_name) uncategorized by stage 1.

    Regression fixture for a real bug: an earlier version of
    silver_transaction_categories_embedding excluded a merchant from stage-2
    candidacy entirely if *any* of its transactions were rule-matched, so the
    Chase-side transaction would have been silently stranded. The fix makes
    candidacy transaction-level. Built independently of ``built_warehouse``
    (rather than reusing its 3-source synth scenario) since no merchant there
    naturally spans two accounts.
    """
    root = tmp_path_factory.mktemp("wh_partial_match")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    sources = {s.name: s for s in config.sources}

    exports = root / "exports"
    exports.mkdir()
    chase_csv = exports / "chase_checking.csv"
    chase_csv.write_text(
        f"Posting Date,Amount,Description\n01/15/2026,-25.00,{_PARTIAL_MATCH_MERCHANT}\n"
    )
    capital_one_csv = exports / "capital_one.csv"
    capital_one_csv.write_text(
        f"Posted Date,Debit,Credit,Description\n2026-01-16,30.00,0.00,{_PARTIAL_MATCH_MERCHANT}\n"
    )
    run_ingestion(sources["chase_checking"], chase_csv, bronze)
    run_ingestion(sources["capital_one"], capital_one_csv, bronze)

    rules = [
        *config.rules,
        RuleConfig(
            pattern=r"(?i)^Capital One Card$",
            applies_to=RuleApplyField.ACCOUNT_NAME,
            category="non-essentials/dining",
        ),
    ]
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, rules)
        seed_merchant_aliases(conn, config.merchant_aliases)
        conn.execute(
            "INSERT INTO merchant_embeddings (id, created_at, merchant_name, model, embedding) "
            "VALUES ($id, now(), $merchant_name, $model, $embedding)",
            {
                "id": merchant_embedding_id(_PARTIAL_MATCH_MERCHANT, _TEST_EMBEDDING_MODEL),
                "merchant_name": _PARTIAL_MATCH_MERCHANT,
                "model": _TEST_EMBEDDING_MODEL,
                "embedding": [1.0],
            },
        )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


# CHIPOTLE is the embedding stage's deliberately-unmatched merchant (see
# _SYNTHETIC_EMBEDDINGS above) — the LLM stage picks it up from there. A
# self-reported confidence rather than a real Ollama call, since the dbt-side
# gating logic is what's under test here, not any specific chat model.
_TEST_LLM_MODEL = "test-chat-model"
_TEST_LLM_CONFIDENCE_THRESHOLD = 0.50
_SYNTHETIC_LLM_CATEGORIES = {
    "CHIPOTLE": ("non-essentials/dining", 0.9),
}


@pytest.fixture(scope="module")
def llm_warehouse(embedding_warehouse, built_warehouse):
    """``embedding_warehouse`` plus a synthetic ``merchant_llm_categories`` row,
    with dbt re-run (overriding the LLM vars) so the LLM-stage model picks it
    up.
    """
    warehouse = embedding_warehouse
    _, bronze, config, _ = built_warehouse
    with duckdb.connect(str(warehouse)) as conn:
        for name, (path, confidence) in _SYNTHETIC_LLM_CATEGORIES.items():
            conn.execute(
                "INSERT INTO merchant_llm_categories "
                "(id, created_at, merchant_name, model, category_id, confidence) "
                "VALUES ($id, now(), $merchant_name, $model, $category_id, $confidence)",
                {
                    "id": merchant_llm_category_id(name, _TEST_LLM_MODEL),
                    "merchant_name": name,
                    "model": _TEST_LLM_MODEL,
                    "category_id": category_id_for_path(path),
                    "confidence": confidence,
                },
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_CONFIDENCE_THRESHOLD,
                            "llm_model": _TEST_LLM_MODEL,
                            "llm_confidence_threshold": _TEST_LLM_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


@pytest.fixture(scope="module")
def human_warehouse(llm_warehouse, built_warehouse):
    """``llm_warehouse`` plus three human labels, with dbt re-run so the
    human-review stage picks them up: one overriding an existing stage-1
    (rule) assignment, one filling a gap no stage covered at all, and one
    assigning a transaction to a genuine 3-level-deep category
    (essentials/groceries/apples) so gold_category_ancestors' transitive
    (grandchild -> root) closure and gold_category_rollups' multi-level
    propagation both get exercised with real, non-zero data — not just the
    2-level/zero-activity cases the other fixtures cover.
    """
    warehouse = llm_warehouse
    _, bronze, config, _ = built_warehouse
    with duckdb.connect(str(warehouse)) as conn:
        (overridden_id,) = conn.execute(
            """
            select sc.transaction_id
            from main_silver.silver_transaction_categories sc
            join main_silver.silver_transactions t using (transaction_id)
            where t.merchant_name = 'KROGER'
            limit 1
            """
        ).fetchone()
        (gap_id,) = conn.execute(
            """
            select transaction_id from main_silver.silver_transactions
            where transaction_id not in (
                select transaction_id from main_silver.silver_transaction_categories_all
            )
            limit 1
            """
        ).fetchone()
        (apples_id,) = conn.execute(
            """
            select transaction_id from main_silver.silver_transactions
            where transaction_id not in ($overridden_id, $gap_id) and not is_transfer
            order by transaction_id
            limit 1
            """,
            {"overridden_id": overridden_id, "gap_id": gap_id},
        ).fetchone()
        for transaction_id, path in (
            (overridden_id, "non-essentials/dining"),
            (gap_id, "non-essentials/entertainment/streaming"),
            (apples_id, "essentials/groceries/apples"),
        ):
            conn.execute(
                "INSERT INTO labels (id, created_at, subject_kind, subject_id, category_id) "
                "VALUES (uuid()::text, now(), 'transaction', $subject_id, $category_id)",
                {"subject_id": transaction_id, "category_id": category_id_for_path(path)},
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_CONFIDENCE_THRESHOLD,
                            "llm_model": _TEST_LLM_MODEL,
                            "llm_confidence_threshold": _TEST_LLM_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse, overridden_id, gap_id, apples_id


class TestDbtBuild:
    def test_build_succeeds_including_data_tests(self, built_warehouse):
        _, _, _, result = built_warehouse
        assert result.success, f"dbt build failed: {result.exception}"

    def test_silver_matches_seeded_categories(self, built_warehouse):
        warehouse, _, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute("select count(*) from main_silver.silver_categories").fetchone()
        assert count == len(config.category_paths())

    def test_gold_paths_match_taxonomy_paths(self, built_warehouse):
        warehouse, _, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            paths = {
                path
                for (path,) in conn.execute(
                    "select path from main_gold.gold_category_paths"
                ).fetchall()
            }
        assert paths == config.category_paths()

    def test_gold_depth_consistent_with_path(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute("select path, depth from main_gold.gold_category_paths").fetchall()
        for path, depth in rows:
            assert depth == path.count("/")


class TestGoldCategoryAncestors:
    def test_every_category_is_its_own_ancestor(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (missing_self,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_categories c
                where not exists (
                    select 1 from main_gold.gold_category_ancestors a
                    where a.category_id = c.id and a.ancestor_id = c.id
                )
                """
            ).fetchone()
        assert missing_self == 0

    def test_leaf_ancestors_match_its_path(self, built_warehouse):
        """essentials/groceries's ancestor set (by path) must be exactly
        {essentials, essentials/groceries}."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            paths = {
                path
                for (path,) in conn.execute(
                    """
                    select gc.path
                    from main_gold.gold_category_ancestors a
                    join main_silver.silver_categories leaf on leaf.id = a.category_id
                    join main_gold.gold_category_paths gc on gc.id = a.ancestor_id
                    join main_gold.gold_category_paths leaf_path on leaf_path.id = leaf.id
                    where leaf_path.path = 'essentials/groceries'
                    """
                ).fetchall()
            }
        assert paths == {"essentials", "essentials/groceries"}

    def test_grandchild_ancestors_are_transitive_to_the_root(self, built_warehouse):
        """essentials/groceries/apples is 3 levels deep; its ancestor set must
        include the root (essentials) and the intermediate node
        (essentials/groceries), not just its immediate parent — proving the
        recursive walk doesn't stop after one hop."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            paths = {
                path
                for (path,) in conn.execute(
                    """
                    select gc.path
                    from main_gold.gold_category_ancestors a
                    join main_silver.silver_categories leaf on leaf.id = a.category_id
                    join main_gold.gold_category_paths gc on gc.id = a.ancestor_id
                    join main_gold.gold_category_paths leaf_path on leaf_path.id = leaf.id
                    where leaf_path.path = 'essentials/groceries/apples'
                    """
                ).fetchall()
            }
        assert paths == {"essentials", "essentials/groceries", "essentials/groceries/apples"}

    def test_root_has_only_itself_as_ancestor(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute(
                """
                select count(*)
                from main_gold.gold_category_ancestors a
                join main_gold.gold_category_paths gc on gc.id = a.category_id
                where gc.path = 'essentials'
                """
            ).fetchone()
        assert count == 1


class TestGoldCategoryRollups:
    def _row(self, warehouse: Path, path: str) -> tuple:
        with duckdb.connect(str(warehouse)) as conn:
            return conn.execute(
                "select transaction_count, total_outflow, total_inflow, net_amount "
                "from main_gold.gold_category_rollups where path = $path",
                {"path": path},
            ).fetchone()

    def test_every_taxonomy_category_has_a_row(self, built_warehouse):
        warehouse, _, config, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute(
                "select count(*) from main_gold.gold_category_rollups"
            ).fetchone()
        assert count == len(config.category_paths())

    def test_zero_activity_category_is_present_and_zeroed(self, built_warehouse):
        """No rule ever assigns to essentials/groceries/apples -- it must
        still appear, zeroed out, not be absent."""
        warehouse, _, _, _ = built_warehouse
        row = self._row(warehouse, "essentials/groceries/apples")
        assert row == (0, Decimal("0.00"), Decimal("0.00"), Decimal("0.00"))

    def test_leaf_rollup_matches_directly_assigned_transactions(self, built_warehouse):
        """Reads from silver_transaction_categories_all (every cascade stage),
        the same table gold_category_rollups itself reads from — not just
        stage 1 — so this stays correct if built_warehouse ever grows
        embedding/LLM/human fixtures of its own."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            expected = conn.execute(
                """
                select count(*), -sum(t.amount)
                from main_silver.silver_transaction_categories_all a
                join main_silver.silver_transactions t using (transaction_id)
                join main_gold.gold_category_paths gc on gc.id = a.category_id
                where gc.path = 'essentials/groceries' and not t.is_transfer
                """
            ).fetchone()
        row = self._row(warehouse, "essentials/groceries")
        assert row[0] == expected[0]
        assert row[1] == expected[1]

    def test_parent_rollup_equals_sum_of_children(self, built_warehouse):
        """essentials' totals must equal the sum of its direct children's
        totals (groceries + commute + housing), proving the hierarchy walk."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            children_total = conn.execute(
                """
                select sum(r.transaction_count), sum(r.total_outflow), sum(r.total_inflow)
                from main_gold.gold_category_rollups r
                join main_silver.silver_categories c on c.id = r.category_id
                join main_silver.silver_categories parent on parent.id = c.parent_id
                join main_gold.gold_category_paths parent_path on parent_path.id = parent.id
                where parent_path.path = 'essentials'
                """
            ).fetchone()
        parent_row = self._row(warehouse, "essentials")
        assert parent_row[0] == children_total[0]
        assert parent_row[1] == children_total[1]
        assert parent_row[2] == children_total[2]

    def test_transfers_are_excluded(self, built_warehouse):
        """Total rolled-up transaction_count across every root can't exceed
        categorized, non-transfer transactions -- transfers never count."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (categorized_non_transfer,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_transaction_categories_all a
                join main_silver.silver_transactions t using (transaction_id)
                where not t.is_transfer
                """
            ).fetchone()
            (root_total,) = conn.execute(
                "select sum(transaction_count) from main_gold.gold_category_rollups where depth = 0"
            ).fetchone()
        assert root_total == categorized_non_transfer

    def test_net_amount_is_inflow_minus_outflow(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select total_inflow, total_outflow, net_amount from main_gold.gold_category_rollups"
            ).fetchall()
        for inflow, outflow, net in rows:
            assert net == inflow - outflow


class TestSilverTransactions:
    def _rows(self, warehouse: Path) -> list[tuple]:
        with duckdb.connect(str(warehouse)) as conn:
            return conn.execute(
                "select transaction_id, source, amount, flow, description_raw "
                "from main_silver.silver_transactions"
            ).fetchall()

    def test_unions_every_ingested_source(self, built_warehouse):
        warehouse, bronze, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            silver = conn.execute(
                "select count(*), count(distinct transaction_id), count(distinct source) "
                "from main_silver.silver_transactions"
            ).fetchone()
            (bronze_distinct,) = conn.execute(
                "select count(distinct row_hash) from "
                f"read_parquet('{bronze}/bronze/*/*.parquet', union_by_name = true)"
            ).fetchone()
        count, distinct_ids, distinct_sources = silver
        assert count == bronze_distinct  # one row per unique bronze transaction
        assert distinct_ids == count  # transaction_id is the grain (no dups)
        assert distinct_sources == len(_BRONZE_SOURCES)

    def test_flow_matches_amount_sign(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        for _tid, _source, amount, flow, _desc in self._rows(warehouse):
            expected = "outflow" if amount < 0 else "inflow"
            assert flow == expected

    def test_amounts_normalized_to_two_decimal_places(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (scale,) = conn.execute(
                "select numeric_scale from information_schema.columns "
                "where table_name = 'silver_transactions' and column_name = 'amount'"
            ).fetchone()
        assert scale == 2

    def test_descriptions_present_and_trimmed(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        for _tid, _source, _amount, _flow, desc in self._rows(warehouse):
            assert desc is None or desc == desc.strip()


class TestSilverMerchants:
    def test_merchant_name_is_normalized_key(self, built_warehouse):
        """Every cleaned name is upper-cased, trimmed, and stripped of the
        obvious noise (store/reference numbers)."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            names = [
                name
                for (name,) in conn.execute(
                    "select distinct merchant_name from main_silver.silver_transactions "
                    "where merchant_name is not null"
                ).fetchall()
            ]
        assert names
        for name in names:
            assert name == name.strip() == name.upper()
            assert "#" not in name

    def test_locality_and_store_numbers_stripped_end_to_end(self, built_warehouse):
        """'CHEVRON 0093 BELLEVUE WA' across locations collapses to one merchant."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            merchants = {
                name
                for (name,) in conn.execute(
                    "select merchant_name from main_silver.silver_merchants"
                ).fetchall()
            }
        assert "CHEVRON" in merchants
        assert "TRADER JOE'S" in merchants

    def test_dimension_covers_every_named_transaction(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (named_txns,) = conn.execute(
                "select count(*) from main_silver.silver_transactions "
                "where merchant_name is not null"
            ).fetchone()
            (dim_total, dim_rows) = conn.execute(
                "select sum(transaction_count), count(*) from main_silver.silver_merchants"
            ).fetchone()
        assert dim_total == named_txns  # every named transaction is counted once
        assert dim_rows > 0


class TestConfigDrivenMerchantNormalization:
    """Coverage for the config-driven backlog item: known_cities (places.yaml)
    strips a bare-city suffix the generic macro can't (no state to anchor
    on), and merchant_aliases (merchants.yaml) resolves brand variants
    afterward — both exercised end-to-end via config/examples/, not just
    unit-tested in isolation."""

    def test_known_city_with_no_state_is_stripped(self, built_warehouse):
        """THAI GINGER BELLEVUE has no state suffix (unlike CHEVRON 0093
        BELLEVUE WA, already handled generically) -- config/examples/places.yaml
        lists 'Bellevue', so it must collapse to THAI GINGER."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            names = {
                name
                for (name,) in conn.execute(
                    "select distinct merchant_name from main_silver.silver_transactions"
                ).fetchall()
            }
        assert "THAI GINGER" in names
        assert "THAI GINGER BELLEVUE" not in names

    def test_known_cities_var_is_empty_by_default(self):
        """A config-free build (no places.yaml) must be a no-op here, same as
        every other cascade stage — see dbt_project.yml's known_cities: []."""
        with Path(REPO_ROOT / "transform" / "dbt_project.yml").open(encoding="utf-8") as f:
            assert "known_cities: []" in f.read()


@pytest.fixture(scope="module")
def merchant_alias_warehouse(tmp_path_factory):
    """A small, self-contained warehouse demonstrating merchant_aliases
    (merchants.yaml): two raw descriptors the generic normalize_merchant
    macro leaves genuinely distinct ("FOO BAR ONE", "FOO BAR TWO" -- no
    numbers/domains/store words for it to strip) must collapse to one
    canonical name, and a narrower, higher-priority alias must win over a
    broader one that would also match.
    """
    root = tmp_path_factory.mktemp("wh_merchant_alias")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    sources = {s.name: s for s in config.sources}

    exports = root / "exports"
    exports.mkdir()
    chase_csv = exports / "chase_checking.csv"
    chase_csv.write_text(
        "Posting Date,Amount,Description\n"
        "01/15/2026,-10.00,FOO BAR ONE\n"
        "01/16/2026,-20.00,FOO BAR TWO\n"
        "01/17/2026,-30.00,FOO BAR SPECIAL\n"
    )
    run_ingestion(sources["chase_checking"], chase_csv, bronze)

    aliases = [
        # Narrower/higher-priority: must win for "FOO BAR SPECIAL" over the
        # broader "^FOO BAR" pattern below it.
        MerchantAliasConfig(pattern="(?i)^FOO BAR SPECIAL", canonical_name="FOO BAR SPECIAL CO"),
        MerchantAliasConfig(pattern="(?i)^FOO BAR", canonical_name="FOO BAR INC"),
    ]
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, config.rules)
        seed_merchant_aliases(conn, aliases)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


class TestMerchantAliasResolution:
    def test_distinct_descriptors_collapse_to_the_canonical_name(self, merchant_alias_warehouse):
        with duckdb.connect(str(merchant_alias_warehouse)) as conn:
            names = {
                name
                for (name,) in conn.execute(
                    "select distinct merchant_name from main_silver.silver_transactions "
                    "where merchant_name in ('FOO BAR ONE', 'FOO BAR TWO', 'FOO BAR INC')"
                ).fetchall()
            }
        assert names == {"FOO BAR INC"}

    def test_narrower_higher_priority_alias_wins(self, merchant_alias_warehouse):
        with duckdb.connect(str(merchant_alias_warehouse)) as conn:
            (name,) = conn.execute(
                "select merchant_name from main_silver.silver_transactions "
                "where description_raw = 'FOO BAR SPECIAL'"
            ).fetchone()
        assert name == "FOO BAR SPECIAL CO"

    def test_is_transfer_and_other_columns_unaffected(self, merchant_alias_warehouse):
        """The `base.* exclude (merchant_name)` refactor of silver_transactions.sql
        must not disturb any other passthrough column, including ones with no
        other test/schema.yml coverage (external_id, source_file, ingested_at)."""
        with duckdb.connect(str(merchant_alias_warehouse)) as conn:
            rows = conn.execute(
                "select transaction_id, is_transfer, amount, external_id, source_file, "
                "ingested_at from main_silver.silver_transactions"
            ).fetchall()
        assert rows
        for _transaction_id, is_transfer, amount, external_id, source_file, ingested_at in rows:
            assert is_transfer is False  # no transfer pairs in this tiny fixture
            assert amount < 0
            assert external_id is None  # chase_checking.csv fixture has no FITID column
            assert source_file.endswith("chase_checking.csv")
            assert ingested_at is not None


class TestSilverTransfers:
    def test_detects_the_scenario_transfer_pairs(self, built_warehouse):
        """Two transfer pairs per month (card payment + Venmo cash-out) over the
        two-month scenario ⇒ four transfers, eight flagged legs."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (transfers,) = conn.execute(
                "select count(*) from main_silver.silver_transfers"
            ).fetchone()
            (flagged,) = conn.execute(
                "select count(*) from main_silver.silver_transactions where is_transfer"
            ).fetchone()
            directions = set(
                conn.execute(
                    "select from_account, to_account from main_silver.silver_transfers"
                ).fetchall()
            )
        assert transfers == 4
        assert flagged == 2 * transfers  # both legs of each pair
        assert ("Venmo", "Chase Checking") in directions  # cash-out
        assert ("Chase Checking", "Capital One Card") in directions  # card payment

    def test_pairs_are_well_formed(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select from_account, to_account, amount, day_gap from main_silver.silver_transfers"
            ).fetchall()
        for from_account, to_account, amount, day_gap in rows:
            assert from_account != to_account  # across accounts
            assert amount > 0  # reported as a positive magnitude
            assert 0 <= day_gap <= 3  # within the transfer window

    def test_each_transaction_is_at_most_one_transfer_leg(self, built_warehouse):
        """1:1 matching — no transaction is reused across pairs (as out or in)."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            legs = conn.execute(
                "select outflow_id from main_silver.silver_transfers "
                "union all select inflow_id from main_silver.silver_transfers"
            ).fetchall()
        ids = [leg for (leg,) in legs]
        assert len(ids) == len(set(ids))

    def test_name_match_corroborates_and_sets_confidence(self, built_warehouse):
        """A leg that names the counterparty account raises confidence to high.
        The Venmo cash-out landing in checking names 'VENMO'; the card payment
        has no name overlap in this fixture, so it stays medium."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select from_account, to_account, name_match, confidence "
                "from main_silver.silver_transfers"
            ).fetchall()
        for _from, _to, name_match, confidence in rows:
            assert confidence == ("high" if name_match else "medium")
        tagged = {(f, t, nm, c) for f, t, nm, c in rows}
        assert ("Venmo", "Chase Checking", True, "high") in tagged
        assert ("Chase Checking", "Capital One Card", False, "medium") in tagged

    def test_excluding_transfers_reduces_spend(self, built_warehouse):
        """The card-payment and cash-out legs drop out of a spend measure once
        transfers are excluded."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (with_transfers,) = conn.execute(
                "select -sum(amount) from main_silver.silver_transactions where amount < 0"
            ).fetchone()
            (without_transfers,) = conn.execute(
                "select -sum(amount) from main_silver.silver_transactions "
                "where amount < 0 and not is_transfer"
            ).fetchone()
        assert without_transfers < with_transfers


class TestSilverTransactionCategories:
    def test_matches_expected_categories(self, built_warehouse):
        """Every merchant the example rules.yaml names lands in the right
        category path, matched against merchant_name (the recommended,
        cleaned target)."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                """
                select t.merchant_name, gc.path
                from main_silver.silver_transaction_categories sc
                join main_silver.silver_transactions t using (transaction_id)
                join main_gold.gold_category_paths gc on gc.id = sc.category_id
                """
            ).fetchall()
        by_merchant = dict(rows)
        assert by_merchant["ACME CORP PAYROLL"] == "income/salary"
        assert by_merchant["KROGER"] == "essentials/groceries"
        assert by_merchant["SAFEWAY"] == "essentials/groceries"
        assert by_merchant["ALDI"] == "essentials/groceries"
        assert by_merchant["TRADER JOE'S"] == "essentials/groceries"
        assert by_merchant["SHELL OIL"] == "essentials/commute/gas"
        assert by_merchant["CHEVRON"] == "essentials/commute/gas"
        assert by_merchant["NETFLIX"] == "non-essentials/entertainment/streaming"
        assert by_merchant["SPOTIFY"] == "non-essentials/entertainment/streaming"

    def test_first_match_wins_by_priority(self, built_warehouse):
        """Every categorized row used the lowest-priority (first-declared)
        rule that matched — never a later one."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            bad = conn.execute(
                """
                select sc.transaction_id
                from main_silver.silver_transaction_categories sc
                join main.rules r on r.id = sc.rule_id
                join main.rules better
                    on better.priority < r.priority
                where exists (
                    select 1
                    from main_silver.silver_transactions t
                    where t.transaction_id = sc.transaction_id
                    and (
                        (better.applies_to = 'description_raw'
                         and regexp_matches(t.description_raw, better.pattern))
                        or (better.applies_to = 'merchant_name'
                            and t.merchant_name is not null
                            and regexp_matches(t.merchant_name, better.pattern))
                        or (better.applies_to = 'source'
                            and regexp_matches(t.source, better.pattern))
                        or (better.applies_to = 'account_name'
                            and regexp_matches(t.account_name, better.pattern))
                    )
                )
                """
            ).fetchall()
        assert bad == []

    def test_at_most_one_category_per_transaction(self, built_warehouse):
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            total, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories"
            ).fetchone()
        assert total == distinct

    def test_uncategorized_transactions_absent_not_nulled(self, built_warehouse):
        """A transaction with no matching rule (transfers, misc merchants,
        emoji-containing Venmo notes) is simply absent from this stage — not a
        row with a null category_id."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            total_tx = conn.execute(
                "select count(*) from main_silver.silver_transactions"
            ).fetchone()[0]
            categorized = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories"
            ).fetchone()[0]
        assert 0 < categorized < total_tx

    def test_emoji_containing_transactions_do_not_crash_and_stay_uncategorized(
        self, built_warehouse
    ):
        """Regression test for a real DuckDB 1.5.4 engine crash (SIGSEGV) that
        this model's query shape triggered when a value with a multi-byte
        character (e.g. an emoji in a Venmo note) flowed through regexp_matches
        inside the rule cross join. built_warehouse succeeding at all is most of
        this test; we also confirm the emoji rows land as expected (no rule
        matches them, so they're simply absent from this stage)."""
        warehouse, _, _, _ = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            emoji_merchants = conn.execute(
                r"""
                select merchant_name
                from main_silver.silver_transactions
                where regexp_matches(merchant_name, '[^\x00-\x7F]')
                """
            ).fetchall()
            categorized_ids = {
                row[0]
                for row in conn.execute(
                    "select transaction_id from main_silver.silver_transaction_categories"
                ).fetchall()
            }
            emoji_tx_ids = {
                row[0]
                for row in conn.execute(
                    r"""
                    select transaction_id
                    from main_silver.silver_transactions
                    where regexp_matches(merchant_name, '[^\x00-\x7F]')
                    """
                ).fetchall()
            }
        assert emoji_merchants  # the fixture does contain a multi-byte value
        assert emoji_tx_ids.isdisjoint(categorized_ids)


class TestSilverTransactionCategoriesEmbedding:
    def test_near_duplicate_merchant_is_matched(self, embedding_warehouse):
        with duckdb.connect(str(embedding_warehouse)) as conn:
            rows = conn.execute(
                """
                select e.matched_merchant, e.categorization_confidence
                from main_silver.silver_transaction_categories_embedding e
                join main_silver.silver_transactions t using (transaction_id)
                where t.merchant_name = 'STARBUCKS'
                """
            ).fetchall()
        assert rows, "STARBUCKS should have matched via embedding similarity"
        for matched, confidence in rows:
            assert matched == "KROGER"
            assert confidence > 0.99

    def test_orthogonal_merchant_stays_unmatched(self, embedding_warehouse):
        """CHIPOTLE's embedding is orthogonal to every reference — similarity
        0 is far below the threshold, so it must not appear in this stage."""
        with duckdb.connect(str(embedding_warehouse)) as conn:
            (count,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_transaction_categories_embedding e
                join main_silver.silver_transactions t using (transaction_id)
                where t.merchant_name = 'CHIPOTLE'
                """
            ).fetchone()
        assert count == 0

    def test_matched_merchant_inherits_reference_category(self, embedding_warehouse):
        with duckdb.connect(str(embedding_warehouse)) as conn:
            row = conn.execute(
                """
                select gc.path
                from main_silver.silver_transaction_categories_embedding e
                join main_silver.silver_transactions t using (transaction_id)
                join main_gold.gold_category_paths gc on gc.id = e.category_id
                where t.merchant_name = 'STARBUCKS'
                limit 1
                """
            ).fetchone()
        assert row[0] == "essentials/groceries"  # inherited from KROGER

    def test_grain_has_no_duplicates(self, embedding_warehouse):
        with duckdb.connect(str(embedding_warehouse)) as conn:
            total, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_embedding"
            ).fetchone()
        assert total == distinct

    def test_never_recategorizes_a_stage1_transaction(self, embedding_warehouse):
        """Stage 2 must only cover merchants stage 1 missed entirely."""
        with duckdb.connect(str(embedding_warehouse)) as conn:
            (overlap,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_transaction_categories_embedding e
                where e.transaction_id in (
                    select transaction_id from main_silver.silver_transaction_categories
                )
                """
            ).fetchone()
        assert overlap == 0


class TestSilverTransactionCategoriesEmbeddingPartialMerchantMatch:
    """Regression coverage: a merchant with *some* rule-matched transactions
    (via an account_name rule) must not be excluded wholesale from stage 2 —
    its other, still-uncategorized transactions get their own chance."""

    def test_leftover_transaction_is_still_a_candidate(self, partial_merchant_match_warehouse):
        with duckdb.connect(str(partial_merchant_match_warehouse)) as conn:
            rows = conn.execute(
                """
                select e.categorization_confidence, gc.path
                from main_silver.silver_transaction_categories_embedding e
                join main_silver.silver_transactions t using (transaction_id)
                join main_gold.gold_category_paths gc on gc.id = e.category_id
                where t.merchant_name = $merchant and t.account_name <> 'Capital One Card'
                """,
                {"merchant": _PARTIAL_MATCH_MERCHANT},
            ).fetchall()
        assert rows, (
            f"{_PARTIAL_MATCH_MERCHANT}'s non-Capital-One transaction should still have "
            "matched via embedding similarity (a trivial self-match against its own "
            "Capital-One-rule-assigned category), not been stranded"
        )
        for confidence, path in rows:
            assert path == "non-essentials/dining"
            assert confidence == pytest.approx(1.0)

    def test_rule_matched_transaction_is_excluded_from_stage2(
        self, partial_merchant_match_warehouse
    ):
        """The Capital One transaction is stage 1's, not stage 2's — no double count."""
        with duckdb.connect(str(partial_merchant_match_warehouse)) as conn:
            (count,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_transaction_categories_embedding e
                join main_silver.silver_transactions t using (transaction_id)
                where t.merchant_name = $merchant and t.account_name = 'Capital One Card'
                """,
                {"merchant": _PARTIAL_MATCH_MERCHANT},
            ).fetchone()
        assert count == 0


class TestSilverTransactionCategoriesLlm:
    def test_chipotle_is_classified(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            rows = conn.execute(
                """
                select l.categorization_confidence, gc.path
                from main_silver.silver_transaction_categories_llm l
                join main_silver.silver_transactions t using (transaction_id)
                join main_gold.gold_category_paths gc on gc.id = l.category_id
                where t.merchant_name = 'CHIPOTLE'
                """
            ).fetchall()
        assert rows, "CHIPOTLE should have been classified via the LLM stage"
        for confidence, path in rows:
            assert path == "non-essentials/dining"
            assert confidence == pytest.approx(0.9)

    def test_grain_has_no_duplicates(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            total, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_llm"
            ).fetchone()
        assert total == distinct

    def test_never_recategorizes_a_stage1_or_stage2_transaction(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            (overlap,) = conn.execute(
                """
                select count(*)
                from main_silver.silver_transaction_categories_llm l
                where l.transaction_id in (
                    select transaction_id from main_silver.silver_transaction_categories
                    union
                    select transaction_id from main_silver.silver_transaction_categories_embedding
                )
                """
            ).fetchone()
        assert overlap == 0


class TestSilverTransactionCategoriesAll:
    def test_unions_all_three_stages_without_duplicates(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            stage1 = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories"
            ).fetchone()[0]
            stage2 = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories_embedding"
            ).fetchone()[0]
            stage3 = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories_llm"
            ).fetchone()[0]
            combined, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_all"
            ).fetchone()
        assert stage2 > 0  # sanity: the synthetic matches actually landed
        assert stage3 > 0
        assert combined == stage1 + stage2 + stage3
        assert distinct == combined  # no transaction counted by more than one stage

    def test_starbucks_appears_via_the_combined_view(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            row = conn.execute(
                """
                select a.categorization_source, a.categorization_confidence
                from main_silver.silver_transaction_categories_all a
                join main_silver.silver_transactions t using (transaction_id)
                where t.merchant_name = 'STARBUCKS'
                limit 1
                """
            ).fetchone()
        assert row == ("embedding", row[1])
        assert row[1] > 0.99

    def test_chipotle_appears_via_the_combined_view(self, llm_warehouse):
        with duckdb.connect(str(llm_warehouse)) as conn:
            row = conn.execute(
                """
                select a.categorization_source, a.categorization_confidence
                from main_silver.silver_transaction_categories_all a
                join main_silver.silver_transactions t using (transaction_id)
                where t.merchant_name = 'CHIPOTLE'
                limit 1
                """
            ).fetchone()
        assert row == ("llm", 0.9)


class TestSilverTransactionCategoriesHuman:
    def test_overridden_transaction_gets_the_human_category(self, human_warehouse):
        warehouse, overridden_id, _gap_id, _apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                """
                select gc.path, h.categorization_confidence
                from main_silver.silver_transaction_categories_human h
                join main_gold.gold_category_paths gc on gc.id = h.category_id
                where h.transaction_id = $id
                """,
                {"id": overridden_id},
            ).fetchone()
        assert row == ("non-essentials/dining", 1.0)

    def test_gap_transaction_gets_the_human_category(self, human_warehouse):
        warehouse, _overridden_id, gap_id, _apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                """
                select gc.path
                from main_silver.silver_transaction_categories_human h
                join main_gold.gold_category_paths gc on gc.id = h.category_id
                where h.transaction_id = $id
                """,
                {"id": gap_id},
            ).fetchone()
        assert row == ("non-essentials/entertainment/streaming",)

    def test_grain_has_no_duplicates(self, human_warehouse):
        warehouse, _, _, _ = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            total, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_human"
            ).fetchone()
        assert total == distinct


class TestSilverTransactionCategoriesAllWithHumanOverride:
    def test_overridden_transaction_shows_human_not_rule(self, human_warehouse):
        """KROGER's rule-assigned category loses to the human correction."""
        warehouse, overridden_id, _gap_id, _apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                """
                select categorization_source, gc.path
                from main_silver.silver_transaction_categories_all a
                join main_gold.gold_category_paths gc on gc.id = a.category_id
                where a.transaction_id = $id
                """,
                {"id": overridden_id},
            ).fetchone()
        assert row == ("human", "non-essentials/dining")

    def test_gap_transaction_now_appears(self, human_warehouse):
        warehouse, _overridden_id, gap_id, _apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                """
                select categorization_source, gc.path
                from main_silver.silver_transaction_categories_all a
                join main_gold.gold_category_paths gc on gc.id = a.category_id
                where a.transaction_id = $id
                """,
                {"id": gap_id},
            ).fetchone()
        assert row == ("human", "non-essentials/entertainment/streaming")

    def test_no_transaction_is_double_counted(self, human_warehouse):
        warehouse, _, _, _ = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            combined, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_all"
            ).fetchone()
        assert combined == distinct

    def test_rule_stage_itself_is_unaffected(self, human_warehouse):
        """The human override only changes the combined view — silver_transaction_categories
        (stage 1) still reports its own original assignment."""
        warehouse, overridden_id, _gap_id, _apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (source,) = conn.execute(
                "select categorization_source from main_silver.silver_transaction_categories "
                "where transaction_id = $id",
                {"id": overridden_id},
            ).fetchone()
        assert source == "rule"


class TestGoldCategoryRollupsMultiLevel:
    """Placed last (like the other human_warehouse-dependent classes) so
    requesting human_warehouse here doesn't force its labels into the shared
    warehouse file ahead of earlier tests that expect the pre-human state —
    built_warehouse/embedding_warehouse/llm_warehouse/human_warehouse all
    share one underlying DuckDB file, mutated in place by whichever fixture
    is first requested in test execution order.
    """

    def test_real_two_level_propagation_from_a_depth_2_category(self, human_warehouse):
        """``human_warehouse`` labels one real transaction
        essentials/groceries/apples (depth 2). Its activity must reach both
        essentials/groceries (depth 1) and essentials (depth 0) — not just
        the direct-parent/direct-child relationship TestGoldCategoryRollups
        covers — proving real, non-zero data actually propagates two hops
        up, not just the zero-activity case."""
        warehouse, _overridden_id, _gap_id, apples_id = human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (apples_path,) = conn.execute(
                """
                select gc.path
                from main_silver.silver_transaction_categories_all a
                join main_gold.gold_category_paths gc on gc.id = a.category_id
                where a.transaction_id = $id
                """,
                {"id": apples_id},
            ).fetchone()
            assert apples_path == "essentials/groceries/apples"  # the label actually landed

            for path in ("essentials/groceries/apples", "essentials/groceries", "essentials"):
                expected = conn.execute(
                    """
                    select
                        count(*),
                        coalesce(sum(case when t.flow = 'outflow' then -t.amount else 0 end), 0)
                    from main_silver.silver_transaction_categories_all a
                    join main_silver.silver_transactions t using (transaction_id)
                    join main_gold.gold_category_ancestors anc using (category_id)
                    join main_gold.gold_category_paths ancestor_path on ancestor_path.id = anc.ancestor_id
                    where ancestor_path.path = $path and not t.is_transfer
                    """,
                    {"path": path},
                ).fetchone()
                actual = conn.execute(
                    "select transaction_count, total_outflow from main_gold.gold_category_rollups "
                    "where path = $path",
                    {"path": path},
                ).fetchone()
                assert actual[0] == expected[0], path
                assert actual[1] == expected[1], path


@pytest.fixture(scope="module")
def amazon_warehouse(tmp_path_factory):
    """A self-contained warehouse with both bank and Amazon order-history
    data ingested — proves silver_amazon_shipments aggregates real generated
    order-history rows correctly (the empty-glob-safe path is covered
    separately in TestDbtBuild's default `built_warehouse`, which never
    ingests Amazon data at all).
    """
    root = tmp_path_factory.mktemp("wh_amazon")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    sources = {s.name: s for s in config.sources}

    scenario = generate_scenario(seed=42, months=2)
    exports = root / "exports"
    write_scenario(scenario, exports)
    run_ingestion(sources["chase_checking"], exports / "chase_checking.csv", bronze)
    # Amazon charges post to the credit card (scenario.credit), not checking —
    # ingest it too so silver_amazon_order_matches has real card-charge rows
    # to match against, not just checking transactions.
    run_ingestion(sources["amex"], exports / "amex.csv", bronze)

    orders = generate_amazon_orders(scenario, seed=42)
    amazon_dir = root / "amazon"
    write_amazon_orders(orders, amazon_dir)
    run_ingestion(sources["amazon"], amazon_dir / "Retail.OrderHistory.1.csv", bronze)

    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, config.rules)
        seed_merchant_aliases(conn, config.merchant_aliases)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse, scenario, orders


class TestSilverAmazonShipments:
    def test_one_row_per_shipment(self, amazon_warehouse):
        warehouse, _scenario, orders = amazon_warehouse
        expected_shipments = {(o.website_order_id, o.ship_date) for o in orders}
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute(
                "select count(*) from main_silver.silver_amazon_shipments"
            ).fetchone()
        assert count == len(expected_shipments) > 0

    def test_total_owed_matches_source_transactions(self, amazon_warehouse):
        warehouse, scenario, _orders = amazon_warehouse
        by_id = {t.external_id: -t.amount for t in scenario.credit.transactions}
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select website_order_id, ship_date, total_owed "
                "from main_silver.silver_amazon_shipments"
            ).fetchall()
        assert rows
        # Every shipment's total_owed must equal SOME Amazon charge's amount —
        # the exact mapping back to a transaction_external_id isn't stored in
        # silver (that's the not-yet-built matching stage), so check
        # membership in the set of real charge amounts rather than a 1:1 join.
        charge_amounts = set(by_id.values())
        for _order_id, _ship_date, total_owed in rows:
            assert total_owed in charge_amounts

    def test_item_count_matches_generated_line_items(self, amazon_warehouse):
        warehouse, _scenario, orders = amazon_warehouse
        by_shipment: dict[tuple[str, object], int] = {}
        for order in orders:
            key = (order.website_order_id, order.ship_date)
            by_shipment[key] = by_shipment.get(key, 0) + 1
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select website_order_id, ship_date, item_count "
                "from main_silver.silver_amazon_shipments"
            ).fetchall()
        for order_id, ship_date, item_count in rows:
            assert item_count == by_shipment[order_id, ship_date]


class TestSilverAmazonOrderMatches:
    def test_every_shipment_matches_its_generated_transaction(self, amazon_warehouse):
        # None of the standard credit-card CSV formats carry external_id (only
        # venmo does), so match ground truth via amount + date — the same
        # pair the SQL join keys on — rather than transaction_external_id.
        warehouse, scenario, orders = amazon_warehouse
        expected = {
            txn.external_id: -txn.amount
            for txn in scenario.credit.transactions
            if txn.external_id in {o.transaction_external_id for o in orders}
        }
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select website_order_id, ship_date, total_owed "
                "from main_silver.silver_amazon_order_matches"
            ).fetchall()
        assert rows
        assert len(rows) == len({o.website_order_id for o in orders})
        matched_amounts = {total_owed for _, _, total_owed in rows}
        assert matched_amounts == set(expected.values())

    def test_matching_is_one_to_one(self, amazon_warehouse):
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (transaction_dupes,) = conn.execute(
                "select count(*) from ("
                "  select transaction_id from main_silver.silver_amazon_order_matches"
                "  group by transaction_id having count(*) > 1"
                ")"
            ).fetchone()
            (shipment_dupes,) = conn.execute(
                "select count(*) from ("
                "  select website_order_id, ship_date from main_silver.silver_amazon_order_matches"
                "  group by website_order_id, ship_date having count(*) > 1"
                ")"
            ).fetchone()
        assert transaction_dupes == 0
        assert shipment_dupes == 0

    def test_day_gap_is_zero_for_generated_data(self, amazon_warehouse):
        # The synth generator sets ship_date == the source transaction's
        # posted_on, so every match should be a same-day match.
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select day_gap from main_silver.silver_amazon_order_matches"
            ).fetchall()
        assert rows
        assert all(day_gap == 0 for (day_gap,) in rows)


class TestSilverAmazonSplits:
    def test_one_split_per_line_item(self, amazon_warehouse):
        warehouse, _scenario, orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (count,) = conn.execute(
                "select count(*) from main_silver.silver_amazon_splits"
            ).fetchone()
        assert count == len(orders) > 0

    def test_proportional_allocation_rounds_half_cent_boundary_correctly(self):
        # Regression guard for a real bug found in review: `charge * item /
        # shipment` can land exactly on a half-cent boundary (185.64 * 3.72 /
        # 5.44 = 126.945 exactly), where DOUBLE/DECIMAL division rounds the
        # wrong way due to binary floating-point approximation (it produced
        # 126.94, not the correct 126.95) — even though the model's overall
        # sum-to-transaction-amount invariant still held, since the remainder
        # step absorbs any per-item error into the last item. This runs the
        # model's exact integer-cents formula directly (not the full dbt
        # build) against that adversarial input.
        with duckdb.connect() as conn:
            (rounded_cents,) = conn.execute(
                """
                with cents as (
                    select
                        cast(round(185.64 * 100) as bigint) as charge_cents,
                        cast(round(3.72 * 100) as bigint) as item_cents,
                        cast(round(5.44 * 100) as bigint) as shipment_cents
                )
                select (charge_cents * item_cents + shipment_cents // 2) // shipment_cents
                from cents
                """
            ).fetchone()
        assert rounded_cents == 12695  # $126.95, not $126.94

    def test_splits_sum_to_exact_transaction_amount(self, amazon_warehouse):
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select s.transaction_id, sum(s.amount), any_value(t.amount) "
                "from main_silver.silver_amazon_splits s "
                "join main_silver.silver_transactions t using (transaction_id) "
                "group by s.transaction_id"
            ).fetchall()
        assert rows
        for _transaction_id, split_total, charge_amount in rows:
            assert split_total == charge_amount

    def test_multi_item_shipment_splits_proportionally(self, amazon_warehouse):
        # A shipment with items of different subtotals should not just divide
        # the charge evenly — each split's magnitude should track its item's
        # (subtotal + tax) share, confirming proportional (not flat) allocation.
        warehouse, _scenario, orders = amazon_warehouse
        multi_item_orders: dict[str, list] = {}
        for order in orders:
            multi_item_orders.setdefault(order.website_order_id, []).append(order)
        website_order_id, items = next(
            (oid, items) for oid, items in multi_item_orders.items() if len(items) > 1
        )
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select s.asin, s.amount from main_silver.silver_amazon_splits s "
                "join main_silver.silver_amazon_order_matches m using (transaction_id) "
                "where m.website_order_id = $order_id",
                {"order_id": website_order_id},
            ).fetchall()
        by_asin = dict(rows)
        largest_item = max(
            items, key=lambda o: o.shipment_item_subtotal + o.shipment_item_subtotal_tax
        )
        smallest_item = min(
            items, key=lambda o: o.shipment_item_subtotal + o.shipment_item_subtotal_tax
        )
        assert abs(by_asin[largest_item.asin]) >= abs(by_asin[smallest_item.asin])


class TestSilverSplitCategories:
    def test_apple_line_items_are_categorized(self, amazon_warehouse):
        # The demo goal this whole phase is building toward (docs/PLAN.md):
        # "how much have I spent this year on apples" answerable at the
        # line-item level, not just per-charge.
        warehouse, _scenario, orders = amazon_warehouse
        apple_items = [o for o in orders if "apple" in o.product_name.lower()]
        assert apple_items, "fixture must include an apple line item for this test to mean anything"
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select cat.name, sc.categorization_source, sc.categorization_confidence "
                "from main_silver.silver_split_categories sc "
                "join main_silver.silver_amazon_splits s using (split_id) "
                "join main_silver.silver_categories cat on cat.id = sc.category_id "
                "where s.product_name = 'Organic Gala Apples, 3 lb Bag'"
            ).fetchall()
        assert len(rows) == len(apple_items)
        for name, source, confidence in rows:
            assert name == "apples"
            assert source == "rule"
            assert confidence == pytest.approx(1.0)

    def test_non_matching_product_names_are_uncategorized(self, amazon_warehouse):
        # Nothing in rules.yaml matches "Echo Dot"/"Bounty"/etc. — confirming
        # the "absent = uncategorized" contract, same as the transaction cascade.
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select s.split_id from main_silver.silver_amazon_splits s "
                "where s.product_name not like '%Apple%' "
                "and s.split_id not in (select split_id from main_silver.silver_split_categories)"
            ).fetchall()
            (total_non_apple,) = conn.execute(
                "select count(*) from main_silver.silver_amazon_splits "
                "where product_name not like '%Apple%'"
            ).fetchone()
        assert len(rows) == total_non_apple > 0

    def test_split_category_ids_resolve_to_real_categories(self, amazon_warehouse):
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (orphans,) = conn.execute(
                "select count(*) from main_silver.silver_split_categories sc "
                "left join main_silver.silver_categories cat on cat.id = sc.category_id "
                "where cat.id is null"
            ).fetchone()
        assert orphans == 0


# Hand-crafted vectors (not real Ollama output), same shape as the
# transaction cascade's _SYNTHETIC_EMBEDDINGS: "Organic Gala Apples" is a real
# stage-1-categorized product in the amazon_warehouse fixture (essentials/
# groceries/apples); "Kindle..."/"Ninja..." are real stage-1-uncategorized
# products (nothing in rules.yaml matches them) — one a deliberate
# near-duplicate of the apples vector (should match), one orthogonal (should
# not, staying uncategorized for the LLM stage to pick up).
_TEST_PRODUCT_EMBEDDING_MODEL = "test-embedding-model"
_TEST_PRODUCT_CONFIDENCE_THRESHOLD = 0.80
_SYNTHETIC_PRODUCT_EMBEDDINGS = {
    "Organic Gala Apples, 3 lb Bag": [1.0, 0.0, 0.0],
    "Kindle Paperwhite Fabric Case": [
        0.99,
        0.01,
        0.0,
    ],  # cos with apples ≈ 0.9999 — clears threshold
    "Ninja Foodi Digital Air Fryer": [0.0, 1.0, 0.0],  # cos with apples = 0 — stays unmatched
}


@pytest.fixture(scope="module")
def product_embedding_warehouse(amazon_warehouse):
    """``amazon_warehouse`` plus synthetic ``product_embeddings``, with dbt
    re-run (overriding the embedding vars) so silver_split_categories_embedding
    picks them up.
    """
    warehouse, _scenario, _orders = amazon_warehouse
    bronze = warehouse.parent / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        for name, vector in _SYNTHETIC_PRODUCT_EMBEDDINGS.items():
            conn.execute(
                "INSERT INTO product_embeddings (id, created_at, product_name, model, embedding) "
                "VALUES ($id, now(), $product_name, $model, $embedding)",
                {
                    "id": product_embedding_id(name, _TEST_PRODUCT_EMBEDDING_MODEL),
                    "product_name": name,
                    "model": _TEST_PRODUCT_EMBEDDING_MODEL,
                    "embedding": vector,
                },
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_PRODUCT_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_PRODUCT_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


# "Ninja Foodi Digital Air Fryer" is the embedding stage's deliberately-
# unmatched product — the LLM stage picks it up from there.
_TEST_PRODUCT_LLM_MODEL = "test-chat-model"
_TEST_PRODUCT_LLM_CONFIDENCE_THRESHOLD = 0.50
_SYNTHETIC_PRODUCT_LLM_CATEGORIES = {
    "Ninja Foodi Digital Air Fryer": ("essentials/housing", 0.9),
}


@pytest.fixture(scope="module")
def product_llm_warehouse(product_embedding_warehouse):
    """``product_embedding_warehouse`` plus a synthetic ``product_llm_categories``
    row, with dbt re-run (overriding the LLM vars) so
    silver_split_categories_llm picks it up.
    """
    warehouse = product_embedding_warehouse
    bronze = warehouse.parent / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        for name, (path, confidence) in _SYNTHETIC_PRODUCT_LLM_CATEGORIES.items():
            conn.execute(
                "INSERT INTO product_llm_categories "
                "(id, created_at, product_name, model, category_id, confidence) "
                "VALUES ($id, now(), $product_name, $model, $category_id, $confidence)",
                {
                    "id": product_llm_category_id(name, _TEST_PRODUCT_LLM_MODEL),
                    "product_name": name,
                    "model": _TEST_PRODUCT_LLM_MODEL,
                    "category_id": category_id_for_path(path),
                    "confidence": confidence,
                },
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_PRODUCT_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_PRODUCT_CONFIDENCE_THRESHOLD,
                            "llm_model": _TEST_PRODUCT_LLM_MODEL,
                            "llm_confidence_threshold": _TEST_PRODUCT_LLM_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


@pytest.fixture(scope="module")
def split_human_warehouse(product_llm_warehouse):
    """``product_llm_warehouse`` plus one human label overriding a rule
    assignment and one filling a gap no automated stage covered, with dbt
    re-run so silver_split_categories_human/_all pick them up.
    """
    warehouse = product_llm_warehouse
    bronze = warehouse.parent / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        (overridden_id,) = conn.execute(
            """
            select sc.split_id
            from main_silver.silver_split_categories sc
            join main_silver.silver_amazon_splits s using (split_id)
            where s.product_name = 'Organic Gala Apples, 3 lb Bag'
            limit 1
            """
        ).fetchone()
        (gap_id,) = conn.execute(
            """
            select split_id from main_silver.silver_amazon_splits
            where split_id not in (
                select split_id from main_silver.silver_split_categories_all
            )
            limit 1
            """
        ).fetchone()
        for split_id, path in (
            (overridden_id, "non-essentials/dining"),
            (gap_id, "essentials/housing"),
        ):
            conn.execute(
                "INSERT INTO labels (id, created_at, subject_kind, subject_id, category_id) "
                "VALUES (uuid()::text, now(), 'split', $subject_id, $category_id)",
                {"subject_id": split_id, "category_id": category_id_for_path(path)},
            )

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps(
                        {
                            "embedding_model": _TEST_PRODUCT_EMBEDDING_MODEL,
                            "embedding_confidence_threshold": _TEST_PRODUCT_CONFIDENCE_THRESHOLD,
                            "llm_model": _TEST_PRODUCT_LLM_MODEL,
                            "llm_confidence_threshold": _TEST_PRODUCT_LLM_CONFIDENCE_THRESHOLD,
                            "known_cities": config.known_cities,
                        }
                    ),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse, overridden_id, gap_id


class TestSilverSplitCategoriesEmbedding:
    def test_near_duplicate_product_is_matched(self, product_embedding_warehouse):
        with duckdb.connect(str(product_embedding_warehouse)) as conn:
            row = conn.execute(
                """
                select cat.name, sce.categorization_confidence
                from main_silver.silver_split_categories_embedding sce
                join main_silver.silver_amazon_splits s using (split_id)
                join main_silver.silver_categories cat on cat.id = sce.category_id
                where s.product_name = 'Kindle Paperwhite Fabric Case'
                """
            ).fetchone()
        assert row is not None
        name, confidence = row
        assert name == "apples"
        assert confidence >= _TEST_PRODUCT_CONFIDENCE_THRESHOLD

    def test_orthogonal_product_stays_unmatched(self, product_embedding_warehouse):
        with duckdb.connect(str(product_embedding_warehouse)) as conn:
            row = conn.execute(
                """
                select 1
                from main_silver.silver_split_categories_embedding sce
                join main_silver.silver_amazon_splits s using (split_id)
                where s.product_name = 'Ninja Foodi Digital Air Fryer'
                """
            ).fetchone()
        assert row is None

    def test_rule_categorized_products_are_excluded_from_stage2(self, product_embedding_warehouse):
        # Apples is already rule-matched; it must not also appear here even
        # though it has an embedding (it's the reference vector itself).
        with duckdb.connect(str(product_embedding_warehouse)) as conn:
            row = conn.execute(
                """
                select 1
                from main_silver.silver_split_categories_embedding sce
                join main_silver.silver_amazon_splits s using (split_id)
                where s.product_name = 'Organic Gala Apples, 3 lb Bag'
                """
            ).fetchone()
        assert row is None


class TestSilverSplitCategoriesLlm:
    def test_llm_categorizes_what_embedding_missed(self, product_llm_warehouse):
        with duckdb.connect(str(product_llm_warehouse)) as conn:
            row = conn.execute(
                """
                select cat.name, scl.categorization_confidence
                from main_silver.silver_split_categories_llm scl
                join main_silver.silver_amazon_splits s using (split_id)
                join main_silver.silver_categories cat on cat.id = scl.category_id
                where s.product_name = 'Ninja Foodi Digital Air Fryer'
                """
            ).fetchone()
        assert row is not None
        name, confidence = row
        assert name == "housing"
        assert confidence == pytest.approx(0.9)

    def test_does_not_double_categorize_already_matched_products(self, product_llm_warehouse):
        with duckdb.connect(str(product_llm_warehouse)) as conn:
            row = conn.execute(
                """
                select 1
                from main_silver.silver_split_categories_llm scl
                join main_silver.silver_amazon_splits s using (split_id)
                where s.product_name = 'Kindle Paperwhite Fabric Case'
                """
            ).fetchone()
        assert row is None


class TestSilverSplitCategoriesHuman:
    def test_human_label_appears_in_human_stage(self, split_human_warehouse):
        warehouse, overridden_id, gap_id = split_human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = dict(
                conn.execute(
                    "select split_id, categorization_source "
                    "from main_silver.silver_split_categories_human "
                    "where split_id in ($overridden_id, $gap_id)",
                    {"overridden_id": overridden_id, "gap_id": gap_id},
                ).fetchall()
            )
        assert rows == {overridden_id: "human", gap_id: "human"}


class TestSilverSplitCategoriesAll:
    def test_human_overrides_rule_assignment(self, split_human_warehouse):
        warehouse, overridden_id, _gap_id = split_human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                "select cat.name, sca.categorization_source "
                "from main_silver.silver_split_categories_all sca "
                "join main_silver.silver_categories cat on cat.id = sca.category_id "
                "where sca.split_id = $id",
                {"id": overridden_id},
            ).fetchone()
        assert row == ("dining", "human")

    def test_every_stage_is_represented(self, split_human_warehouse):
        warehouse, _overridden_id, _gap_id = split_human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            sources = {
                row[0]
                for row in conn.execute(
                    "select distinct categorization_source "
                    "from main_silver.silver_split_categories_all"
                ).fetchall()
            }
        assert sources == {"rule", "embedding", "llm", "human"}

    def test_no_duplicate_splits_across_stages(self, split_human_warehouse):
        warehouse, _overridden_id, _gap_id = split_human_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (total,) = conn.execute(
                "select count(*) from main_silver.silver_split_categories_all"
            ).fetchone()
            (distinct,) = conn.execute(
                "select count(distinct split_id) from main_silver.silver_split_categories_all"
            ).fetchone()
        assert total == distinct > 0


class TestGoldLineItems:
    """gold_line_items (Phase 6) is the "implicit split" union: a transaction
    with matched Amazon splits contributes its splits, not itself; every
    other transaction contributes itself as one line item."""

    def test_split_transactions_contribute_splits_not_themselves(self, amazon_warehouse):
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (matched_transactions,) = conn.execute(
                "select count(distinct transaction_id) from main_silver.silver_amazon_splits"
            ).fetchone()
            (split_count,) = conn.execute(
                "select count(*) from main_silver.silver_amazon_splits"
            ).fetchone()
            (line_items_for_matched,) = conn.execute(
                "select count(*) from main_gold.gold_line_items "
                "where transaction_id in (select transaction_id from main_silver.silver_amazon_splits)"
            ).fetchone()
            (transactions_present_directly,) = conn.execute(
                "select count(*) from main_gold.gold_line_items "
                "where line_item_id in (select transaction_id from main_silver.silver_amazon_splits)"
            ).fetchone()
        assert matched_transactions > 0
        # Every matched transaction's line items are its splits (split_count
        # of them across matched_transactions), never the transaction itself.
        assert line_items_for_matched == split_count
        assert transactions_present_directly == 0

    def test_non_amazon_transactions_are_whole_line_items(self, amazon_warehouse):
        warehouse, _scenario, _orders = amazon_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            row = conn.execute(
                "select li.line_item_id, li.amount, t.amount "
                "from main_gold.gold_line_items li "
                "join main_silver.silver_transactions t using (transaction_id) "
                "where li.transaction_id not in (select transaction_id from main_silver.silver_amazon_splits) "
                "limit 1"
            ).fetchone()
        assert row is not None
        _line_item_id, line_item_amount, transaction_amount = row
        assert line_item_amount == transaction_amount

    def test_transfers_are_excluded(self, built_warehouse):
        warehouse, _bronze, _config, _result = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (leaked,) = conn.execute(
                "select count(*) from main_gold.gold_line_items li "
                "join main_silver.silver_transactions t using (transaction_id) "
                "where t.is_transfer"
            ).fetchone()
        assert leaked == 0


class TestGoldMonthlyFlow:
    def test_net_amount_is_inflow_minus_outflow(self, built_warehouse):
        warehouse, _bronze, _config, _result = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select total_inflow, total_outflow, net_amount from main_gold.gold_monthly_flow"
            ).fetchall()
        assert rows
        for inflow, outflow, net in rows:
            assert net == inflow - outflow

    def test_total_transaction_count_matches_non_transfer_transactions(self, built_warehouse):
        warehouse, _bronze, _config, _result = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (expected,) = conn.execute(
                "select count(*) from main_silver.silver_transactions where not is_transfer"
            ).fetchone()
            (total,) = conn.execute(
                "select sum(transaction_count) from main_gold.gold_monthly_flow"
            ).fetchone()
        assert total == expected


class TestGoldSankeyFlow:
    def test_income_edges_cover_every_account_with_inflow(self, built_warehouse):
        warehouse, _bronze, _config, _result = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            income_accounts = {
                row[0]
                for row in conn.execute(
                    "select target_node from main_gold.gold_sankey_flow where stage = 'income'"
                ).fetchall()
            }
            expected_accounts = {
                row[0]
                for row in conn.execute(
                    "select distinct account_name from main_silver.silver_transactions "
                    "where flow = 'inflow' and not is_transfer"
                ).fetchall()
            }
        assert income_accounts == expected_accounts

    def test_spend_edges_target_only_root_categories(self, built_warehouse):
        warehouse, _bronze, _config, _result = built_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            targets = {
                row[0]
                for row in conn.execute(
                    "select distinct target_node from main_gold.gold_sankey_flow where stage = 'spend'"
                ).fetchall()
            }
            roots = {
                row[0]
                for row in conn.execute(
                    "select name from main_gold.gold_category_paths where depth = 0"
                ).fetchall()
            }
        assert targets <= roots


@pytest.fixture(scope="module")
def budget_warehouse(built_warehouse):
    """``built_warehouse`` plus the example config's three budgets, with dbt
    re-run so gold_budget_actuals picks them up."""
    warehouse, bronze, config, _ = built_warehouse
    with duckdb.connect(str(warehouse)) as conn:
        seed_budgets(conn, config.budgets)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps({"known_cities": config.known_cities}),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


class TestGoldBudgetActuals:
    def test_every_row_has_a_positive_budgeted_amount(self, budget_warehouse):
        with duckdb.connect(str(budget_warehouse)) as conn:
            rows = conn.execute(
                "select budgeted_amount from main_gold.gold_budget_actuals"
            ).fetchall()
        assert rows
        assert all(amount > 0 for (amount,) in rows)

    def test_variance_is_actual_minus_budgeted(self, budget_warehouse):
        with duckdb.connect(str(budget_warehouse)) as conn:
            rows = conn.execute(
                "select actual_outflow, budgeted_amount, variance "
                "from main_gold.gold_budget_actuals"
            ).fetchall()
        for actual, budgeted, variance in rows:
            assert variance == actual - budgeted

    def test_actual_outflow_matches_manual_subtree_rollup(self, budget_warehouse):
        with duckdb.connect(str(budget_warehouse)) as conn:
            budget_id, category_id, period_start = conn.execute(
                "select budget_id, category_id, period_start from main_gold.gold_budget_actuals "
                "order by budget_id, period_start limit 1"
            ).fetchone()
            (reported,) = conn.execute(
                "select actual_outflow from main_gold.gold_budget_actuals "
                "where budget_id = $budget_id and period_start = $period_start",
                {"budget_id": budget_id, "period_start": period_start},
            ).fetchone()
            (expected,) = conn.execute(
                """
                select sum(-li.amount)
                from main_gold.gold_line_items li
                join main_gold.gold_category_ancestors anc on anc.category_id = li.category_id
                where anc.ancestor_id = $category_id
                and li.amount < 0
                and date_trunc('month', li.posted_on) = $period_start
                """,
                {"category_id": category_id, "period_start": period_start},
            ).fetchone()
        assert reported == expected


@pytest.fixture(scope="module")
def recurring_warehouse(tmp_path_factory):
    """A seeded warehouse with 6 months of synth activity (enough occurrences
    for gold_recurring_flows' >= 3-occurrence heuristic to fire on the
    fixture's monthly rent/subscription charges and semi-monthly payroll),
    with dbt build run once."""
    root = tmp_path_factory.mktemp("wh")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, config.rules)
        seed_merchant_aliases(conn, config.merchant_aliases)

    exports = root / "exports"
    write_scenario(generate_scenario(seed=42, months=6), exports)
    sources = {s.name: s for s in config.sources}
    for name, filename in _BRONZE_SOURCES:
        run_ingestion(sources[name], exports / filename, bronze)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            result = dbtRunner().invoke(
                [
                    "build",
                    "--project-dir",
                    str(REPO_ROOT / "transform"),
                    "--profiles-dir",
                    str(REPO_ROOT / "transform"),
                    "--vars",
                    json.dumps({"known_cities": config.known_cities}),
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


class TestGoldRecurringFlows:
    def test_detects_known_subscriptions_and_rent(self, recurring_warehouse):
        with duckdb.connect(str(recurring_warehouse)) as conn:
            rows = conn.execute(
                "select merchant_name, amount, cadence, occurrence_count "
                "from main_gold.gold_recurring_flows "
                "where merchant_name in ('NETFLIX', 'SPOTIFY', 'CITYLINE APARTMENTS RENT')"
            ).fetchall()
        found = {name: (amount, cadence, count) for name, amount, cadence, count in rows}
        # normalize_merchant strips "NETFLIX.COM" -> "NETFLIX" and "SPOTIFY USA" -> "SPOTIFY"
        # (domain suffix / trailing state, respectively) — see transform/macros/normalize_merchant.sql.
        assert found["NETFLIX"] == (Decimal("15.49"), "monthly", 6)
        assert found["SPOTIFY"] == (Decimal("11.99"), "monthly", 6)
        assert found["CITYLINE APARTMENTS RENT"] == (Decimal("1800.00"), "monthly", 6)

    def test_detects_recurring_income(self, recurring_warehouse):
        # The synth scenario pays $2,500 on the 1st and the 15th, so gaps
        # alternate 14 and 16-17 days — an average of ~15, which lands in the
        # biweekly bucket. This is the case the biweekly range exists for:
        # without it the paycheck falls between the weekly and monthly ranges
        # and the most predictable flow in the ledger goes undetected.
        with duckdb.connect(str(recurring_warehouse)) as conn:
            row = conn.execute(
                "select flow, amount, cadence, occurrence_count, avg_gap_days "
                "from main_gold.gold_recurring_flows "
                "where merchant_name = 'ACME CORP PAYROLL'"
            ).fetchone()
        assert row is not None, "semi-monthly payroll should be detected as recurring income"
        flow, amount, cadence, count, avg_gap_days = row
        assert flow == "inflow"
        assert amount == Decimal("2500.00")
        assert cadence == "biweekly"
        assert count == 12  # two paydays a month over six months
        assert 12 <= avg_gap_days <= 16

    def test_excludes_random_one_off_spend(self, recurring_warehouse):
        # Groceries/gas/dining/Amazon are random-amount, random-occurrence spend
        # in the synth scenario — none of it should clear the >= 3-occurrences,
        # regular-cadence bar. Only the fixed-amount monthly charges and the
        # semi-monthly paycheck should.
        with duckdb.connect(str(recurring_warehouse)) as conn:
            (merchant_names,) = conn.execute(
                "select list(distinct merchant_name) from main_gold.gold_recurring_flows"
            ).fetchone()
        assert set(merchant_names) == {
            "NETFLIX",
            "SPOTIFY",
            "CITYLINE APARTMENTS RENT",
            "ACME CORP PAYROLL",
        }

    def test_excludes_transfers(self, recurring_warehouse):
        # The card-autopay pair is a fixed-cadence, same-amount movement and
        # would otherwise be the most "recurring" thing in the ledger — but
        # it is money moving between the user's own accounts, not a flow.
        with duckdb.connect(str(recurring_warehouse)) as conn:
            (card_payment_count,) = conn.execute(
                "select count(*) from main_gold.gold_recurring_flows "
                "where merchant_name like '%CREDIT CRD AUTOPAY%'"
            ).fetchone()
        assert card_payment_count == 0

    def test_amount_is_positive_and_cadence_is_regular(self, recurring_warehouse):
        with duckdb.connect(str(recurring_warehouse)) as conn:
            rows = conn.execute(
                "select flow, amount, avg_gap_days, gap_days_stddev "
                "from main_gold.gold_recurring_flows"
            ).fetchall()
        assert rows
        for flow, amount, avg_gap_days, gap_days_stddev in rows:
            # Published as a magnitude regardless of direction — `flow` carries
            # the sign, so a consumer never has to guess at the convention.
            assert amount > 0
            assert flow in {"inflow", "outflow"}
            assert gap_days_stddev <= avg_gap_days * 0.25


@pytest.fixture(scope="module")
def forecast_warehouse(tmp_path_factory):
    """A warehouse with 18 months of history entirely in the past, budgets
    seeded, and `pf forecast` run — so gold_forecasts is populated.

    The scenario deliberately starts 2025-01 and runs 18 months (ending
    2026-06) rather than using the synth default: forecasting only consumes
    *complete* months strictly before ``today``, so a fixture whose activity
    ran into the future would leave almost nothing to fit.
    """
    root = tmp_path_factory.mktemp("wh")
    warehouse = root / "warehouse.duckdb"
    bronze = root / "bronze"
    config = load_user_config(EXAMPLES_CONFIG_DIR)
    with duckdb.connect(str(warehouse)) as conn:
        create_schema(conn)
        seed_categories(conn, config.taxonomy)
        seed_rules(conn, config.rules)
        seed_merchant_aliases(conn, config.merchant_aliases)
        seed_budgets(conn, config.budgets)

    exports = root / "exports"
    write_scenario(generate_scenario(seed=42, start=date(2025, 1, 1), months=18), exports)
    sources = {s.name: s for s in config.sources}
    # Only the three canonical accounts: the other synth export files are
    # alternate FORMATS of these same accounts, so ingesting them too would
    # post every charge several times over under different account names.
    for name, filename in _BRONZE_SOURCES:
        run_ingestion(sources[name], exports / filename, bronze)

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(warehouse))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(bronze))
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from dbt.cli.main import dbtRunner

            args = [
                "build",
                "--project-dir",
                str(REPO_ROOT / "transform"),
                "--profiles-dir",
                str(REPO_ROOT / "transform"),
                "--vars",
                json.dumps({"known_cities": config.known_cities}),
            ]
            assert dbtRunner().invoke(args).success  # silver/gold, incl. recurring flows
            with duckdb.connect(str(warehouse)) as conn:
                # Fixed `today` keeps trained_through pinned to 2026-06 instead
                # of drifting with the wall clock.
                written = compute_forecasts(conn, horizon=3, today=date(2026, 7, 15))
            result = dbtRunner().invoke(args)  # republish gold_forecasts
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse, written


class TestGoldForecasts:
    def test_forecast_rows_were_written(self, forecast_warehouse):
        _, written = forecast_warehouse
        assert written > 0

    def test_covers_totals_and_every_budget(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            kinds = dict(
                conn.execute(
                    "select series_kind, count(distinct series_key) "
                    "from main_gold.gold_forecasts group by series_kind"
                ).fetchall()
            )
        assert kinds["total_inflow"] == 1
        assert kinds["total_outflow"] == 1
        assert kinds["budget_category"] == 3  # config/examples/budgets.yaml

    def test_components_sum_to_the_prediction(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (bad,) = conn.execute(
                "select count(*) from main_gold.gold_forecasts "
                "where predicted_amount != committed_amount + variable_amount"
            ).fetchone()
        assert bad == 0

    def test_recurring_charges_land_in_the_committed_component(self, forecast_warehouse):
        """Rent + both subscriptions are detected as recurring, so total spend
        must carry them as committed rather than leaving them to the model."""
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            (committed,) = conn.execute(
                "select distinct committed_amount from main_gold.gold_forecasts "
                "where series_key = 'total_outflow'"
            ).fetchone()
        # CITYLINE RENT 1800.00 + NETFLIX 15.49 + SPOTIFY 11.99
        assert committed == Decimal("1827.48")

    def test_recurring_income_lands_in_the_committed_component(self, forecast_warehouse):
        """The inflow half of the same property. The synth scenario pays
        $2,500 on the 1st and the 15th, so once recurring detection covers
        inflows, income is almost entirely committed — and the model is left
        predicting only what varies instead of re-deriving a known salary.
        """
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select committed_amount from main_gold.gold_forecasts "
                "where series_key = 'total_inflow' order by horizon"
            ).fetchall()
        assert rows
        # Two $2,500 paydays projected into each forecast month.
        assert [committed for (committed,) in rows] == [Decimal("5000.00")] * len(rows)

    def test_recurring_income_is_removed_from_the_variable_series(self, forecast_warehouse):
        """The other half of the decomposition, and the one nothing guarded.

        `test_recurring_income_lands_in_the_committed_component` reads
        committed_amount, which comes from projecting the recurring groups —
        entirely independent of the SQL that splits the *history* into its
        committed and variable halves. If that split regressed to matching
        outflows only, the salary would be projected as committed AND left in
        the variable series for the model to re-fit, and income would forecast
        at roughly double. Every other test in this class still passes then,
        including the components-sum invariant, which holds fine at twice the
        right value.
        """
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select variable_amount, predicted_amount from main_gold.gold_forecasts "
                "where series_key = 'total_inflow'"
            ).fetchall()
            (typical,) = conn.execute(
                "select median(total_inflow) from main_gold.gold_monthly_flow "
                "where month < date_trunc('month', date '2026-07-15')"
            ).fetchone()
        assert rows
        for variable, predicted in rows:
            # Payroll is $5,000/month of the ~$5,100 that actually arrives, so
            # only the small non-salary tail is left to model.
            assert variable < Decimal("1000.00"), "salary was left in the variable series"
            assert abs(predicted - typical) < Decimal("1000.00"), (
                f"predicted {predicted} is implausible against a typical {typical}"
            )

    def test_fully_recurring_category_gets_a_zero_width_interval(self, forecast_warehouse):
        """The property the decomposition exists for: the Streaming budget is
        100% subscriptions, so there is nothing uncertain left to widen it."""
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select committed_amount, variable_amount, lower_bound, upper_bound "
                "from main_gold.gold_forecasts where series_label = 'Streaming'"
            ).fetchall()
        assert rows
        for committed, variable, lower, upper in rows:
            assert committed == Decimal("27.48")  # NETFLIX + SPOTIFY
            assert variable == Decimal("0.00")
            assert lower == upper  # deterministic: no uncertainty to express

    def test_horizons_are_consecutive_future_months(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select horizon, period_start, trained_through from main_gold.gold_forecasts "
                "where series_key = 'total_outflow' order by horizon"
            ).fetchall()
        assert [r[0] for r in rows] == [1, 2, 3]
        assert [r[1] for r in rows] == [date(2026, 7, 1), date(2026, 8, 1), date(2026, 9, 1)]
        # the partial month (2026-07) is excluded from training
        assert all(r[2] == date(2026, 6, 1) for r in rows)

    def test_category_path_is_joined_for_budget_series(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = conn.execute(
                "select category_path from main_gold.gold_forecasts "
                "where series_kind = 'budget_category'"
            ).fetchall()
        assert rows
        assert all(path for (path,) in rows)

    def test_recompute_replaces_rather_than_appends(self, forecast_warehouse):
        """Forecasts are a full recompute: re-running must not duplicate rows."""
        warehouse, written = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            again = compute_forecasts(conn, horizon=3, today=date(2026, 7, 15))
            (total,) = conn.execute("select count(*) from forecasts").fetchone()
        assert again == written
        assert total == written


class TestCalloutsOverRealMarts:
    """`detect_callouts` reuses `forecast.load_series`, so it depends on the
    same gold SQL. The statistical decisions are unit-tested in
    test_callouts.py; what this class proves is that the queries run against a
    real warehouse and that the feed refers to series that actually exist.
    """

    def test_produces_a_feed_from_the_marts(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            feed = detect_callouts(conn, today=date(2026, 7, 15))
        assert feed.forecasts_available is True
        # Non-empty is the load-bearing part: without it every per-callout
        # assertion below iterates an empty list and passes vacuously, which
        # would keep the whole feed green if it silently produced nothing.
        assert feed.callouts, "18 months of synth activity should yield something to say"
        for callout in feed.callouts:
            assert callout.title
            assert callout.detail
            assert callout.series_key

    def test_series_totals_feed_the_anomaly_detector_at_the_right_grain(self, forecast_warehouse):
        """Anomalies key off `SeriesHistory.totals`, and every unit test for
        them builds that history by hand. This is the only check that the real
        `load_series` produces it at the right grain and sign — if it returned
        net amounts, half-months, or negated outflows, the hand-built unit
        tests would all still pass while every real callout was nonsense.
        """
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            histories = {h.key: h for h in load_series(conn, date(2026, 6, 1))}
            mart = dict(
                conn.execute(
                    "select month, total_outflow from main_gold.gold_monthly_flow "
                    "where month <= date '2026-06-01' order by month"
                ).fetchall()
            )
        spend = histories["total_outflow"]
        assert spend.months[-1] == date(2026, 6, 1)  # the partial month is excluded
        for month, total in zip(spend.months, spend.totals, strict=True):
            assert total == pytest.approx(float(mart[datetime(month.year, month.month, 1)]))

    def test_budgets_are_joined_onto_their_forecast_rows(self, forecast_warehouse):
        """The LEFT JOIN in _NEXT_FORECAST_SQL is the only thing that makes a
        BUDGET_RISK callout reachable at all. If it matched nothing, that whole
        kind would be dead code — and if its `series_kind` predicate migrated
        from the ON clause to a WHERE, the join would quietly become an INNER
        one and drop both total series from the feed. Neither shows up as a
        failure anywhere else.
        """
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            rows = [
                ForecastRow(*r)
                for r in conn.execute(
                    _NEXT_FORECAST_SQL, {"current_month": date(2026, 7, 1)}
                ).fetchall()
            ]
        budget_rows = [r for r in rows if r.series_kind == "budget_category"]
        assert budget_rows
        assert all(r.budgeted_amount is not None for r in budget_rows)
        assert all(r.budget_period is not None for r in budget_rows)
        # The totals must survive the LEFT JOIN carrying NULL budgets.
        assert {"total_inflow", "total_outflow"} <= {r.series_key for r in rows}
        assert all(r.budgeted_amount is None for r in rows if r.series_kind.startswith("total_"))

    def test_every_callout_names_a_real_series(self, forecast_warehouse):
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            feed = detect_callouts(conn, today=date(2026, 7, 15))
            known = {
                key
                for (key,) in conn.execute(
                    "select distinct series_key from main_gold.gold_forecasts"
                ).fetchall()
            }
        assert {c.series_key for c in feed.callouts} <= known

    def test_budget_risk_callouts_only_target_budgeted_series(self, forecast_warehouse):
        """A budget-risk callout on a total would be comparing a whole month's
        spend to one category's cap."""
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            feed = detect_callouts(conn, today=date(2026, 7, 15))
        for callout in feed.callouts:
            if callout.kind is CalloutKind.BUDGET_RISK:
                assert callout.series_kind is ForecastSeriesKind.BUDGET_CATEGORY

    def test_a_forecast_whose_months_have_all_passed_is_not_used(self, forecast_warehouse):
        """`pf forecast` is run by hand, so its rows can be months old.

        The fixture forecasts 2026-07 through 2026-09. Viewed from December,
        every one of those months has ended. Selecting `horizon = 1` blindly
        would nudge the user about July — a month they can no longer change,
        presented as though it were current. The rows drop out instead and the
        feed reports that no usable forecast exists, which sends the user to
        re-run `pf forecast` rather than acting on stale numbers.
        """
        warehouse, _ = forecast_warehouse
        with duckdb.connect(str(warehouse)) as conn:
            current = detect_callouts(conn, today=date(2026, 7, 15))
            stale = detect_callouts(conn, today=date(2026, 12, 15))
        assert current.forecasts_available is True
        assert stale.forecasts_available is False
        assert all(
            c.kind not in {CalloutKind.TREND, CalloutKind.BUDGET_RISK} for c in stale.callouts
        )

    def test_reports_no_forecasts_when_none_have_been_computed(self, recurring_warehouse):
        """The other fixture never runs `pf forecast`, so the trend half of
        the feed is unavailable and has to say so.

        Note this fixture yields no callouts at all (6 months of unremarkable
        synth activity), so the kind assertion below is deliberately weak —
        `forecasts_available` is the claim being tested. The anomaly path
        itself is covered by `test_series_totals_feed_the_anomaly_detector_at_
        the_right_grain` plus the unit tests in test_callouts.py.
        """
        with duckdb.connect(str(recurring_warehouse)) as conn:
            feed = detect_callouts(conn, today=date(2026, 7, 15))
        assert feed.forecasts_available is False
        assert all(c.kind in {CalloutKind.SPIKE, CalloutKind.DIP} for c in feed.callouts)
