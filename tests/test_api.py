"""Tests for the FastAPI layer (personal_finance.api) over the gold marts."""

import shutil
import warnings

import duckdb
import pytest
from typer.testing import CliRunner

# starlette.testclient warns (as of this fastapi/starlette pairing) that it
# prefers a separate `httpx2` package over `httpx` — a forward-looking notice
# about a package this project has no reason to add; suppressed locally
# rather than in the global (treat-warnings-as-errors) pytest config, since
# it fires at import time, before any per-test filterwarnings marker applies.
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    from fastapi.testclient import TestClient

from personal_finance.cli import app as cli_app
from personal_finance.config import get_settings

runner = CliRunner()

# `pf transform` (dbt-duckdb) emits a DeprecationWarning (codecs.open) from a
# dependency internal, same as test_cli.py's transform-building tests —
# harmless, upstream, and every test here depends on a built warehouse.
pytestmark = pytest.mark.filterwarnings("ignore")


@pytest.fixture(autouse=True)
def fresh_settings(monkeypatch, tmp_path):
    """Point the warehouse, bronze, and user config at temp paths."""
    config_dir = tmp_path / "config"
    shutil.copytree("config/examples", config_dir)
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(tmp_path / "warehouse.duckdb"))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(tmp_path / "bronze"))
    monkeypatch.setenv("CONFIG_DIR", str(config_dir))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def client():
    from personal_finance.api import app

    return TestClient(app)


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_overview_returns_503_before_transform(client):
    response = client.get("/overview")
    assert response.status_code == 503
    assert (
        "pf transform" in response.json()["detail"] or "does not exist" in response.json()["detail"]
    )


def test_merchants_top_returns_503_before_transform(client):
    """Regression test: this endpoint used to read main_silver.silver_merchants
    with no build-gate, raising a raw duckdb.CatalogException (bare 500)
    instead of the same friendly 503 every other endpoint gives."""
    duckdb.connect(str(get_settings().data.warehouse_path)).close()
    response = client.get("/merchants/top")
    assert response.status_code == 503
    assert "pf transform" in response.json()["detail"]


def test_review_queue_returns_503_before_transform(client):
    """Same regression as test_merchants_top_returns_503_before_transform."""
    duckdb.connect(str(get_settings().data.warehouse_path)).close()
    response = client.get("/review/queue")
    assert response.status_code == 503
    assert "pf transform" in response.json()["detail"]


def _build_transformed_warehouse(tmp_path) -> None:
    config_dir = str(tmp_path / "config")
    init = runner.invoke(cli_app, ["init-db", "--config-dir", config_dir])
    assert init.exit_code == 0, init.output
    synth = runner.invoke(cli_app, ["synth", "--out", str(tmp_path / "synth"), "--months", "2"])
    assert synth.exit_code == 0, synth.output
    for file_path, source in (
        (tmp_path / "synth" / "exports" / "chase_checking.csv", None),
        (tmp_path / "synth" / "exports" / "amex.csv", None),
        (tmp_path / "synth" / "amazon" / "Retail.OrderHistory.1.csv", "amazon"),
    ):
        args = ["ingest", str(file_path), "--config-dir", config_dir]
        if source:
            args += ["--source", source]
        ingest = runner.invoke(cli_app, args)
        assert ingest.exit_code == 0, ingest.output
    transform = runner.invoke(cli_app, ["transform"])
    assert transform.exit_code == 0, transform.output


