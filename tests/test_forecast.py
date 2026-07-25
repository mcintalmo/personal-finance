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
    _MODELS,
    DEFAULT_INTERVAL_LEVEL,
    MIN_HISTORY_MONTHS,
    RecurringGroup,
    SeriesHistory,
    _add_months,
    _candidate_models,
    _conformal_half_widths,
    _last_complete_month,
    _mean_absolute_naive_error,
    _project_committed,
    _rolling_origin_errors,
    _safe_forecast,
    fit_variable,
    forecast_series,
    trend_direction,
)
from personal_finance.models import ForecastSeriesKind, TrendDirection


def _monthly(amount: float, last_seen_on: date = date(2026, 6, 1)) -> RecurringGroup:
    return RecurringGroup("RENT", amount, "monthly", 30.4, last_seen_on, None)


def _history(
    committed: list[float],
    variable: list[float],
    kind: ForecastSeriesKind = ForecastSeriesKind.TOTAL_OUTFLOW,
    recurring: tuple[RecurringGroup, ...] = (),
) -> SeriesHistory:
    months = tuple(_add_months(date(2026, 1, 1), i) for i in range(len(variable)))
    is_budget = kind is ForecastSeriesKind.BUDGET_CATEGORY
    return SeriesHistory(
        kind=kind,
        key="budget-1" if is_budget else kind.value,
        label="Total spend",
        category_id="cat-1" if is_budget else None,
        months=months,
        committed=tuple(committed),
        variable=tuple(variable),
        recurring=recurring,
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
        assert all(r.series_key == "budget-1" for r in rows)
        assert all(r.trained_through == date(2026, 6, 1) for r in rows)
        assert all(r.interval_level == DEFAULT_INTERVAL_LEVEL for r in rows)

    def test_history_floor_is_the_documented_constant(self) -> None:
        exactly_enough = _history([0.0] * MIN_HISTORY_MONTHS, [100.0] * MIN_HISTORY_MONTHS)
        assert forecast_series(exactly_enough, date(2026, 6, 1), horizon=1)


class TestEveryCandidateActuallyFits:
    """The bug this class exists for: `_theta` was passed a plain list, which
    statsmodels rejects, so `_safe_forecast` swallowed an AttributeError and
    Theta was silently disqualified for every series ever forecast. Asserting
    that a model is *listed* is not the same as asserting it *runs*."""

    @pytest.mark.parametrize("candidate", _MODELS, ids=lambda c: c.name)
    def test_candidate_produces_a_finite_forecast(self, candidate) -> None:
        values = [100.0, 120.0, 90.0, 140.0, 110.0, 130.0, 105.0, 125.0, 95.0, 135.0]
        result = _safe_forecast(candidate.fn, values, 3)
        assert result is not None, f"{candidate.name} failed to fit a well-behaved series"
        assert len(result) == 3

    @pytest.mark.parametrize("candidate", _MODELS, ids=lambda c: c.name)
    def test_candidate_backtests(self, candidate) -> None:
        """A model that cannot backtest is silently unselectable."""
        values = [100.0, 120.0, 90.0, 140.0, 110.0, 130.0, 105.0, 125.0, 95.0, 135.0]
        assert _rolling_origin_errors(candidate.fn, values), f"{candidate.name} produced no folds"

    def test_selection_reports_a_real_model_name(self) -> None:
        fit = fit_variable([100.0, 120.0, 90.0, 140.0, 110.0, 130.0, 105.0, 125.0], horizon=3)
        assert fit.model_name in {m.name for m in _MODELS}


class TestProjectCommittedCadence:
    """Recurring charges must be projected on their own cadence. Collapsing the
    committed history to a monthly average loses annual charges entirely and
    triples quarterly ones."""

    def test_monthly_charge_lands_in_every_month(self) -> None:
        groups = (RecurringGroup("RENT", 1800.0, "monthly", 30.4, date(2026, 6, 1), None),)
        assert _project_committed(groups, date(2026, 6, 1), 3) == pytest.approx([1800.0] * 3)

    def test_annual_charge_does_not_vanish(self) -> None:
        """It is removed from the variable series, so if it also projects as
        zero the money simply disappears from the forecast."""
        groups = (RecurringGroup("INSURANCE", 600.0, "yearly", 365.0, date(2026, 6, 15), None),)
        # next charge is ~2027-06-15, outside a 3-month horizon
        assert _project_committed(groups, date(2026, 6, 1), 3) == pytest.approx([0.0] * 3)
        # ...but inside a horizon that reaches it, it appears exactly once
        twelve = _project_committed(groups, date(2027, 4, 1), 3)
        assert sum(twelve) == pytest.approx(600.0)
        assert sorted(twelve)[-1] == pytest.approx(600.0)

    def test_quarterly_charge_is_not_smeared_across_every_month(self) -> None:
        groups = (RecurringGroup("WATER", 300.0, "quarterly", 91.0, date(2026, 6, 10), None),)
        projected = _project_committed(groups, date(2026, 6, 1), 3)
        assert sum(projected) == pytest.approx(300.0)  # exactly one charge, not three

    def test_lapsed_subscription_stops_being_committed(self) -> None:
        """Cancelled six months ago; it should not be projected forward."""
        groups = (RecurringGroup("OLD GYM", 40.0, "monthly", 30.4, date(2025, 12, 1), None),)
        assert _project_committed(groups, date(2026, 6, 1), 3) == pytest.approx([0.0] * 3)

    def test_no_groups_projects_zero(self) -> None:
        assert _project_committed((), date(2026, 6, 1), 3) == [0.0, 0.0, 0.0]


class TestIntervalFloor:
    def test_near_perfect_fit_still_expresses_uncertainty(self) -> None:
        """A perfectly linear history drives backtest residuals to ~0. Without
        a floor this publishes a multi-month extrapolation as a zero-width 80%
        interval — maximal confidence exactly where it is least earned."""
        fit = fit_variable([300.0 + 20 * i for i in range(12)], horizon=6)
        assert all(hw > 0 for hw in fit.half_width)
        assert fit.half_width[0] < fit.half_width[5]


class TestSeriesHistoryInvariant:
    def test_mismatched_parallel_arrays_are_rejected_at_construction(self) -> None:
        with pytest.raises(ValueError, match="equal length"):
            SeriesHistory(
                kind=ForecastSeriesKind.TOTAL_OUTFLOW,
                key="k",
                label="l",
                category_id=None,
                months=(date(2026, 1, 1), date(2026, 2, 1)),
                committed=(1.0,),
                variable=(1.0, 2.0),
            )


class TestForecastRowInvariants:
    def test_components_sum_exactly_after_quantization(self) -> None:
        """Quantizing the float sum instead of summing the quantized parts lets
        both halves round up while the total rounds down, breaking the
        invariant the dbt test compares exactly."""
        history = _history(
            [0.0] * 8,
            [1.005] * 8,
            recurring=(RecurringGroup("X", 1.005, "monthly", 30.4, date(2026, 6, 1), None),),
        )
        for row in forecast_series(history, date(2026, 6, 1), horizon=3):
            assert row.predicted_amount == row.committed_amount + row.variable_amount

    def test_declining_series_never_yields_a_negative_forecast(self) -> None:
        """Theta/ETS extrapolate a trend without bound; negative spend is not a
        smaller forecast, it is a meaningless one — and it inverted the bounds."""
        declining = [900.0, 750.0, 600.0, 450.0, 300.0, 150.0, 50.0, 10.0]
        for row in forecast_series(_history([0.0] * 8, declining), date(2026, 6, 1), horizon=6):
            assert row.predicted_amount >= Decimal("0.00")
            assert row.lower_bound <= row.predicted_amount <= row.upper_bound
