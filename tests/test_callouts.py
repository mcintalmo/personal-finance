"""Tests for the callout engine (personal_finance.callouts).

Like the forecaster, the interesting logic is pure — it takes a monthly
history or a forecast row and decides whether there is anything worth saying —
so most of this file needs no warehouse. The warehouse-backed end
(`detect_callouts` over real marts) is exercised in test_dbt.py against a
built warehouse, where the SQL it depends on actually exists.

The recurring theme in these tests is *restraint*: a callout engine that fires
on every ordinary month is worse than none at all, because the user stops
reading it. Roughly half of what follows asserts that nothing is emitted.
"""

from __future__ import annotations

import logging
from datetime import date

import pytest

from personal_finance.callouts import (
    ANOMALY_LOOKBACK_MONTHS,
    MIN_ANOMALY_AMOUNT,
    CalloutFeed,
    CalloutKind,
    CalloutLevel,
    ForecastRow,
    _anomaly_callouts,
    _budget_risk_callout,
    _robust_scale,
    _trend_callout,
    detect_callouts,
)
from personal_finance.forecast import SeriesHistory, _add_months
from personal_finance.models import BudgetPeriod, ForecastSeriesKind, TrendDirection

TRAINED_THROUGH = date(2026, 6, 1)


def _history(
    values: list[float],
    kind: ForecastSeriesKind = ForecastSeriesKind.TOTAL_OUTFLOW,
    key: str = "total_outflow",
    label: str = "Total spend",
) -> SeriesHistory:
    """A series whose last month is TRAINED_THROUGH, all in the variable half."""
    months = tuple(_add_months(TRAINED_THROUGH, -(len(values) - 1 - i)) for i in range(len(values)))
    return SeriesHistory(
        kind=kind,
        key=key,
        label=label,
        category_id="cat-1" if kind is ForecastSeriesKind.BUDGET_CATEGORY else None,
        months=months,
        committed=tuple(0.0 for _ in values),
        variable=tuple(values),
    )


_BASE_ROW = ForecastRow(
    series_kind="total_outflow",
    series_key="total_outflow",
    series_label="Total spend",
    category_id=None,
    period_start=date(2026, 7, 1),
    predicted_amount=3000.0,
    lower_bound=2500.0,
    upper_bound=3500.0,
    trend="flat",
    budgeted_amount=None,
    budget_period=None,
)


def _forecast_row(**overrides: object) -> ForecastRow:
    """A row shaped like _NEXT_FORECAST_SQL's output, as detect_callouts builds it."""
    return _BASE_ROW._replace(**overrides)


class TestRobustScale:
    def test_uses_mad_when_it_is_nonzero(self) -> None:
        values = [10.0, 12.0, 14.0, 16.0, 18.0]
        # median 14, absolute deviations [4, 2, 0, 2, 4] -> MAD 2
        assert _robust_scale(values, 14.0) == pytest.approx(2.0 / 0.6745)

    def test_falls_back_to_mean_absolute_deviation_when_mad_is_zero(self) -> None:
        """More than half the months identical drives MAD to exactly 0.

        This is the common shape for a category the user rarely touches, not
        an edge case — without the fallback it divides by zero and the whole
        series is skipped, which is precisely the series where a single big
        month matters most.
        """
        values = [0.0, 0.0, 0.0, 0.0, 0.0, 900.0]
        assert _robust_scale(values, 0.0) > 0

    def test_constant_series_has_no_scale(self) -> None:
        assert _robust_scale([100.0] * 6, 100.0) == pytest.approx(0.0)


