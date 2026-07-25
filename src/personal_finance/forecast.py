"""Spend/income forecasting over the gold marts (Phase 7).

Personal cash flow is two processes with different dynamics, so this module
forecasts them separately and adds the results:

* **Committed** — recurring charges (rent, subscriptions). These are already
  detected by ``gold_recurring_expenses``, so they are *projected* forward on
  each group's own observed cadence rather than forecast. Deterministic:
  they contribute to the point estimate at full weight and to the prediction
  interval at **zero width**.
* **Variable** — everything else. This is the only genuinely uncertain part,
  and the only part a statistical model is asked to predict.

``gold_recurring_expenses`` detects outflows only, so the income series has
no committed component and is modelled in full. That is a real limitation,
not an oversight: a salary is highly predictable and would benefit from the
same treatment if recurring detection is ever extended to inflows.

Modelling the aggregate directly would blend a near-deterministic series with
a noisy one, inflate the interval on categories that are mostly subscriptions,
and produce a number nobody can interrogate. The decomposition instead yields
an explainable result: "$1,800 rent + $27 subscriptions committed, plus
$1,450 ± $380 estimated variable spend".

Design constraints this module is built around, all consequences of monthly
grain over a personal ledger:

* **Series are short.** Two years of exports is 24 points. Yearly seasonality
  needs 2-3 full cycles to estimate, so seasonal models are deliberately not
  offered — they would fit noise. The candidate panel is instead gated by
  history length (see :func:`_candidate_models`).
* **The current month is incomplete.** Training on a partial month drags the
  last point down and makes every model forecast a decline. History is always
  truncated to the last *complete* month (:func:`_last_complete_month`).
* **Cold start is explicit.** Below :data:`MIN_HISTORY_MONTHS` no forecast is
  produced at all, rather than a confidently wrong number.
"""

from __future__ import annotations

import calendar
import logging
import math
import statistics
import warnings
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, NamedTuple

from personal_finance.models import Forecast, ForecastSeriesKind, TrendDirection

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    import duckdb

logger = logging.getLogger(__name__)

MIN_HISTORY_MONTHS = 6
"""Below this many complete months, refuse to forecast rather than guess."""

DEFAULT_HORIZON = 3
"""Months ahead to forecast. Beyond ~6 a monthly personal-spend forecast is
fiction; :func:`compute_forecasts` caps the caller at :data:`MAX_HORIZON`."""

MAX_HORIZON = 6

DEFAULT_INTERVAL_LEVEL = 80
"""Prediction-interval coverage, in percent."""

_INTERVAL_FLOOR_FRACTION = 0.5
"""Floor the conformal half-width at this fraction of the series' typical
month-to-month movement. A near-linear history drives backtest residuals to
~0, which would otherwise publish an extrapolation several months out as a
zero-width 80% interval — maximal confidence exactly where it is least earned.
Fitting history perfectly is not the same as knowing the trend continues."""

_TREND_RELATIVE_THRESHOLD = 0.10
"""Annualized slope must exceed 10% of the series mean to count as a trend
rather than noise — keeps a single expensive month from reading as RISING."""

_CENT = Decimal("0.01")


@dataclass(frozen=True)
class RecurringGroup:
    """One detected recurring charge, as published by gold_recurring_expenses.

    ``amount`` is a positive magnitude and ``avg_gap_days`` is the observed
    spacing between charges — which is what makes cadence-aware projection
    possible: a yearly premium lands in one specific future month, not smeared
    across every month of the horizon.
    """

    merchant_name: str
    amount: float
    cadence: str  # weekly | monthly | quarterly | yearly
    avg_gap_days: float
    last_seen_on: date
    category_id: str | None


