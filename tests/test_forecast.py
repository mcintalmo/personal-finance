"""Tests for the forecasting engine (personal_finance.forecast).

The statistical core is pure — it takes lists of floats and returns forecasts
— so most of this file needs no warehouse. The warehouse-backed end of the
module (load_series/compute_forecasts) is exercised in test_dbt.py against a
real built warehouse, where the SQL it depends on actually exists.
"""

from datetime import date
from decimal import Decimal

import pytest

from personal_finance.forecast import (
    DEFAULT_INTERVAL_LEVEL,
    MIN_HISTORY_MONTHS,
    SeriesHistory,
    _add_months,
    _candidate_models,
    _conformal_half_widths,
    _last_complete_month,
    _mean_absolute_naive_error,
    _project_committed,
    _safe_forecast,
    fit_variable,
    forecast_series,
    trend_direction,
)
from personal_finance.models import ForecastSeriesKind, TrendDirection


def _history(
    committed: list[float],
    variable: list[float],
    kind: ForecastSeriesKind = ForecastSeriesKind.TOTAL_OUTFLOW,
) -> SeriesHistory:
    months = tuple(_add_months(date(2026, 1, 1), i) for i in range(len(variable)))
    return SeriesHistory(
        kind=kind,
        key="total_outflow",
        label="Total spend",
        category_id=None,
        months=months,
        committed=tuple(committed),
        variable=tuple(variable),
    )


class TestAddMonths:
    @pytest.mark.parametrize(
        ("start", "delta", "expected"),
        [
            (date(2026, 1, 1), 1, date(2026, 2, 1)),
            (date(2026, 12, 1), 1, date(2027, 1, 1)),
            (date(2026, 1, 1), -1, date(2025, 12, 1)),
            (date(2026, 3, 1), 12, date(2027, 3, 1)),
            (date(2026, 6, 1), 0, date(2026, 6, 1)),
        ],
    )
    def test_crosses_year_boundaries(self, start: date, delta: int, expected: date) -> None:
        assert _add_months(start, delta) == expected


class TestLastCompleteMonth:
    """The partial-month guard — training on an incomplete month makes every
    model forecast a spurious decline."""

    @pytest.mark.parametrize(
        ("today", "expected"),
        [
            (date(2026, 7, 25), date(2026, 6, 1)),
            (date(2026, 7, 1), date(2026, 6, 1)),  # 1st: this month has barely started
            (date(2026, 7, 31), date(2026, 6, 1)),  # last day: still not complete
            (date(2026, 1, 15), date(2025, 12, 1)),  # crosses the year boundary
        ],
    )
    def test_excludes_the_current_month(self, today: date, expected: date) -> None:
        assert _last_complete_month(today) == expected


class TestCandidateModels:
    @pytest.mark.parametrize(
        ("n", "expected"),
        [
            (1, ["naive"]),
            (2, ["naive", "mean3"]),
            (5, ["naive", "mean3", "theta"]),
            (8, ["naive", "mean3", "theta", "ets"]),
            (24, ["naive", "mean3", "theta", "ets"]),
        ],
    )
    def test_panel_is_gated_by_history_length(self, n: int, expected: list[str]) -> None:
        assert [m.name for m in _candidate_models(n)] == expected


class TestSafeForecast:
    def test_returns_none_when_the_model_raises(self) -> None:
        def explode(values: list[float], horizon: int) -> list[float]:
            raise RuntimeError("no fit")

        assert _safe_forecast(explode, [1.0, 2.0, 3.0], 3) is None

    def test_returns_none_on_non_finite_output(self) -> None:
        def nan_out(values: list[float], horizon: int) -> list[float]:
            return [float("nan")] * horizon

        assert _safe_forecast(nan_out, [1.0, 2.0, 3.0], 3) is None

    def test_passes_through_a_good_fit(self) -> None:
        assert _safe_forecast(lambda v, h: [1.0] * h, [1.0, 2.0], 2) == [1.0, 2.0 - 1.0]


