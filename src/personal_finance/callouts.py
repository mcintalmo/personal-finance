"""Trend and anomaly callouts over the gold marts (Phase 7).

A dashboard full of charts still leaves the user to spot what changed. This
module does that spotting: it reads the same monthly histories the forecaster
fits (:func:`personal_finance.forecast.load_series`) plus the forecasts that
run produced, and emits a small ranked list of plain-language observations —
"groceries was 2.6x its usual last month", "dining out is trending up",
"the travel budget is projected to run over".

**Nothing here is persisted.** Unlike forecasts, which cache an expensive
model fit in a table and publish it through dbt, a callout is a cheap
re-derivation over marts that already exist: a few aggregates and a median
over at most three years of monthly points. Storing it would buy nothing and
introduce a staleness window in which the dashboard shows a callout about a
month whose underlying numbers have since changed. It is computed on demand
and shared by the CLI (``pf callouts``) and the API (``GET /callouts``).

Anomaly detection uses the **modified z-score** (Iglewicz & Hoaglin), i.e.
deviation from the median scaled by the median absolute deviation, rather than
a mean/standard-deviation z-score. On a personal ledger the outlier is often
several times the typical month, and it drags a mean and inflates a standard
deviation enough to hide itself — the classic masking failure. The median and
MAD barely move, so the spike stays visible.
"""

from __future__ import annotations

import logging
import statistics
from datetime import date
from enum import StrEnum
from typing import TYPE_CHECKING, NamedTuple

from pydantic import BaseModel

from personal_finance.forecast import last_complete_month, load_series
from personal_finance.models import BudgetPeriod, ForecastSeriesKind, TrendDirection

if TYPE_CHECKING:
    from collections.abc import Sequence
    from decimal import Decimal

    import duckdb

    from personal_finance.forecast import SeriesHistory

logger = logging.getLogger(__name__)

ROBUST_Z_THRESHOLD = 3.5
"""Modified z-score at which a month counts as anomalous. 3.5 is Iglewicz &
Hoaglin's published cutoff, and on 6-36 monthly points it is the difference
between "notably expensive" and "flagging every third month"."""

MIN_ANOMALY_AMOUNT = 50.0
"""A month must also deviate from the median by at least this many dollars.
The z-score is scale-free, which is exactly wrong for a near-zero category:
$4 against a $1 median is a huge z-score and a completely uninteresting
callout."""

ANOMALY_LOOKBACK_MONTHS = 3
"""Only call out anomalies in the last few complete months. Older ones are
still real, but a callout the user cannot act on is noise on a dashboard."""

MIN_ANOMALY_HISTORY_MONTHS = 6
"""Months of history required before any month can be called unusual. Below
this a median is not a description of "typical" — with three points the third
one always looks extreme relative to the other two.

Deliberately its own constant rather than the forecaster's
:data:`~personal_finance.forecast.MIN_HISTORY_MONTHS`, which they currently
happen to agree on: one is "enough points to fit a model", the other is
"enough points for a median to mean anything". Tying them together would let
a change to the forecasting floor silently move the anomaly threshold."""

_MAD_TO_SIGMA = 0.6745
"""Consistency constant: MAD * this approximates a normal distribution's
standard deviation, which is what makes 3.5 comparable to a z-score."""

_MEANAD_TO_SIGMA = 1.253314
"""Fallback scale when MAD is exactly 0 — which happens whenever more than
half the months are identical (a category that is usually 0). Without it every
nonzero month in a mostly-zero series would divide by zero."""

_MONTHS_PER_PERIOD: dict[BudgetPeriod, float] = {
    BudgetPeriod.WEEKLY: 7 / (365.25 / 12),
    BudgetPeriod.MONTHLY: 1.0,
    BudgetPeriod.QUARTERLY: 3.0,
    BudgetPeriod.YEARLY: 12.0,
}
"""How many months one budget period spans. Forecasts are monthly, so a
non-monthly budget has to be converted before the two can be compared at all —
holding a monthly projection up against a yearly cap would read as "wildly
under budget" every single month."""


class CalloutKind(StrEnum):
    """What kind of observation a callout makes."""

    SPIKE = "spike"
    DIP = "dip"
    TREND = "trend"
    BUDGET_RISK = "budget_risk"


class CalloutLevel(StrEnum):
    """How much attention a callout wants.

    Deliberately not a pure function of magnitude: whether a change is good or
    bad depends on which way the money is moving. Income falling and spending
    rising are both WARNING; income rising and spending falling are both INFO.
    """

    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_LEVEL_ORDER: dict[CalloutLevel, int] = {
    CalloutLevel.CRITICAL: 0,
    CalloutLevel.WARNING: 1,
    CalloutLevel.INFO: 2,
}


class Callout(BaseModel):
    """One plain-language observation about a series.

    ``title`` is the headline; ``detail`` carries the numbers that justify it,
    so the user can disagree with the heuristic rather than having to trust
    it. ``rank`` orders callouts of the same level — bigger is more notable.
    """

    kind: CalloutKind
    level: CalloutLevel
    title: str
    detail: str
    series_kind: ForecastSeriesKind
    series_key: str
    series_label: str
    category_id: str | None = None
    period_start: date | None = None
    rank: float = 0.0