@dataclass(frozen=True)
class SeriesHistory:
    """One series' observed monthly history, already decomposed.

    ``months``, ``committed`` and ``variable`` are parallel and ordered oldest
    first, densely filled (a month with no activity is a real 0.0, not a gap —
    "we spent nothing on dining in March" is information, not missing data).
    """

    kind: ForecastSeriesKind
    key: str
    label: str
    category_id: str | None
    months: tuple[date, ...]
    committed: tuple[float, ...]
    variable: tuple[float, ...]
    # The recurring charges that make up this series' committed component.
    # Carried alongside the history so the forward projection can use each
    # group's own cadence instead of collapsing them to a monthly average.
    recurring: tuple[RecurringGroup, ...] = ()

    def __post_init__(self) -> None:
        if not (len(self.months) == len(self.committed) == len(self.variable)):
            message = (
                f"parallel series must be equal length: {len(self.months)} months, "
                f"{len(self.committed)} committed, {len(self.variable)} variable"
            )
            raise ValueError(message)

    @property
    def totals(self) -> tuple[float, ...]:
        return tuple(c + v for c, v in zip(self.committed, self.variable, strict=True))


@dataclass(frozen=True)
class VariableFit:
    """The statistical model's verdict on a variable-spend series."""

    model_name: str
    point: tuple[float, ...]  # one per horizon step
    half_width: tuple[float, ...]  # conformal, one per horizon step
    mase: float | None


def _quantize(value: float) -> Decimal:
    """Round a float to a 2dp money Decimal, matching the warehouse's scale."""
    return Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP)


def _add_months(day: date, months: int) -> date:
    year_delta, month = divmod(day.month - 1 + months, 12)
    return date(day.year + year_delta, month + 1, 1)


def _last_complete_month(today: date) -> date:
    """Return the first day of the most recent *complete* month.

    The current month is always partial, and training on it makes every model
    forecast a spurious decline — the single most likely defect in a naive
    implementation of this feature.
    """
    return _add_months(today.replace(day=1), -1)


# ── Candidate models ────────────────────────────────────────────
# Each takes the variable history and a horizon, and returns one point
# forecast per step. They are deliberately simple and non-seasonal: with
# 6-24 observations, anything richer fits noise. statsmodels supplies the two
# non-trivial ones; the baselines are arithmetic because wrapping a mean in a
# library adds nothing.


def _naive(values: Sequence[float], horizon: int) -> list[float]:
    return [values[-1]] * horizon


def _mean_of_last_3(values: Sequence[float], horizon: int) -> list[float]:
    window = values[-3:]
    return [sum(window) / len(window)] * horizon


def _theta(values: Sequence[float], horizon: int) -> list[float]:
    """statsmodels' Theta method — strong on short series, few parameters.

    ``deseasonalize=False``/``period=1`` because monthly personal-finance
    history is too short to identify yearly seasonality (see module docstring).
    """
    import numpy as np
    from statsmodels.tools.sm_exceptions import ConvergenceWarning
    from statsmodels.tsa.forecasting.theta import ThetaModel

    with warnings.catch_warnings():
        # Only convergence noise — a blanket ignore would also hide overflow
        # and invalid-value RuntimeWarnings, which are real signals here.
        warnings.simplefilter("ignore", ConvergenceWarning)
        # statsmodels 0.14.6 assigns to ndarray.shape, which NumPy 2.5
        # deprecates. Under a strict warning filter (this project runs pytest
        # with filterwarnings=error) that raises, _safe_forecast swallows it,
        # and Theta silently drops out of every selection — which is exactly
        # how it went unnoticed the first time. Remove once statsmodels stops
        # doing this; the forecast itself is unaffected.
        warnings.filterwarnings("ignore", "Setting the shape on a NumPy array", DeprecationWarning)
        # np.asarray, not list(): ThetaModel indexes `.shape` internally, so a
        # plain list raises AttributeError.
        fitted = ThetaModel(np.asarray(values, dtype=float), period=1, deseasonalize=False).fit()
        return [float(v) for v in fitted.forecast(horizon)]


