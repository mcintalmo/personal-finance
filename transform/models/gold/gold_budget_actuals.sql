-- Budget vs. actual: one row per (budget, period bucket) with the budgeted
-- amount alongside the real outflow rolled up from gold_line_items across
-- the budget's whole category subtree (essentials/groceries covers
-- essentials/groceries/apples too), bucketed at the budget's own cadence
-- (weekly/monthly/quarterly/yearly) via date_trunc. Powers Phase 6's
-- budget-vs-actual view — a time series per budget, not just a single
-- lifetime total, so a dashboard can show whether a budget was blown in a
-- specific month.
--
-- Only outflow counts against a budget (a refund/inflow in a spend category
-- offsets rather than adds to the budget check, same as every other
-- spend measure in this project). starts_on gates out activity before the
-- budget existed.
--
-- A period bucket with zero activity in the budget's subtree is absent
-- (this is a sparse time series, not a zero-filled calendar) — a dashboard
-- computes the buckets it wants to display and left-joins in.

with budgets as (
    select
        id as budget_id,
        name,
        category_id,
        period,
        amount as budgeted_amount,
        starts_on
    from {{ source('app', 'budgets') }}
),

in_subtree as (
    select
        b.budget_id,
        li.line_item_id,
        li.amount,
        li.posted_on
    from budgets as b
    inner join {{ ref('gold_category_ancestors') }} as anc on anc.ancestor_id = b.category_id
    inner join {{ ref('gold_line_items') }} as li on li.category_id = anc.category_id
    where li.posted_on >= b.starts_on and li.amount < 0
),

bucketed as (
    select
        s.budget_id,
        case b.period
            when 'weekly' then date_trunc('week', s.posted_on)
            when 'monthly' then date_trunc('month', s.posted_on)
            when 'quarterly' then date_trunc('quarter', s.posted_on)
            when 'yearly' then date_trunc('year', s.posted_on)
        end as period_start,
        s.amount
    from in_subtree as s
    inner join budgets as b using (budget_id)
)

select
    b.budget_id,
    b.name,
    b.category_id,
    b.period,
    b.budgeted_amount,
    x.period_start,
    sum(-x.amount) as actual_outflow,
    sum(-x.amount) - b.budgeted_amount as variance
from bucketed as x
inner join budgets as b using (budget_id)
group by b.budget_id, b.name, b.category_id, b.period, b.budgeted_amount, x.period_start
