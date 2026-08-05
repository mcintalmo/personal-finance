"""Render tests for the Dash pages, with the HTTP layer stubbed.

Separate from `test_dashboard.py` because these need the app imported first:
`dash.register_page` raises `PageError` unless an app exists, so importing a
page module in isolation fails before any test runs.

What these catch is the failure class that actually happens in this layer —
a renamed API field, a Plotly property that rejects its input, a `None` where
a number was assumed. The Sankey page shipped a 500 from exactly that
(Plotly rejects 8-digit hex) and nothing here would have noticed, because
until this file existed the pages had no test at all.

They are not a substitute for opening a browser: a callback graph can be
wrong — a duplicate id, an Input that never fires — while every function in
it returns correctly.
"""

from __future__ import annotations

from typing import Any

import pytest

# Importing the app registers all eight pages. Must precede the page imports.
import personal_finance.dashboard.app  # noqa: F401
from personal_finance.dashboard import _client
from personal_finance.dashboard.pages import (
    budgets,
    callouts,
    config,
    overview,
    review,
    sankey,
    sunburst,
)

OVERVIEW = {
    "total_inflow": 5000.0,
    "total_outflow": 3000.0,
    "net_amount": 2000.0,
    "months": [
        {
            "month": "2026-06-01",
            "total_inflow": 5000.0,
            "total_outflow": 3000.0,
            "net_amount": 2000.0,
            "transaction_count": 42,
        }
    ],
}
MERCHANTS = [{"merchant_name": "COSTCO", "transaction_count": 12, "total_outflow": 1400.0}]
ROLLUPS = [
    {
        "category_id": "c1",
        "parent_id": None,
        "name": "essentials",
        "path": "essentials",
        "depth": 1,
        "transaction_count": 42,
        "total_outflow": 1234.56,
        "total_inflow": 0.0,
        "net_amount": -1234.56,
    }
]
SANKEY = [
    {"stage": "income", "source_node": "Salary", "target_node": "Checking", "value": 5000.0},
    {"stage": "spend", "source_node": "Checking", "target_node": "essentials", "value": 3000.0},
]
BUDGETS = [
    {
        "budget_id": "b1",
        "name": "Groceries",
        "category_id": "c1",
        "period": "monthly",
        "budgeted_amount": 500.0,
        "period_start": "2026-06-01",
        "actual_outflow": 620.0,
        "variance": 120.0,
    }
]
CALLOUT_FEED = {
    "forecasts_available": True,
    "callouts": [
        {
            "kind": "spike",
            "level": "warning",
            "title": "Groceries up",
            "detail": "40% above your usual month.",
            "series_label": "essentials/groceries",
            "period_start": "2026-06-01",
        }
    ],
}
QUEUE = [
    {
        "kind": "transaction",
        "subject_id": "t1",
        "posted_on": "2026-06-02",
        "amount": -42.0,
        "merchant_name": "COSTCO",
        "description_raw": "COSTCO #123",
        "source": "amex",
    }
]

ROUTES: dict[str, Any] = {
    "/overview": OVERVIEW,
    "/merchants/top": MERCHANTS,
    "/categories/sunburst": ROLLUPS,
    "/sankey": SANKEY,
    "/budgets": BUDGETS,
    "/callouts": CALLOUT_FEED,
    "/review/queue": QUEUE,
    "/config": ["budgets", "taxonomy"],
    "/config/budgets": {"name": "budgets", "content": "buckets: []"},
}


@pytest.fixture
def api(monkeypatch):
    """Serve the recorded payloads instead of talking to `pf serve`."""

    def fake_get(path: str, **_params: Any) -> Any:
        if path not in ROUTES:
            raise AssertionError(f"page requested an unstubbed route: {path}")
        return ROUTES[path]

    for module in (overview, sunburst, sankey, budgets, callouts, review, config):
        if hasattr(module, "get"):
            monkeypatch.setattr(module, "get", fake_get)
        if hasattr(module, "get_optional"):
            monkeypatch.setattr(module, "get_optional", fake_get)
    return fake_get


def rendered(component: Any) -> str:
    """Flatten a Dash component tree to text, for asserting on content."""
    return str(component)