def _ets(values: Sequence[float], horizon: int) -> list[float]:
    """Additive-error, additive-trend exponential smoothing (no seasonality)."""
    from statsmodels.tools.sm_exceptions import ConvergenceWarning
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ConvergenceWarning)
        # A perfectly-fitting series gives ETS a zero residual variance, and it
        # divides by that when standardizing errors. Harmless for the point
        # forecast we take, and that degenerate case is exactly what
        # _INTERVAL_FLOOR_FRACTION exists to keep from being published as
        # certainty. Still narrower than a blanket ignore.
        warnings.filterwarnings("ignore", "invalid value encountered", RuntimeWarning)
        fitted = ETSModel(list(values), error="add", trend="add", seasonal=None).fit(disp=False)
        return [float(v) for v in fitted.forecast(horizon)]


class _Candidate(NamedTuple):
    """A forecasting method and the shortest history it can be fit to."""

    name: str
    min_observations: int
    fn: Callable[[Sequence[float], int], list[float]]


# Ordered cheapest-first. The gates are what each method can actually fit, not
# a preference ranking — selection is by measured error (see fit_variable).
_MODELS: tuple[_Candidate, ...] = (
    _Candidate("naive", 1, _naive),
    _Candidate("mean3", 2, _mean_of_last_3),
    _Candidate("theta", 5, _theta),
    _Candidate("ets", 8, _ets),
)


def _candidate_models(n_observations: int) -> list[_Candidate]:
    """Return the models fittable against ``n_observations`` points."""
    return [m for m in _MODELS if n_observations >= m.min_observations]


def _safe_forecast(
    fn: Callable[..., list[float]], values: Sequence[float], horizon: int
) -> list[float] | None:
    """Run a candidate model, returning None if it cannot fit these values.

    statsmodels raises a variety of errors (convergence, singular matrices,
    degenerate all-zero input) that all mean the same thing here: this model
    is not usable for this series, try the next one.
    """
    try:
        result = fn(values, horizon)
    except Exception:  # any fit failure disqualifies the candidate, whatever its type
        logger.debug("candidate model failed to fit", exc_info=True)
        return None
    if any(not math.isfinite(v) for v in result):
        return None
    return result


def _mean_absolute_naive_error(values: Sequence[float]) -> float:
    """Denominator of MASE: the in-sample one-step naive error."""
    if len(values) < 2:
        return 0.0
    diffs = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    return sum(diffs) / len(diffs)


def _rolling_origin_errors(fn: Callable[..., list[float]], values: Sequence[float]) -> list[float]:
    """Absolute one-step-ahead errors from a rolling-origin backtest.

    Walks the origin forward one month at a time, refitting on everything
    before it and scoring against the held-out actual. These residuals do
    double duty: they rank the candidate models (via MASE) and they calibrate
    the conformal interval, so the band reflects how this model actually
    performed on this series rather than a distributional assumption.
    """
    min_train = max(3, len(values) - 6)  # at most 6 folds, never fewer than 3 points
    errors: list[float] = []
    for origin in range(min_train, len(values)):
        predicted = _safe_forecast(fn, values[:origin], 1)
        if predicted is None:
            continue
        errors.append(abs(values[origin] - predicted[0]))
    return errors


def fit_variable(
    values: Sequence[float],
    horizon: int = DEFAULT_HORIZON,
    interval_level: int = DEFAULT_INTERVAL_LEVEL,
) -> VariableFit:
    """Select and fit a model for one variable-spend series.

    Candidates are ranked by rolling-origin MASE (scale-free, and < 1 means
    "beat naive"), so the winner is chosen by measured out-of-sample error on
    this specific series rather than by a global preference. MAPE is
    deliberately not used: category spend hits zero regularly, where MAPE is
    undefined and explodes just above it.

    The interval is **conformal** — the empirical quantile of the backtest
    residuals, widened by sqrt(h) as the horizon extends. Parametric intervals
    from a model fit to 6-24 points are badly overconfident; a quantile of
    real errors is distribution-free and honest about what we observed.
    """
    if not values:
        message = "cannot fit a forecast to an empty series"
        raise ValueError(message)

    scale = _mean_absolute_naive_error(values)
    best: tuple[float, str, list[float], list[float]] | None = None

    for candidate in _candidate_models(len(values)):
        point = _safe_forecast(candidate.fn, values, horizon)
        if point is None:
            continue
        errors = _rolling_origin_errors(candidate.fn, values)
        if not errors:
            continue
        mae = sum(errors) / len(errors)
        mase = mae / scale if scale > 0 else mae
        if best is None or mase < best[0]:
            best = (mase, candidate.name, point, errors)

    if best is None:  # every candidate failed — fall back to the flat mean
        mean = sum(values) / len(values)
        return VariableFit("mean", (mean,) * horizon, (0.0,) * horizon, None)

    mase, name, point, errors = best
    half_width = _conformal_half_widths(errors, horizon, interval_level, floor=scale)
    return VariableFit(name, tuple(point), half_width, mase if scale > 0 else None)