class TestMeanAbsoluteNaiveError:
    def test_zero_for_a_constant_series(self) -> None:
        assert _mean_absolute_naive_error([5.0] * 6) == pytest.approx(0.0)

    def test_is_the_mean_step_size(self) -> None:
        # steps of 10, 10, 10 -> 10
        assert _mean_absolute_naive_error([0.0, 10.0, 20.0, 30.0]) == pytest.approx(10.0)

    def test_single_point_has_no_step(self) -> None:
        assert _mean_absolute_naive_error([42.0]) == pytest.approx(0.0)


class TestFitVariable:
    def test_rejects_an_empty_series(self) -> None:
        with pytest.raises(ValueError, match="empty series"):
            fit_variable([])

    def test_extrapolates_a_clean_linear_trend(self) -> None:
        values = [1000.0 + 80 * i for i in range(8)]
        fit = fit_variable(values, horizon=3)
        # continues +80/month rather than flattening out
        assert fit.point[0] == pytest.approx(1640, abs=30)
        assert fit.point[2] == pytest.approx(1800, abs=30)

    def test_constant_series_gets_a_zero_width_interval(self) -> None:
        fit = fit_variable([500.0] * 8, horizon=3)
        assert fit.point == pytest.approx((500.0, 500.0, 500.0))
        assert fit.half_width == (0.0, 0.0, 0.0)

    def test_noisy_series_beats_naive(self) -> None:
        values = [1200.0, 900.0, 1400.0, 1100.0, 1250.0, 1050.0]
        fit = fit_variable(values, horizon=3)
        assert fit.mase is not None
        assert fit.mase < 1.0  # MASE < 1 means it beat a naive forecast

    def test_interval_widens_with_horizon(self) -> None:
        values = [1200.0, 900.0, 1400.0, 1100.0, 1250.0, 1050.0]
        fit = fit_variable(values, horizon=3)
        assert fit.half_width[0] < fit.half_width[1] < fit.half_width[2]

    def test_all_zero_series_does_not_crash(self) -> None:
        """A budgeted category with no spend at all is a real case."""
        fit = fit_variable([0.0] * 8, horizon=3)
        assert fit.point == pytest.approx((0.0, 0.0, 0.0))


class TestConformalHalfWidths:
    def test_no_errors_gives_no_width(self) -> None:
        assert _conformal_half_widths([], 3, 80) == (0.0, 0.0, 0.0)

    def test_widens_as_sqrt_of_horizon(self) -> None:
        widths = _conformal_half_widths([10.0] * 5, 4, 80)
        assert widths[0] == pytest.approx(10.0)
        assert widths[3] == pytest.approx(20.0)  # 10 * sqrt(4)

    def test_higher_coverage_is_never_narrower(self) -> None:
        errors = [1.0, 5.0, 9.0, 20.0, 50.0]
        assert _conformal_half_widths(errors, 1, 95)[0] >= _conformal_half_widths(errors, 1, 50)[0]


class TestTrendDirection:
    @pytest.mark.parametrize(
        ("values", "expected"),
        [
            ([100.0 + 20 * i for i in range(8)], TrendDirection.RISING),
            ([500.0 - 40 * i for i in range(8)], TrendDirection.FALLING),
            ([500.0] * 8, TrendDirection.FLAT),
            ([1.0, 2.0], TrendDirection.FLAT),  # too short to call
            ([0.0] * 8, TrendDirection.FLAT),  # all-zero: no scale to compare against
        ],
    )
    def test_classifies_direction(self, values: list[float], expected: TrendDirection) -> None:
        assert trend_direction(values) == expected

    def test_one_expensive_month_is_not_a_trend(self) -> None:
        """The distinction the user actually cares about: a level shift or a
        single spike must not read as 'climbing month over month'."""
        spike = [500.0, 500.0, 500.0, 2000.0, 500.0, 500.0, 500.0, 500.0]
        assert trend_direction(spike) == TrendDirection.FLAT

    def test_sustained_climb_is_a_trend(self) -> None:
        climb = [500.0, 560.0, 615.0, 680.0, 742.0, 800.0, 870.0, 930.0]
        assert trend_direction(climb) == TrendDirection.RISING