class CalloutFeed(BaseModel):
    """The callouts, plus why some kinds might be missing.

    ``forecasts_available`` exists so an empty (or trend-free) feed can be
    explained rather than silently shrugged at: with no forecast rows, TREND
    and BUDGET_RISK cannot be produced at all, and a dashboard that just shows
    "nothing to report" would be claiming something it never checked.
    """

    callouts: list[Callout]
    forecasts_available: bool


def _is_outflow(kind: ForecastSeriesKind) -> bool:
    """Whether more of this series is bad news. Budgets measure spend."""
    return kind is not ForecastSeriesKind.TOTAL_INFLOW


def _robust_scale(values: Sequence[float], median: float) -> float:
    """Median-absolute-deviation scale, with a mean-absolute fallback.

    Returns 0.0 for a perfectly constant series, which has no scale to measure
    deviations against and therefore no detectable anomalies.
    """
    mad = statistics.median(abs(v - median) for v in values)
    if mad > 0:
        return mad / _MAD_TO_SIGMA
    mean_ad = sum(abs(v - median) for v in values) / len(values)
    return mean_ad * _MEANAD_TO_SIGMA if mean_ad > 0 else 0.0


def _money(value: float) -> str:
    return f"${value:,.0f}"


def _anomaly_callouts(history: SeriesHistory, trained_through: date) -> list[Callout]:
    """Flag recent months that are far from this series' own typical month.

    Judged against the *whole* history, not a trailing window: a trailing
    window that happens to contain the previous spike raises the bar just as
    the repeat spike arrives, which is precisely when the user wants to know.
    """
    totals = history.totals
    if len(totals) < MIN_ANOMALY_HISTORY_MONTHS:
        return []
    median = statistics.median(totals)
    scale = _robust_scale(totals, median)
    if scale <= 0:
        return []  # constant series: nothing deviates from anything

    outflow = _is_outflow(history.kind)
    cutoff_index = max(0, len(totals) - ANOMALY_LOOKBACK_MONTHS)
    callouts: list[Callout] = []
    for index in range(cutoff_index, len(totals)):
        value = totals[index]
        deviation = value - median
        if abs(deviation) < MIN_ANOMALY_AMOUNT:
            continue
        z = deviation / scale
        if abs(z) < ROBUST_Z_THRESHOLD:
            continue
        high = deviation > 0
        month = history.months[index]
        month_label = month.strftime("%B %Y")
        noun = "spending" if outflow else "income"
        # High spend and low income are the concerning directions; the other
        # two are worth surfacing but not worth alarming about.
        level = CalloutLevel.WARNING if high == outflow else CalloutLevel.INFO
        multiple = f" ({value / median:.1f}x the usual)" if median > 0 else ""
        callouts.append(
            Callout(
                kind=CalloutKind.SPIKE if high else CalloutKind.DIP,
                level=level,
                title=(
                    f"{history.label}: unusually "
                    f"{'high' if high else 'low'} {noun} in {month_label}"
                ),
                detail=(
                    f"{_money(value)} against a typical {_money(median)}{multiple} — "
                    f"{abs(z):.1f} robust standard deviations from the median of "
                    f"{len(totals)} months through {trained_through:%b %Y}."
                ),
                series_kind=history.kind,
                series_key=history.key,
                series_label=history.label,
                category_id=history.category_id,
                period_start=month,
                rank=abs(z),
            )
        )
    return callouts


_NEXT_FORECAST_SQL = """
SELECT
    f.series_kind, f.series_key, f.series_label, f.category_id, f.period_start,
    f.predicted_amount, f.lower_bound, f.upper_bound, f.trend,
    b.amount AS budgeted_amount, b.period AS budget_period
FROM forecasts AS f
LEFT JOIN budgets AS b
    ON b.id = f.series_key AND f.series_kind = 'budget_category'
WHERE f.horizon = 1
ORDER BY f.series_key
"""
"""The next month's forecast for every series, with the budget it is measured
against when there is one.

Reads the `forecasts` app table rather than `gold_forecasts`: the gold model
is only refreshed by the next `pf transform`, so between `pf forecast` and
that run the mart holds the *previous* forecast — and a callout is a claim
about right now. horizon = 1 because a callout is about the month the user is
in a position to change; horizon 3 is context for a chart, not a nudge.
"""


class ForecastRow(NamedTuple):
    """One row of :data:`_NEXT_FORECAST_SQL`, in select order.

    Field order is load-bearing — rows are constructed positionally from the
    cursor — which is exactly why this is a NamedTuple rather than a dict
    zipped against a separately maintained list of column names. There is one
    place to keep in step with the query instead of two.
    """

    series_kind: str
    series_key: str
    series_label: str
    category_id: str | None
    period_start: date
    # Money columns are DECIMAL(18, 2), so DuckDB hands back Decimal — the
    # float alternative is for hand-built rows in tests. Every use site
    # converts before arithmetic rather than mixing the two.
    predicted_amount: Decimal | float
    lower_bound: Decimal | float
    upper_bound: Decimal | float
    trend: str
    budgeted_amount: Decimal | float | None
    budget_period: str | None