def _conformal_half_widths(
    errors: Sequence[float], horizon: int, interval_level: int, floor: float = 0.0
) -> tuple[float, ...]:
    """Empirical error quantile per horizon step.

    One-step residuals are the only ones a short series affords enough folds
    to estimate, so later steps scale the one-step band by sqrt(h) — the
    standard random-walk widening. It is an approximation, and an honest one:
    the alternative (per-horizon quantiles from 1-2 folds) is noise.
    """
    if not errors:
        return (0.0,) * horizon
    ordered = sorted(errors)
    # Nearest-rank quantile: with few folds this is more stable than
    # interpolating between order statistics.
    # ceil, not round: nearest-rank needs the smallest index covering the
    # requested fraction. round() picks the median for an 80% level at 3 folds,
    # shipping a band labelled 80% that covers far less.
    index = min(len(ordered) - 1, math.ceil((interval_level / 100) * len(ordered)) - 1)
    base = max(ordered[max(0, index)], _INTERVAL_FLOOR_FRACTION * floor)
    return tuple(base * (step**0.5) for step in range(1, horizon + 1))


def trend_direction(values: Sequence[float]) -> TrendDirection:
    """Classify the series' direction from an ordinary-least-squares slope.

    Compares the annualized slope against the series mean, so the answer is
    "is this climbing meaningfully?" rather than "is the slope non-zero?" —
    which is what separates a genuine upward drift from one expensive month.
    """
    if len(values) < 3:
        return TrendDirection.FLAT
    mean_value = statistics.fmean(values)
    if mean_value == 0:
        return TrendDirection.FLAT
    try:
        slope = _robust_slope(values)
    except ValueError, statistics.StatisticsError:
        return TrendDirection.FLAT
    relative = (slope * 12) / abs(mean_value)
    if relative > _TREND_RELATIVE_THRESHOLD:
        return TrendDirection.RISING
    if relative < -_TREND_RELATIVE_THRESHOLD:
        return TrendDirection.FALLING
    return TrendDirection.FLAT


def _robust_slope(values: Sequence[float]) -> float:
    """Theil-Sen slope: the median of all pairwise slopes.

    Ordinary least squares is not usable for this question. One unusually
    expensive month tilts an OLS line enough to flip the label — and worse,
    it tilts it in whichever direction the outlier happens to sit relative to
    the series midpoint, so a single spike early in the window reads as
    FALLING. Theil-Sen tolerates roughly 29% contaminated points before it
    breaks down, so an outlier month leaves the verdict alone and only a
    sustained drift moves it.
    """
    from scipy.stats import theilslopes

    return float(theilslopes(list(values), list(range(len(values)))).slope)