class TestAnomalyCallouts:
    def test_flags_a_recent_spike(self) -> None:
        history = _history([400.0, 420.0, 380.0, 410.0, 395.0, 2400.0])
        (callout,) = _anomaly_callouts(history, TRAINED_THROUGH)
        assert callout.kind is CalloutKind.SPIKE
        assert callout.level is CalloutLevel.WARNING
        assert callout.period_start == TRAINED_THROUGH
        assert "$2,400" in callout.detail

    def test_says_nothing_about_an_ordinary_series(self) -> None:
        history = _history([400.0, 420.0, 380.0, 410.0, 395.0, 430.0])
        assert _anomaly_callouts(history, TRAINED_THROUGH) == []

    def test_ignores_a_series_shorter_than_the_minimum_history(self) -> None:
        """Three points have no meaningful median, and the third would always
        look extreme relative to the first two."""
        history = _history([100.0, 100.0, 5000.0])
        assert _anomaly_callouts(history, TRAINED_THROUGH) == []

    def test_ignores_a_statistically_large_but_trivial_deviation(self) -> None:
        """A metronomic $2 series makes $20 a huge z-score and a worthless
        callout. The dollar floor is what keeps small categories quiet."""
        history = _history([2.0, 2.0, 2.0, 2.0, 2.0, 2.0 + MIN_ANOMALY_AMOUNT / 2])
        assert _anomaly_callouts(history, TRAINED_THROUGH) == []

    def test_ignores_an_old_anomaly(self) -> None:
        """Real, but months ago and beyond acting on."""
        spike_index = 0
        values = [400.0] * 10
        values[spike_index] = 4000.0
        history = _history(values)
        assert len(values) - spike_index > ANOMALY_LOOKBACK_MONTHS
        assert _anomaly_callouts(history, TRAINED_THROUGH) == []

    def test_constant_series_yields_nothing(self) -> None:
        assert _anomaly_callouts(_history([500.0] * 8), TRAINED_THROUGH) == []

    def test_detects_a_spike_in_a_mostly_zero_category(self) -> None:
        """The MAD fallback's reason for existing, end to end."""
        history = _history([0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1200.0])
        (callout,) = _anomaly_callouts(history, TRAINED_THROUGH)
        assert callout.kind is CalloutKind.SPIKE
        # median is 0, so a "3.2x the usual" phrase would be a divide by zero
        assert "the usual" not in callout.detail

    @pytest.mark.parametrize(
        ("kind", "spike_level", "dip_level"),
        [
            # Spending more is the bad direction; earning more is the good one.
            (ForecastSeriesKind.TOTAL_OUTFLOW, CalloutLevel.WARNING, CalloutLevel.INFO),
            (ForecastSeriesKind.TOTAL_INFLOW, CalloutLevel.INFO, CalloutLevel.WARNING),
        ],
    )
    def test_level_follows_whether_the_change_is_good_news(
        self, kind: ForecastSeriesKind, spike_level: CalloutLevel, dip_level: CalloutLevel
    ) -> None:
        key = kind.value
        base = [1000.0, 1020.0, 980.0, 1010.0, 990.0]
        (spike,) = _anomaly_callouts(_history([*base, 5000.0], kind, key), TRAINED_THROUGH)
        (dip,) = _anomaly_callouts(_history([*base, 10.0], kind, key), TRAINED_THROUGH)
        assert spike.kind is CalloutKind.SPIKE
        assert spike.level is spike_level
        assert dip.kind is CalloutKind.DIP
        assert dip.level is dip_level

    def test_carries_the_series_identity_onto_the_callout(self) -> None:
        history = _history(
            [400.0, 420.0, 380.0, 410.0, 395.0, 2400.0],
            kind=ForecastSeriesKind.BUDGET_CATEGORY,
            key="budget-1",
            label="Dining out",
        )
        (callout,) = _anomaly_callouts(history, TRAINED_THROUGH)
        assert callout.series_key == "budget-1"
        assert callout.series_label == "Dining out"
        assert callout.category_id == "cat-1"