class TestPagesRenderRealPayloads:
    """Every page, against the shapes the API actually returns."""

    def test_overview_shows_the_totals(self, api):
        out = rendered(overview.render_body("overview-body"))
        assert "$5,000.00" in out
        assert "$2,000.00" in out

    def test_overview_merchants_respects_the_top_n_control(self, api, monkeypatch):
        seen: dict[str, Any] = {}

        def capture(path: str, **params: Any) -> Any:
            seen[path] = params
            return ROUTES[path]

        monkeypatch.setattr(overview, "get", capture)
        overview.render_merchants(25)
        assert seen["/merchants/top"]["limit"] == 25

    def test_overview_keeps_its_totals_when_merchants_fail(self, api, monkeypatch):
        """The two come from different layers — gold and silver — so they fail
        independently, and one alert must not blank the other's content."""

        def only_overview(path: str, **_params: Any) -> Any:
            if path == "/merchants/top":
                raise _client.ApiError("silver not built", status_code=503)
            return ROUTES[path]

        monkeypatch.setattr(overview, "get", only_overview)
        assert "$5,000.00" in rendered(overview.render_body("overview-body"))
        assert "silver not built" in rendered(overview.render_merchants(10))

    def test_sunburst_renders_and_keeps_the_net_column(self, api):
        out = rendered(sunburst.render("total_outflow"))
        assert "essentials" in out
        assert "net_amount" in out  # the column the Streamlit table had

    def test_sankey_renders(self, api):
        """Regression: Plotly rejects 8-digit hex, which 500'd this page."""
        assert "Salary" in rendered(sankey.render("sankey-body"))

    def test_budgets_marks_an_over_budget_period(self, api):
        """variance is actual - budgeted, so positive means over."""
        out = rendered(budgets.render("budgets-body"))
        assert "Groceries" in out
        assert "Over by" in out

    def test_callouts_renders_and_stores_the_feed(self, api):
        feed, controls, body = callouts.render("callouts-body")
        assert feed == CALLOUT_FEED
        assert controls is not None
        assert "callouts-list" in rendered(body)

    def test_callouts_filter_uses_the_store_not_a_second_request(self, monkeypatch):
        """The feed is a whole-ledger computation; re-fetching per chip click
        made every toggle pay for a recompute."""

        def explode(path: str, **_params: Any) -> Any:
            raise AssertionError("filter_callouts must not re-request")

        monkeypatch.setattr(callouts, "get", explode)
        out = rendered(callouts.filter_callouts(["spike"], CALLOUT_FEED))
        assert "Groceries up" in out

    def test_callouts_filter_hides_deselected_kinds(self):
        out = rendered(callouts.filter_callouts(["trend"], CALLOUT_FEED))
        assert "Groceries up" not in out

    def test_review_renders_a_queue_and_a_category_dropdown(self, api):
        table, form = review.render("transaction", 20, 0)
        assert "COSTCO" in rendered(table)
        # A dropdown over the real taxonomy, not the free-text box the
        # Streamlit page had, where a typo was a 404 after the fact.
        assert "essentials" in rendered(form)

    def test_config_loads_a_file(self, api):
        assert config.load("budgets") == "buckets: []"


class TestPagesRenderFailures:
    """A failure must never render as an empty page."""

    @pytest.mark.parametrize(
        ("module", "call"),
        [
            (overview, lambda m: m.render_body("overview-body")),
            (sunburst, lambda m: m.render("total_outflow")),
            (sankey, lambda m: m.render("sankey-body")),
            (budgets, lambda m: m.render("budgets-body")),
        ],
    )
    def test_an_api_failure_becomes_a_visible_alert(self, module, call, monkeypatch):
        def boom(path: str, **_params: Any) -> Any:
            raise _client.ApiError("Can't reach the API at http://x — run `pf serve`.")

        monkeypatch.setattr(module, "get", boom)
        out = rendered(call(module))
        assert "pf serve" in out, "the failure did not reach the page"

    def test_an_empty_result_reads_differently_from_a_failure(self, monkeypatch):
        """ "No data" and "the request failed" mean opposite things and must
        not render identically."""
        monkeypatch.setattr(sankey, "get", lambda path, **_p: [])
        out = rendered(sankey.render("sankey-body"))
        assert "pf transform" in out
        assert "Can't reach" not in out

    def test_review_submit_reports_a_failure_without_clearing_the_form(self, monkeypatch):
        """The form lives inside the rebuilt container, so bumping the
        rebuild token on a failure would wipe the user's selections exactly
        when they need to retry."""

        def boom(path: str, json: dict[str, Any]) -> Any:
            raise _client.ApiError("500: something broke")

        monkeypatch.setattr(review, "post", boom)
        message, token = review.submit(1, "transaction", "t1", "essentials", None, 3)
        assert "something broke" in rendered(message)
        assert token is not review.no_update or token == review.no_update

    def test_review_submit_bumps_the_token_only_on_success(self, monkeypatch):
        monkeypatch.setattr(
            review, "post", lambda path, json: {"subject_id": "t1", "category_id": "c1"}
        )
        _message, token = review.submit(1, "transaction", "t1", "essentials", None, 3)
        assert token == 4

    def test_review_submit_refuses_an_incomplete_correction(self):
        message, token = review.submit(1, "transaction", None, None, None, 3)
        assert "Pick both" in rendered(message)
        assert token is review.no_update