def forecast_series(
    history: SeriesHistory,
    trained_through: date,
    horizon: int = DEFAULT_HORIZON,
    interval_level: int = DEFAULT_INTERVAL_LEVEL,
) -> list[Forecast]:
    """Produce ``horizon`` months of forecast rows for one decomposed series.

    Returns an empty list when the series is too short to model
    (:data:`MIN_HISTORY_MONTHS`) — an explicit "we don't know" rather than a
    fabricated number.
    """
    if len(history.months) < MIN_HISTORY_MONTHS:
        logger.info(
            "skipping %s: %d complete months < %d required",
            history.key,
            len(history.months),
            MIN_HISTORY_MONTHS,
        )
        return []

    fit = fit_variable(history.variable, horizon, interval_level)
    committed = _project_committed(history.recurring, trained_through, horizon)
    trend = trend_direction(history.totals)

    forecasts: list[Forecast] = []
    for step in range(1, horizon + 1):
        half_width = fit.half_width[step - 1]
        # Every series here is a non-negative magnitude (spend and income are
        # both stored positive), so clamp the modelled component at zero. Theta
        # and ETS extrapolate a trend without bound, and a winding-down
        # category drives them straight past zero into negative "spend" — which
        # is not a smaller forecast, it is a meaningless one. Clamping the
        # component rather than only the bound also keeps lower <= predicted <=
        # upper true, which a bound-only clamp breaks.
        variable = max(0.0, fit.point[step - 1])
        committed_step = max(0.0, committed[step - 1])
        # Quantize the parts, then SUM THE QUANTIZED PARTS. Quantizing the
        # float sum instead lets both halves round up while their total rounds
        # down (0.125 + 0.125 -> 0.26 vs 0.25), breaking the
        # predicted == committed + variable invariant by a cent — which
        # assert_forecast_components_sum compares exactly.
        committed_amount = _quantize(committed_step)
        variable_amount = _quantize(variable)
        predicted_amount = committed_amount + variable_amount
        predicted = float(predicted_amount)
        # The interval covers the variable component only — committed spend is
        # known, so it shifts the band without widening it.
        forecasts.append(
            Forecast(
                series_kind=history.kind,
                series_key=history.key,
                series_label=history.label,
                category_id=history.category_id,
                period_start=_add_months(trained_through, step),
                horizon=step,
                committed_amount=committed_amount,
                variable_amount=variable_amount,
                predicted_amount=predicted_amount,
                lower_bound=_quantize(max(0.0, predicted - half_width)),
                upper_bound=_quantize(predicted + half_width),
                interval_level=interval_level,
                model_name=fit.model_name,
                mase=fit.mase,
                trend=trend,
                trained_through=trained_through,
            )
        )
    return forecasts


_CADENCE_MONTHS = {"monthly": 1, "quarterly": 3, "yearly": 12}
"""Calendar-month step per cadence. Stepping by avg_gap_days instead drifts:
30.4-day hops from a June charge put two "monthly" charges in July and none in
some later month."""


def _next_charge(charge_on: date, cadence: str, avg_gap_days: float) -> date:
    """The next charge date after ``charge_on`` for this cadence."""
    months = _CADENCE_MONTHS.get(cadence)
    if months is None:  # weekly, or an unrecognized label — fall back to the gap
        return charge_on + timedelta(days=max(1.0, avg_gap_days))
    year_delta, month_index = divmod(charge_on.month - 1 + months, 12)
    year, month = charge_on.year + year_delta, month_index + 1
    # Clamp for short months: a charge on the 31st recurs on the 30th in June.
    return date(year, month, min(charge_on.day, calendar.monthrange(year, month)[1]))


def _project_committed(
    groups: Sequence[RecurringGroup], trained_through: date, horizon: int
) -> list[float]:
    """Project each recurring charge forward on its own observed cadence.

    Walks each group's charge dates forward from ``last_seen_on`` in steps of
    its ``avg_gap_days`` and bins them into the horizon's months, so a monthly
    subscription contributes to every month, a quarterly premium to one month
    in three, and an annual one to at most a single month.

    Collapsing the committed history to a monthly average instead (e.g. the
    median of recent months) is wrong in both directions, and silently: an
    annual premium is removed from the variable series but averages to ~0, so
    the money simply disappears from the forecast; a quarterly charge that
    happens to fall inside the averaging window gets projected into every
    month, overstating it threefold.

    Groups that have not charged in roughly two cycles are treated as lapsed
    and dropped — a cancelled subscription should stop being committed spend.
    """
    totals = [0.0] * horizon
    month_index = {
        (month.year, month.month): step - 1
        for step in range(1, horizon + 1)
        for month in (_add_months(trained_through, step),)
    }
    as_of = _add_months(trained_through, 1)  # first day of the first forecast month
    horizon_end = _add_months(trained_through, horizon + 1)  # exclusive

    for group in groups:
        gap = max(1.0, group.avg_gap_days)
        if (as_of - group.last_seen_on).days > 2 * gap:
            continue  # lapsed: no charge for ~two cycles
        charge_on = group.last_seen_on
        # Bounded: every step advances at least a day and horizon is at most
        # MAX_HORIZON months, so this runs a few hundred times at worst.
        while True:
            charge_on = _next_charge(charge_on, group.cadence, gap)
            if charge_on >= horizon_end:
                break
            step_index = month_index.get((charge_on.year, charge_on.month))
            if step_index is not None:
                totals[step_index] += group.amount
    return totals