class TestProjectCommitted:
    def test_carries_the_recurring_total_forward(self) -> None:
        assert _project_committed([1800.0, 1800.0, 1800.0], 3) == [1800.0] * 3

    def test_median_absorbs_a_double_posted_charge(self) -> None:
        """A single double-post must not propagate into every future month."""
        assert _project_committed([1800.0, 3600.0, 1800.0], 2) == [1800.0] * 2

    def test_median_absorbs_a_missed_charge(self) -> None:
        assert _project_committed([1800.0, 0.0, 1800.0], 2) == [1800.0] * 2

    def test_empty_history_projects_zero(self) -> None:
        assert _project_committed([], 3) == [0.0, 0.0, 0.0]


class TestForecastSeries:
    def test_refuses_to_forecast_below_the_history_floor(self) -> None:
        short = _history([0.0] * 3, [100.0, 110.0, 105.0])
        assert forecast_series(short, date(2026, 6, 1)) == []

    def test_produces_one_row_per_horizon_step(self) -> None:
        history = _history([1800.0] * 8, [1000.0] * 8)
        rows = forecast_series(history, date(2026, 6, 1), horizon=3)
        assert [r.horizon for r in rows] == [1, 2, 3]
        assert [r.period_start for r in rows] == [
            date(2026, 7, 1),
            date(2026, 8, 1),
            date(2026, 9, 1),
        ]

    def test_components_always_sum_to_the_prediction(self) -> None:
        history = _history(
            [1800.0] * 8, [900.0, 1100.0, 1000.0, 1250.0, 980.0, 1120.0, 1040.0, 990.0]
        )
        for row in forecast_series(history, date(2026, 6, 1), horizon=3):
            assert row.predicted_amount == row.committed_amount + row.variable_amount

    def test_interval_brackets_the_prediction(self) -> None:
        history = _history(
            [1800.0] * 8, [900.0, 1100.0, 1000.0, 1250.0, 980.0, 1120.0, 1040.0, 990.0]
        )
        for row in forecast_series(history, date(2026, 6, 1), horizon=3):
            assert row.lower_bound <= row.predicted_amount <= row.upper_bound

    def test_committed_spend_does_not_widen_the_interval(self) -> None:
        """The property the whole decomposition exists for: a mostly-recurring
        category must get a tighter band than a mostly-discretionary one with
        the same total, because only the variable part is uncertain."""
        variable_noise = [900.0, 1100.0, 1000.0, 1250.0, 980.0, 1120.0, 1040.0, 990.0]
        mostly_committed = _history([1000.0] * 8, [v / 10 for v in variable_noise])
        mostly_variable = _history([0.0] * 8, variable_noise)

        committed_rows = forecast_series(mostly_committed, date(2026, 6, 1), horizon=1)
        variable_rows = forecast_series(mostly_variable, date(2026, 6, 1), horizon=1)

        committed_width = committed_rows[0].upper_bound - committed_rows[0].lower_bound
        variable_width = variable_rows[0].upper_bound - variable_rows[0].lower_bound
        assert committed_width < variable_width

    def test_lower_bound_never_goes_negative(self) -> None:
        """A spend forecast below zero is not a meaningful lower bound."""
        history = _history([0.0] * 8, [10.0, 900.0, 5.0, 1200.0, 8.0, 1500.0, 12.0, 20.0])
        for row in forecast_series(history, date(2026, 6, 1), horizon=3):
            assert row.lower_bound >= Decimal("0.00")

    def test_carries_series_identity_onto_every_row(self) -> None:
        history = _history([100.0] * 8, [50.0] * 8, kind=ForecastSeriesKind.BUDGET_CATEGORY)
        rows = forecast_series(history, date(2026, 6, 1), horizon=2)
        assert all(r.series_kind == ForecastSeriesKind.BUDGET_CATEGORY for r in rows)
        assert all(r.series_key == "total_outflow" for r in rows)
        assert all(r.trained_through == date(2026, 6, 1) for r in rows)
        assert all(r.interval_level == DEFAULT_INTERVAL_LEVEL for r in rows)

    def test_history_floor_is_the_documented_constant(self) -> None:
        exactly_enough = _history([0.0] * MIN_HISTORY_MONTHS, [100.0] * MIN_HISTORY_MONTHS)
        assert forecast_series(exactly_enough, date(2026, 6, 1), horizon=1)
