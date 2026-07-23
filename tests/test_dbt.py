"""End-to-end test of the dbt medallion skeleton.

Seeds a temporary warehouse from the example taxonomy, then runs ``dbt build``
programmatically (models + data tests). Because this runs under pytest, dbt's
tests are wired into CI with no extra workflow step: if a dbt data test fails,
CI fails.
"""

import json
import warnings
from decimal import Decimal
from pathlib import Path

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.embed import merchant_embedding_id
from personal_finance.ingest import run_ingestion
from personal_finance.llm_categorize import merchant_llm_category_id
from personal_finance.seed import seed_categories, seed_merchant_aliases, seed_rules
from personal_finance.synth import generate_scenario, write_scenario
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
        """The alias-resolution refactor of silver_transactions.sql must not
        disturb any other column."""
        with duckdb.connect(str(merchant_alias_warehouse)) as conn:
            rows = conn.execute(
                "select transaction_id, is_transfer, amount from main_silver.silver_transactions"
            ).fetchall()
        assert rows
        for _transaction_id, is_transfer, amount in rows:
            assert is_transfer is False  # no transfer pairs in this tiny fixture
            assert amount < 0


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