# ── Warehouse I/O ───────────────────────────────────────────────
# Every series is built dense (zero-filled across a month spine) and split into
# its committed/variable halves in SQL. A charge counts as committed when its
# (merchant_name, amount) matches a detected recurring group — the same key
# gold_recurring_expenses groups on.

_TOTALS_SQL = """
WITH spine AS (
    SELECT unnest(generate_series($start::DATE, $end::DATE, INTERVAL 1 MONTH))::DATE AS month
),
tagged AS (
    SELECT
        date_trunc('month', t.posted_on)::DATE AS month,
        t.amount,
        t.flow,
        r.recurring_expense_id IS NOT NULL AS is_committed
    FROM main_silver.silver_transactions AS t
    LEFT JOIN main_gold.gold_recurring_expenses AS r
        ON r.merchant_name = t.merchant_name AND r.amount = -t.amount
    WHERE NOT t.is_transfer
)
SELECT
    s.month,
    coalesce(sum(CASE WHEN x.flow = 'inflow' AND x.is_committed THEN x.amount END), 0) AS in_com,
    coalesce(sum(CASE WHEN x.flow = 'inflow' AND NOT x.is_committed THEN x.amount END), 0) AS in_var,
    coalesce(sum(CASE WHEN x.flow = 'outflow' AND x.is_committed THEN -x.amount END), 0) AS out_com,
    coalesce(sum(CASE WHEN x.flow = 'outflow' AND NOT x.is_committed THEN -x.amount END), 0) AS out_var
FROM spine AS s
LEFT JOIN tagged AS x ON x.month = s.month
GROUP BY s.month
ORDER BY s.month
"""

_BUDGET_SQL = """
WITH spine AS (
    SELECT unnest(generate_series($start::DATE, $end::DATE, INTERVAL 1 MONTH))::DATE AS month
),
line_items AS (
    SELECT
        date_trunc('month', li.posted_on)::DATE AS month,
        li.amount,
        li.category_id,
        r.recurring_expense_id IS NOT NULL AS is_committed
    FROM main_gold.gold_line_items AS li
    INNER JOIN main_silver.silver_transactions AS t USING (transaction_id)
    LEFT JOIN main_gold.gold_recurring_expenses AS r
        ON r.merchant_name = t.merchant_name AND r.amount = -t.amount
    WHERE li.amount < 0 AND li.category_id IS NOT NULL
),
-- A budget covers its whole category subtree (essentials/groceries also
-- catches essentials/groceries/apples), same rollup as gold_budget_actuals.
in_subtree AS (
    SELECT b.id AS budget_id, li.month, li.amount, li.is_committed
    FROM budgets AS b
    INNER JOIN main_gold.gold_category_ancestors AS anc ON anc.ancestor_id = b.category_id
    INNER JOIN line_items AS li ON li.category_id = anc.category_id
)
SELECT
    b.id AS budget_id,
    b.name,
    b.category_id,
    s.month,
    coalesce(sum(CASE WHEN x.is_committed THEN -x.amount END), 0) AS committed,
    coalesce(sum(CASE WHEN NOT x.is_committed THEN -x.amount END), 0) AS variable
FROM budgets AS b
CROSS JOIN spine AS s
LEFT JOIN in_subtree AS x ON x.budget_id = b.id AND x.month = s.month
GROUP BY b.id, b.name, b.category_id, s.month
ORDER BY b.id, s.month
"""

