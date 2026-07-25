-- Recurring-flow detection: one row per (merchant_name, signed amount) pair
-- whose transactions repeat at a regular cadence. Covers BOTH directions —
-- outflows (rent, subscriptions) and inflows (salary, pension, a standing
-- transfer in from someone else). The detection heuristic is identical in
-- both directions, so this is one model with a `flow` column rather than two
-- near-duplicate ones; `amount` is published as a positive magnitude and
-- `flow` says which way the money moved.
--
-- Reads silver_transactions directly (not gold_line_items) — a subscription
-- charge or a paycheck is a whole-transaction concept tied to merchant_name,
-- not something that gets decomposed into splits, same rationale as
-- gold_monthly_flow. Excludes is_transfer, same convention as every other
-- flow measure here: an internal move between the user's own accounts is
-- regular by nature and is not income.
--
-- Heuristic: group same-merchant, same-signed-amount transactions, require at
-- least 3 occurrences (2 gaps — one gap alone can't distinguish "regular"
-- from "coincidence"), bucket the average gap into a cadence, then keep only
-- groups where the gap is actually regular (stddev <= recurring_regularity_
-- threshold of the average) — a merchant charged the same amount twice at
-- random is not "recurring". Grouping on the SIGNED amount matters: a
-- merchant that both charges and refunds $40 is two distinct groups, not one
-- group whose interleaved dates produce meaningless gaps.
--
-- Cadence day-ranges and the regularity threshold are dbt vars
-- (transform/dbt_project.yml), same convention as every other tunable
-- heuristic cutoff in this project (transfer_window_days,
-- embedding_confidence_threshold, ...). The biweekly bucket exists for
-- income specifically: a fortnightly paycheck sits at ~14 days, which falls
-- in the gap between the weekly and monthly buckets and would otherwise be
-- dropped as "irregular" — the single most common salary cadence going
-- undetected.

with flows as (
    select merchant_name, amount, flow, posted_on
    from {{ ref('silver_transactions') }}
    where not is_transfer and merchant_name is not null
),

gaps as (
    select
        merchant_name,
        amount,
        flow,
        posted_on,
        posted_on - lag(posted_on) over (
            partition by merchant_name, amount order by posted_on
        ) as gap_days
    from flows
),

grouped as (
    select
        merchant_name,
        amount,
        flow,
        count(*) as occurrence_count,
        min(posted_on) as first_seen_on,
        max(posted_on) as last_seen_on,
        avg(gap_days) as avg_gap_days,
        stddev_pop(gap_days) as gap_days_stddev
    from gaps
    group by merchant_name, amount, flow
    having count(*) >= 3
),

cadenced as (
    select
        *,
        case
            when avg_gap_days between {{ var('recurring_weekly_gap_days')[0] }}
                and {{ var('recurring_weekly_gap_days')[1] }} then 'weekly'
            when avg_gap_days between {{ var('recurring_biweekly_gap_days')[0] }}
                and {{ var('recurring_biweekly_gap_days')[1] }} then 'biweekly'
            when avg_gap_days between {{ var('recurring_monthly_gap_days')[0] }}
                and {{ var('recurring_monthly_gap_days')[1] }} then 'monthly'
            when avg_gap_days between {{ var('recurring_quarterly_gap_days')[0] }}
                and {{ var('recurring_quarterly_gap_days')[1] }} then 'quarterly'
            when avg_gap_days between {{ var('recurring_yearly_gap_days')[0] }}
                and {{ var('recurring_yearly_gap_days')[1] }} then 'yearly'
        end as cadence
    from grouped
)

select
    md5(merchant_name || '|' || amount::text) as recurring_flow_id,
    merchant_name,
    flow,
    abs(amount) as amount,
    cadence,
    occurrence_count,
    first_seen_on,
    last_seen_on,
    avg_gap_days,
    gap_days_stddev
from cadenced
where cadence is not null
and gap_days_stddev <= avg_gap_days * {{ var('recurring_regularity_threshold') }}