class TestTrendCallout:
    def test_flat_trend_says_nothing(self) -> None:
        assert _trend_callout(_forecast_row(trend="flat"), None) is None

    @pytest.mark.parametrize(
        ("kind", "trend", "expected"),
        [
            ("total_outflow", "rising", CalloutLevel.WARNING),
            ("total_outflow", "falling", CalloutLevel.INFO),
            ("total_inflow", "rising", CalloutLevel.INFO),
            ("total_inflow", "falling", CalloutLevel.WARNING),
        ],
    )
    def test_level_depends_on_the_direction_of_the_money(
        self, kind: str, trend: str, expected: CalloutLevel
    ) -> None:
        callout = _trend_callout(_forecast_row(series_kind=kind, trend=trend), None)
        assert callout is not None
        assert callout.level is expected
        assert callout.kind is CalloutKind.TREND

    def test_compares_the_projection_to_the_history_average(self) -> None:
        history = _history([2000.0] * 6)
        callout = _trend_callout(_forecast_row(trend="rising", predicted_amount=3000.0), history)
        assert callout is not None
        assert "50% above" in callout.detail
        assert callout.rank == pytest.approx(0.5)

    def test_survives_a_series_with_no_usable_baseline(self) -> None:
        """A brand-new budget can forecast before it has any spend to average;
        dividing by that zero would take out the whole feed."""
        callout = _trend_callout(_forecast_row(trend="rising"), _history([0.0] * 6))
        assert callout is not None
        assert "$3,000" in callout.detail

    def test_survives_a_missing_history(self) -> None:
        assert _trend_callout(_forecast_row(trend="rising"), None) is not None