_HISTORY_WINDOW_MONTHS = 36
"""How far back to pull. Three years is plenty for a non-seasonal monthly
model and keeps a long ledger from slowing the query down. The window is a
cap, not a floor — see load_series, which starts at the ledger's first month
when that is later."""

_LEDGER_START_SQL = """
SELECT date_trunc('month', min(posted_on))::DATE FROM main_silver.silver_transactions
"""

# Each detected recurring group, with the category it most often lands in so a
# budget can claim the ones inside its subtree. A group with no categorized
# line item still appears (category_id NULL) and counts toward total spend.
_RECURRING_GROUPS_SQL = """
SELECT
    r.merchant_name,
    r.amount,
    r.cadence,
    r.avg_gap_days,
    r.last_seen_on,
    (
        SELECT li.category_id
        FROM main_silver.silver_transactions AS t
        INNER JOIN main_gold.gold_line_items AS li USING (transaction_id)
        WHERE t.merchant_name = r.merchant_name
          AND -t.amount = r.amount
          AND li.category_id IS NOT NULL
        GROUP BY li.category_id
        ORDER BY count(*) DESC, li.category_id
        LIMIT 1
    ) AS category_id
FROM main_gold.gold_recurring_expenses AS r
"""

# Which categories fall inside each budget's subtree.
_BUDGET_SUBTREE_SQL = """
SELECT b.id AS budget_id, anc.category_id
FROM budgets AS b
INNER JOIN main_gold.gold_category_ancestors AS anc ON anc.ancestor_id = b.category_id
"""


@dataclass
class _BudgetAccumulator:
    """Mutable per-budget scratch space while streaming the budget query."""

    name: str
    category_id: str
    months: list[date] = field(default_factory=list)
    committed: list[float] = field(default_factory=list)
    variable: list[float] = field(default_factory=list)


def load_series(conn: duckdb.DuckDBPyConnection, trained_through: date) -> list[SeriesHistory]:
    """Read every forecastable series from the warehouse, decomposed.

    ``trained_through`` is the last month included — always a *complete* one
    (see :func:`_last_complete_month`).
    """
    # Start at the ledger's own first month, not blindly _HISTORY_WINDOW_MONTHS
    # back: padding the front with zero months for a period the user simply
    # hadn't started importing yet is not "we spent nothing then", it is "we
    # have no idea". Those fake zeros wreck every downstream statistic — the
    # step from the last pad month to the first real one becomes a huge naive
    # error, which corrupts MASE, and a mostly-zero series flattens the trend
    # slope to nothing.
    ledger_start = conn.execute(_LEDGER_START_SQL).fetchone()
    if ledger_start is None or ledger_start[0] is None:
        return []
    window_start = _add_months(trained_through, -(_HISTORY_WINDOW_MONTHS - 1))
    start = max(ledger_start[0], window_start)
    if start > trained_through:
        return []  # every transaction is in the current (incomplete) month or later
    params = {"start": start, "end": trained_through}

    groups = tuple(
        RecurringGroup(
            merchant_name=name,
            amount=float(amount),
            cadence=cadence,
            avg_gap_days=float(avg_gap_days),
            last_seen_on=last_seen_on,
            category_id=category_id,
        )
        for name, amount, cadence, avg_gap_days, last_seen_on, category_id in conn.execute(
            _RECURRING_GROUPS_SQL
        ).fetchall()
    )
    subtree: dict[str, set[str]] = {}
    for budget_id, category_id in conn.execute(_BUDGET_SUBTREE_SQL).fetchall():
        subtree.setdefault(budget_id, set()).add(category_id)

    rows = conn.execute(_TOTALS_SQL, params).fetchall()
    months = tuple(row[0] for row in rows)
    series = [
        SeriesHistory(
            kind=ForecastSeriesKind.TOTAL_INFLOW,
            key="total_inflow",
            label="Total income",
            category_id=None,
            months=months,
            committed=tuple(float(row[1]) for row in rows),
            variable=tuple(float(row[2]) for row in rows),
            # gold_recurring_expenses only detects outflows, so income has no
            # committed component to project — it is modelled in full.
            recurring=(),
        ),
        SeriesHistory(
            kind=ForecastSeriesKind.TOTAL_OUTFLOW,
            key="total_outflow",
            label="Total spend",
            category_id=None,
            months=months,
            committed=tuple(float(row[3]) for row in rows),
            variable=tuple(float(row[4]) for row in rows),
            recurring=groups,
        ),
    ]

    # The budget query returns one row per (budget, month), ordered by budget
    # then month, so accumulate each budget's parallel arrays as they stream by.
    by_budget: dict[str, _BudgetAccumulator] = {}
    for budget_id, name, category_id, month, committed, variable in conn.execute(
        _BUDGET_SQL, params
    ).fetchall():
        entry = by_budget.setdefault(budget_id, _BudgetAccumulator(name, category_id))
        entry.months.append(month)
        entry.committed.append(float(committed))
        entry.variable.append(float(variable))

    series.extend(
        SeriesHistory(
            kind=ForecastSeriesKind.BUDGET_CATEGORY,
            key=budget_id,
            label=entry.name,
            category_id=entry.category_id,
            months=tuple(entry.months),
            committed=tuple(entry.committed),
            variable=tuple(entry.variable),
            recurring=tuple(g for g in groups if g.category_id in subtree.get(budget_id, set())),
        )
        for budget_id, entry in by_budget.items()
    )
    return series