@pytest.fixture(scope="session")
def _prebuilt_warehouse(tmp_path_factory):
    """Build the warehouse ONCE per session, for `built_warehouse` to clone.

    Every API test needs the same read-only warehouse, and building it costs
    ~20s (init-db + synth + three ingests + a full dbt build). Doing that
    per test made this file's fixtures 203 of its 207 seconds, against under
    three seconds of actual assertions.
    """
    root = tmp_path_factory.mktemp("api-warehouse")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setenv("DATA_WAREHOUSE_PATH", str(root / "warehouse.duckdb"))
    monkeypatch.setenv("DATA_BRONZE_PATH", str(root / "bronze"))
    monkeypatch.setenv("CONFIG_DIR", str(root / "config"))
    shutil.copytree("config/examples", root / "config")
    get_settings.cache_clear()
    try:
        _build_transformed_warehouse(root)
        # Fold the write-ahead log into the database file. Closing a DuckDB
        # connection does NOT guarantee this, so the dbt build (in-process,
        # via dbtRunner) leaves every silver view and gold table in the WAL —
        # and a copy of the .duckdb file alone silently arrives with only the
        # app tables, which surfaces as a baffling 503 from every endpoint.
        with duckdb.connect(str(root / "warehouse.duckdb")) as conn:
            conn.execute("CHECKPOINT")
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()
    return root / "warehouse.duckdb"


@pytest.fixture
def built_warehouse(_prebuilt_warehouse, tmp_path):
    """A private copy of the session warehouse, at this test's own path.

    Copied rather than shared so a test that writes (labelling from the review
    queue) cannot change what a later test reads — full isolation for the price
    of a file copy instead of a rebuild.

    The silver models are dbt *views* whose SQL has the session bronze path
    baked in at compile time, so the copy keeps resolving against the session
    fixture's bronze directory. That is why `_prebuilt_warehouse` owns its own
    directory rather than building into a per-test one that would vanish.
    """
    shutil.copy(_prebuilt_warehouse, tmp_path / "warehouse.duckdb")
    get_settings.cache_clear()
    return tmp_path


class TestOverview:
    def test_totals_and_months(self, built_warehouse, client):
        response = client.get("/overview")
        assert response.status_code == 200
        body = response.json()
        assert body["months"], "expected at least one month of activity"
        assert body["net_amount"] == pytest.approx(body["total_inflow"] - body["total_outflow"])


class TestSunburst:
    def test_every_category_present_with_parent_ids(self, built_warehouse, client):
        response = client.get("/categories/sunburst")
        assert response.status_code == 200
        rows = response.json()
        assert len(rows) > 0
        roots = [r for r in rows if r["parent_id"] is None]
        assert len(roots) >= 1
        non_roots = [r for r in rows if r["parent_id"] is not None]
        assert all(r["parent_id"] for r in non_roots)


class TestSankey:
    def test_has_income_and_spend_edges(self, built_warehouse, client):
        response = client.get("/sankey")
        assert response.status_code == 200
        stages = {edge["stage"] for edge in response.json()}
        assert stages == {"income", "spend"}


class TestTopMerchants:
    def test_ordered_descending(self, built_warehouse, client):
        response = client.get("/merchants/top", params={"limit": 5})
        assert response.status_code == 200
        rows = response.json()
        assert rows == sorted(rows, key=lambda r: r["total_outflow"], reverse=True)


class TestBudgets:
    def test_returns_configured_budgets(self, built_warehouse, client):
        response = client.get("/budgets")
        assert response.status_code == 200
        rows = response.json()
        names = {row["name"] for row in rows}
        # config/examples/budgets.yaml defines these three buckets; a budget
        # with zero matching outflow in the fixture window has no row at all
        # (gold_budget_actuals is a sparse time series, not zero-filled).
        assert names, "expected at least one budget with actual activity"
        assert names <= {"Groceries", "Dining out", "Streaming"}
        for row in rows:
            assert row["actual_outflow"] > 0


class TestCallouts:
    def test_returns_503_before_transform(self, client):
        response = client.get("/callouts")
        assert response.status_code == 503
        assert "pf transform" in response.json()["detail"]

    def test_admits_when_no_forecast_has_run(self, built_warehouse, client):
        """The two-month fixture has no forecasts and too little history for an
        anomaly, so the feed is empty — but it must say *why* the trend half is
        missing rather than let the dashboard imply an all-clear."""
        response = client.get("/callouts")
        assert response.status_code == 200
        body = response.json()
        assert body["forecasts_available"] is False
        assert body["callouts"] == []


