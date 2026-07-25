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

import logging
import math
import statistics
import warnings
from dataclasses import dataclass, field
from datetime import date
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

_TREND_RELATIVE_THRESHOLD = 0.10
"""Annualized slope must exceed 10% of the series mean to count as a trend
rather than noise — keeps a single expensive month from reading as RISING."""

_CENT = Decimal("0.01")


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
    from statsmodels.tsa.forecasting.theta import ThetaModel

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        fitted = ThetaModel(list(values), period=1, deseasonalize=False).fit()
        return [float(v) for v in fitted.forecast(horizon)]


def _ets(values: Sequence[float], horizon: int) -> list[float]:
    """Additive-error, additive-trend exponential smoothing (no seasonality)."""
    from statsmodels.tsa.exponential_smoothing.ets import ETSModel

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
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


def _rolling_origin_errors(
    fn: Callable[..., list[float]], values: Sequence[float], horizon: int
) -> list[float]:
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
        errors = _rolling_origin_errors(candidate.fn, values, horizon)
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
    half_width = _conformal_half_widths(errors, horizon, interval_level)
    return VariableFit(name, tuple(point), half_width, mase if scale > 0 else None)


def _conformal_half_widths(
    errors: Sequence[float], horizon: int, interval_level: int
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
    index = min(len(ordered) - 1, round((interval_level / 100) * len(ordered)) - 1)
    base = ordered[max(0, index)]
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
    committed = _project_committed(history.committed, horizon)
    trend = trend_direction(history.totals)

    forecasts: list[Forecast] = []
    for step in range(1, horizon + 1):
        variable = fit.point[step - 1]
        half_width = fit.half_width[step - 1]
        committed_step = committed[step - 1]
        predicted = committed_step + variable
        # The interval covers the variable component only — committed spend is
        # known, so it shifts the band without widening it. Clamped at zero:
        # a spend forecast below zero is not a meaningful lower bound.
        forecasts.append(
            Forecast(
                series_kind=history.kind,
                series_key=history.key,
                series_label=history.label,
                category_id=history.category_id,
                period_start=_add_months(trained_through, step),
                horizon=step,
                committed_amount=_quantize(committed_step),
                variable_amount=_quantize(variable),
                predicted_amount=_quantize(predicted),
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


def _project_committed(committed: Sequence[float], horizon: int) -> list[float]:
    """Carry the committed component forward.

    Recurring charges recur, so the most recent month's committed total is the
    best estimate of the next one's. The median of the last three months is
    used rather than the last value alone, so a single missed or double-posted
    subscription charge doesn't propagate into every future month.
    """
    if not committed:
        return [0.0] * horizon
    window = committed[-3:]
    return [statistics.median(window)] * horizon


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
        ),
        SeriesHistory(
            kind=ForecastSeriesKind.TOTAL_OUTFLOW,
            key="total_outflow",
            label="Total spend",
            category_id=None,
            months=months,
            committed=tuple(float(row[3]) for row in rows),
            variable=tuple(float(row[4]) for row in rows),
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
        )
        for budget_id, entry in by_budget.items()
    )
    return series


_UPSERT_FORECAST = """
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

    conn.execute("DELETE FROM forecasts")
    for forecast in rows:
        payload = forecast.model_dump()
        payload["series_kind"] = forecast.series_kind.value
        payload["trend"] = forecast.trend.value
        conn.execute(_UPSERT_FORECAST, payload)
    return len(rows)