def _trend_callout(row: ForecastRow, history: SeriesHistory | None) -> Callout | None:
    trend = TrendDirection(row.trend)
    if trend is TrendDirection.FLAT:
        return None
    kind = ForecastSeriesKind(row.series_kind)
    rising = trend is TrendDirection.RISING
    outflow = _is_outflow(kind)
    predicted = float(row.predicted_amount)

    # A brand-new budget can have a forecast before it has any spend to
    # average against; comparing to that zero would divide by it.
    totals = history.totals if history is not None else ()
    baseline = statistics.mean(totals) if totals else 0.0
    if baseline <= 0:
        comparison = f"next month is projected at {_money(predicted)}."
        rank = 0.0
    else:
        change = (predicted - baseline) / baseline
        comparison = (
            f"next month is projected at {_money(predicted)}, "
            f"{abs(change):.0%} {'above' if change >= 0 else 'below'} the "
            f"{_money(baseline)} average of the last {len(totals)} months."
        )
        rank = abs(change)

    noun = "Spending" if outflow else "Income"
    return Callout(
        kind=CalloutKind.TREND,
        level=CalloutLevel.WARNING if rising == outflow else CalloutLevel.INFO,
        title=f"{row.series_label}: {noun.lower()} is trending {'up' if rising else 'down'}",
        detail=f"The fitted trend over the observed history is {trend.value}, and {comparison}",
        series_kind=kind,
        series_key=row.series_key,
        series_label=row.series_label,
        category_id=row.category_id,
        period_start=row.period_start,
        rank=rank,
    )


def _budget_risk_callout(row: ForecastRow) -> Callout | None:
    """Flag a budget whose next month is projected to run over.

    Uses the forecast's lower bound to separate "might overrun" from "will
    overrun barring a change": if even the optimistic end of the interval
    clears the cap, the overrun is not a modelling artifact.
    """
    if row.budgeted_amount is None:
        return None
    try:
        period = BudgetPeriod(row.budget_period)
    except ValueError:
        logger.warning(
            "budget %s has unrecognized period %r; skipping its risk callout",
            row.series_key,
            row.budget_period,
        )
        return None

    # Divide, don't multiply: the table says how many months one period spans,
    # so a $6,000 yearly cap is $500/month and a $400 weekly cap is ~$1,739.
    monthly_cap = float(row.budgeted_amount) / _MONTHS_PER_PERIOD[period]
    predicted = float(row.predicted_amount)
    if predicted <= monthly_cap:
        return None

    lower = float(row.lower_bound)
    certain = lower > monthly_cap
    overrun = predicted - monthly_cap
    cap_note = (
        "" if period is BudgetPeriod.MONTHLY else f" ({period.value} budget, prorated per month)"
    )
    return Callout(
        kind=CalloutKind.BUDGET_RISK,
        level=CalloutLevel.CRITICAL if certain else CalloutLevel.WARNING,
        title=(
            f"{row.series_label}: projected to "
            f"{'exceed' if certain else 'risk exceeding'} its budget"
        ),
        detail=(
            f"{_money(predicted)} projected against a {_money(monthly_cap)} monthly "
            f"budget{cap_note} — {_money(overrun)} over. "
            + (
                "Even the low end of the forecast interval clears the cap."
                if certain
                else f"The forecast interval starts at {_money(lower)}, so it may not happen."
            )
        ),
        series_kind=ForecastSeriesKind(row.series_kind),
        series_key=row.series_key,
        series_label=row.series_label,
        category_id=row.category_id,
        period_start=row.period_start,
        rank=overrun / monthly_cap if monthly_cap > 0 else overrun,
    )


def detect_callouts(
    conn: duckdb.DuckDBPyConnection,
    today: date | None = None,
    limit: int | None = None,
) -> CalloutFeed:
    """Build the ranked callout feed from the warehouse's current state.

    ``today`` is injectable so tests (and a backfill) get a deterministic
    result; it only decides which month counts as the last complete one.
    ``limit`` caps the returned list *after* ranking, so trimming drops the
    least notable callouts rather than an arbitrary slice.
    """
    trained_through = last_complete_month(today or date.today())
    histories = load_series(conn, trained_through)
    by_key = {history.key: history for history in histories}

    callouts: list[Callout] = []
    for history in histories:
        callouts.extend(_anomaly_callouts(history, trained_through))

    forecast_rows = [ForecastRow(*values) for values in conn.execute(_NEXT_FORECAST_SQL).fetchall()]
    for row in forecast_rows:
        trend = _trend_callout(row, by_key.get(row.series_key))
        if trend is not None:
            callouts.append(trend)
        risk = _budget_risk_callout(row)
        if risk is not None:
            callouts.append(risk)

    callouts.sort(key=lambda c: (_LEVEL_ORDER[c.level], -c.rank, c.series_label))
    if limit is not None:
        callouts = callouts[:limit]
    return CalloutFeed(callouts=callouts, forecasts_available=bool(forecast_rows))