_INSERT_FORECAST = """
INSERT INTO forecasts (
    id, created_at, series_kind, series_key, series_label, category_id, period_start,
    horizon, committed_amount, variable_amount, predicted_amount, lower_bound, upper_bound,
    interval_level, model_name, mase, trend, trained_through, note
) VALUES (
    $id, $created_at, $series_kind, $series_key, $series_label, $category_id, $period_start,
    $horizon, $committed_amount, $variable_amount, $predicted_amount, $lower_bound, $upper_bound,
    $interval_level, $model_name, $mase, $trend, $trained_through, $note
)
"""


def compute_forecasts(
    conn: duckdb.DuckDBPyConnection,
    horizon: int = DEFAULT_HORIZON,
    today: date | None = None,
    interval_level: int = DEFAULT_INTERVAL_LEVEL,
) -> int:
    """Rebuild every series' forecast, replacing the previous run.

    Forecasts are a full recompute rather than an incremental cache (unlike
    embeddings or LLM categories): every new transaction changes the history
    that every model was fit to, so a stale row is simply wrong. Returns the
    number of forecast rows written.
    """
    horizon = max(1, min(horizon, MAX_HORIZON))
    trained_through = _last_complete_month(today or date.today())

    rows: list[Forecast] = []
    for history in load_series(conn, trained_through):
        rows.extend(forecast_series(history, trained_through, horizon, interval_level))

    if not rows:
        # Leave the previous run's forecasts alone. Wiping them because this
        # run produced nothing turns a transient upstream problem (an empty
        # warehouse, a half-finished transform) into lost output, and the
        # caller cannot tell that from a legitimate cold start.
        logger.warning("no series produced a forecast; leaving existing forecasts untouched")
        return 0

    # One transaction: DuckDB autocommits each execute, so a failure partway
    # through would otherwise leave gold_forecasts publishing (say) an income
    # projection with no matching spend projection — a cash-flow view that is
    # wrong rather than merely absent.
    conn.execute("BEGIN TRANSACTION")
    try:
        conn.execute("DELETE FROM forecasts")
        for forecast in rows:
            payload = forecast.model_dump()
            payload["series_kind"] = forecast.series_kind.value
            payload["trend"] = forecast.trend.value
            conn.execute(_INSERT_FORECAST, payload)
    except Exception:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")
    return len(rows)
