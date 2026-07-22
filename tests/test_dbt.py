"""End-to-end test of the dbt medallion skeleton.

Seeds a temporary warehouse from the example taxonomy, then runs ``dbt build``
programmatically (models + data tests). Because this runs under pytest, dbt's
tests are wired into CI with no extra workflow step: if a dbt data test fails,
CI fails.
"""

import warnings
from pathlib import Path

import duckdb
import pytest

from personal_finance.ddl import create_schema
from personal_finance.embed import merchant_embedding_id
from personal_finance.ingest import run_ingestion
from personal_finance.seed import seed_categories, seed_rules
from personal_finance.synth import generate_scenario, write_scenario
from personal_finance.user_config import load_user_config

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
    warehouse, bronze, _config, _ = built_warehouse
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
                    f'{{"embedding_model": "{_TEST_EMBEDDING_MODEL}", '
                    f'"embedding_confidence_threshold": {_TEST_CONFIDENCE_THRESHOLD}}}',
                ]
            )
    finally:
        monkeypatch.undo()
    assert result.success, f"dbt build failed: {result.exception}"
    return warehouse


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


class TestSilverTransactionCategoriesAll:
    def test_unions_both_stages_without_duplicates(self, embedding_warehouse):
        with duckdb.connect(str(embedding_warehouse)) as conn:
            stage1 = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories"
            ).fetchone()[0]
            stage2 = conn.execute(
                "select count(*) from main_silver.silver_transaction_categories_embedding"
            ).fetchone()[0]
            combined, distinct = conn.execute(
                "select count(*), count(distinct transaction_id) "
                "from main_silver.silver_transaction_categories_all"
            ).fetchone()
        assert stage2 > 0  # sanity: the synthetic match actually landed
        assert combined == stage1 + stage2
        assert distinct == combined  # no transaction counted by both stages

    def test_starbucks_appears_via_the_combined_view(self, embedding_warehouse):
        with duckdb.connect(str(embedding_warehouse)) as conn:
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