class TestReviewQueue:
    def test_transaction_queue(self, built_warehouse, client):
        response = client.get("/review/queue", params={"kind": "transaction", "limit": 5})
        assert response.status_code == 200
        for item in response.json():
            assert item["kind"] == "transaction"

    def test_split_queue(self, built_warehouse, client):
        response = client.get("/review/queue", params={"kind": "split", "limit": 5})
        assert response.status_code == 200
        for item in response.json():
            assert item["kind"] == "split"

    def test_null_description_raw_does_not_500(self, monkeypatch, client):
        """silver_transactions.description_raw has no not_null dbt test — a
        transaction lacking one must still serialize, not 500 (a real bug
        caught by live-verifying against `pf serve` + the Streamlit app)."""
        from datetime import date
        from decimal import Decimal

        from personal_finance import api as api_module
        from personal_finance.config import get_settings
        from personal_finance.review import ReviewItem

        duckdb.connect(str(get_settings().data.warehouse_path)).close()
        monkeypatch.setattr(api_module, "_require_silver_built", lambda conn: None)
        item = ReviewItem(
            transaction_id="txn-1",
            posted_on=date(2026, 1, 1),
            amount=Decimal("-1.00"),
            merchant_name=None,
            description_raw=None,
            source="chase_checking",
        )
        monkeypatch.setattr(api_module, "fetch_review_queue", lambda conn, *, limit: [item])
        response = client.get("/review/queue", params={"kind": "transaction", "limit": 5})
        assert response.status_code == 200
        assert response.json()[0]["description_raw"] is None


class TestReviewLabel:
    def test_label_a_split_and_it_leaves_the_queue(self, built_warehouse, client):
        queue = client.get("/review/queue", params={"kind": "split", "limit": 1}).json()
        assert queue, "expected at least one uncategorized split to label"
        subject_id = queue[0]["subject_id"]

        response = client.post(
            "/review/label",
            json={
                "kind": "split",
                "subject_id": subject_id,
                "category_path": "essentials/groceries/apples",
            },
        )
        assert response.status_code == 200
        assert response.json()["subject_id"] == subject_id

    def test_unknown_category_path_returns_404(self, built_warehouse, client):
        queue = client.get("/review/queue", params={"kind": "transaction", "limit": 1}).json()
        assert queue
        response = client.post(
            "/review/label",
            json={
                "kind": "transaction",
                "subject_id": queue[0]["subject_id"],
                "category_path": "not/a/real/path",
            },
        )
        assert response.status_code == 404


class TestConfigEndpoints:
    def test_list_config_files(self, client):
        response = client.get("/config")
        assert response.status_code == 200
        assert set(response.json()) == {
            "sources",
            "taxonomy",
            "rules",
            "budgets",
            "merchant_aliases",
            "known_cities",
        }

    def test_unknown_file_is_404(self, client):
        assert client.get("/config/nonexistent").status_code == 404
        assert (
            client.put(
                "/config/nonexistent", json={"name": "nonexistent", "content": ""}
            ).status_code
            == 404
        )

    def test_round_trip_write(self, client, tmp_path):
        original = client.get("/config/rules").json()["content"]
        new_content = original + "\n"
        response = client.put("/config/rules", json={"name": "rules", "content": new_content})
        assert response.status_code == 200
        assert client.get("/config/rules").json()["content"] == new_content
        assert (tmp_path / "config" / "rules.yaml").read_text(encoding="utf-8") == new_content

    def test_invalid_yaml_returns_400_and_does_not_write(self, client, tmp_path):
        original = (tmp_path / "config" / "rules.yaml").read_text(encoding="utf-8")
        response = client.put("/config/rules", json={"name": "rules", "content": "not: [valid"})
        assert response.status_code == 400
        assert (tmp_path / "config" / "rules.yaml").read_text(encoding="utf-8") == original

    def test_referential_integrity_violation_returns_400(self, client, tmp_path):
        # A rule naming a category the taxonomy doesn't have must fail the
        # cross-file validation write_config_file runs, not just this file's own schema.
        bad_rule = (
            "- pattern: '(?i)test'\n  applies_to: merchant_name\n  category: no/such/category\n"
        )
        response = client.put("/config/rules", json={"name": "rules", "content": bad_rule})
        assert response.status_code == 400