class TestBudgetRiskCallout:
    def _budget_row(self, **overrides: object) -> ForecastRow:
        return _forecast_row(
            **{
                "series_kind": "budget_category",
                "series_key": "budget-1",
                "series_label": "Dining out",
                "category_id": "cat-1",
                "budgeted_amount": 400.0,
                "budget_period": BudgetPeriod.MONTHLY.value,
                **overrides,
            }
        )

    def test_says_nothing_when_the_series_has_no_budget(self) -> None:
        assert _budget_risk_callout(_forecast_row()) is None

    def test_says_nothing_when_the_projection_is_under_budget(self) -> None:
        row = self._budget_row(predicted_amount=350.0, lower_bound=300.0)
        assert _budget_risk_callout(row) is None

    def test_warns_when_the_projection_alone_exceeds_the_budget(self) -> None:
        row = self._budget_row(predicted_amount=500.0, lower_bound=300.0)
        callout = _budget_risk_callout(row)
        assert callout is not None
        assert callout.kind is CalloutKind.BUDGET_RISK
        assert callout.level is CalloutLevel.WARNING
        assert "may not happen" in callout.detail

    def test_escalates_when_even_the_low_end_exceeds_the_budget(self) -> None:
        """The distinction the interval is for: an overrun that survives the
        optimistic end of the forecast is not a modelling artifact."""
        row = self._budget_row(predicted_amount=700.0, lower_bound=600.0)
        callout = _budget_risk_callout(row)
        assert callout is not None
        assert callout.level is CalloutLevel.CRITICAL

    def test_prorates_a_yearly_budget_before_comparing(self) -> None:
        """$6,000/year is $500/month. A monthly projection of $520 is over;
        comparing it to the raw $6,000 would read as comfortably under."""
        over = self._budget_row(
            budgeted_amount=6000.0,
            budget_period=BudgetPeriod.YEARLY.value,
            predicted_amount=520.0,
            lower_bound=480.0,
        )
        callout = _budget_risk_callout(over)
        assert callout is not None
        assert "prorated per month" in callout.detail

        under = over._replace(predicted_amount=480.0)
        assert _budget_risk_callout(under) is None

    def test_prorates_a_weekly_budget_the_other_way(self) -> None:
        """The sub-monthly direction, which an inverted conversion gets wrong
        the opposite way round: $400/week is ~$1,739/month, so a $900 month is
        comfortably inside it — not the false alarm a $92 cap would raise."""
        row = self._budget_row(
            budgeted_amount=400.0,
            budget_period=BudgetPeriod.WEEKLY.value,
            predicted_amount=900.0,
            lower_bound=800.0,
        )
        assert _budget_risk_callout(row) is None

        over = row._replace(predicted_amount=1800.0, lower_bound=1750.0)
        callout = _budget_risk_callout(over)
        assert callout is not None
        assert "weekly budget, prorated per month" in callout.detail

    def test_skips_and_logs_an_unrecognized_budget_period(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Silently treating an unknown period as monthly would publish a
        confidently wrong overrun; silently dropping it would hide a real
        config problem. It is dropped loudly."""
        row = self._budget_row(budget_period="fortnightly", predicted_amount=900.0)
        with caplog.at_level(logging.WARNING, logger="personal_finance.callouts"):
            assert _budget_risk_callout(row) is None
        assert "fortnightly" in caplog.text


class _FakeConn:
    """Stands in for a DuckDB connection returning fixed forecast rows."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, sql: str, params: dict | None = None) -> _FakeConn:
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


class TestDetectCallouts:
    """Ranking and assembly, with load_series stubbed out — the SQL it issues
    is covered against a real warehouse in test_dbt.py."""

    @pytest.fixture
    def histories(self, monkeypatch: pytest.MonkeyPatch) -> list[SeriesHistory]:
        series = [
            _history([400.0, 420.0, 380.0, 410.0, 395.0, 2400.0]),
            _history(
                [3000.0, 3010.0, 2990.0, 3005.0, 2995.0, 3002.0],
                kind=ForecastSeriesKind.TOTAL_INFLOW,
                key="total_inflow",
                label="Total income",
            ),
        ]
        monkeypatch.setattr("personal_finance.callouts.load_series", lambda conn, through: series)
        return series

    def test_reports_no_forecasts_without_flagging_that_as_nothing_to_say(
        self, histories: list[SeriesHistory]
    ) -> None:
        """Anomalies still work with no forecast run; the feed has to admit
        that the trend half was never computed rather than imply all-clear."""
        feed = detect_callouts(_FakeConn([]), today=date(2026, 7, 15))
        assert isinstance(feed, CalloutFeed)
        assert feed.forecasts_available is False
        assert [c.kind for c in feed.callouts] == [CalloutKind.SPIKE]

    @pytest.fixture
    def mixed_feed(self, histories: list[SeriesHistory]) -> CalloutFeed:
        # Column order matches _NEXT_FORECAST_SQL.
        rows = [
            # A budget certain to overrun -> CRITICAL. trend flat, so this row
            # contributes exactly one callout.
            (
                "budget_category",
                "budget-1",
                "Dining out",
                "cat-1",
                date(2026, 7, 1),
                700.0,
                600.0,
                800.0,
                "flat",
                400.0,
                BudgetPeriod.MONTHLY.value,
            ),
            # Spending trending up -> WARNING, well above the ~400 average.
            (
                "total_outflow",
                "total_outflow",
                "Total spend",
                None,
                date(2026, 7, 1),
                1200.0,
                900.0,
                1500.0,
                TrendDirection.RISING.value,
                None,
                None,
            ),
            # Income trending up -> good news, INFO.
            (
                "total_inflow",
                "total_inflow",
                "Total income",
                None,
                date(2026, 7, 1),
                3600.0,
                3400.0,
                3800.0,
                TrendDirection.RISING.value,
                None,
                None,
            ),
        ]
        return detect_callouts(_FakeConn(rows), today=date(2026, 7, 15))

    def test_ranks_critical_above_warning_above_info(self, mixed_feed: CalloutFeed) -> None:
        assert mixed_feed.forecasts_available is True
        levels = [c.level for c in mixed_feed.callouts]
        assert levels == [
            CalloutLevel.CRITICAL,
            CalloutLevel.WARNING,
            CalloutLevel.WARNING,
            CalloutLevel.INFO,
        ]
        assert mixed_feed.callouts[0].kind is CalloutKind.BUDGET_RISK
        assert mixed_feed.callouts[-1].kind is CalloutKind.TREND

    def test_ranks_the_bigger_deviation_first_within_a_level(self, mixed_feed: CalloutFeed) -> None:
        warnings = [c for c in mixed_feed.callouts if c.level is CalloutLevel.WARNING]
        assert [c.rank for c in warnings] == sorted((c.rank for c in warnings), reverse=True)

    def test_limit_keeps_the_most_notable(self, histories: list[SeriesHistory]) -> None:
        """Trimming has to happen after ranking — a limit that dropped the
        critical budget overrun to keep an informational trend would be worse
        than no limit at all."""
        feed = detect_callouts(_FakeConn([]), today=date(2026, 7, 15), limit=1)
        assert [c.kind for c in feed.callouts] == [CalloutKind.SPIKE]
