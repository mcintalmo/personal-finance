-- Recurring-expense detection: one row per (merchant_name, amount) pair whose
-- outflow charges repeat at a regular cadence (rent, subscriptions, ...).
-- Reads silver_transactions directly (not gold_line_items) — a subscription
-- charge is a whole-transaction concept tied to merchant_name, not something
-- that gets decomposed into splits, same rationale as gold_monthly_flow.
-- Excludes is_transfer, same convention as every other spend measure here.
--
-- Heuristic: group same-merchant, same-amount outflows, require at least 3
-- occurrences (2 gaps — one gap alone can't distinguish "regular" from
-- "coincidence"), bucket the average gap into a cadence, then keep only
-- groups where the gap is actually regular (stddev <= 25% of the average) —
-- a merchant charged the same amount twice at random is not "recurring".

with charges as (
    select merchant_name, amount, posted_on
    from {{ ref('silver_transactions') }}
    where not is_transfer and amount < 0 and merchant_name is not null
),

gaps as (
    select
        merchant_name,
        amount,
        posted_on,
        posted_on - lag(posted_on) over (
            partition by merchant_name, amount order by posted_on
        ) as gap_days
    from charges
),

grouped as (
    select
        merchant_name,
        amount,
        count(*) as occurrence_count,
        min(posted_on) as first_seen_on,
        max(posted_on) as last_seen_on,
        avg(gap_days) as avg_gap_days,
        stddev_pop(gap_days) as gap_days_stddev
    from gaps
    group by merchant_name, amount
    having count(*) >= 3
),

cadenced as (
    select
        *,
        case
            when avg_gap_days between 5 and 9 then 'weekly'
            when avg_gap_days between 25 and 35 then 'monthly'
            when avg_gap_days between 80 and 100 then 'quarterly'
            when avg_gap_days between 350 and 380 then 'yearly'
        end as cadence
    from grouped
)

select
    md5(merchant_name || amount::text) as recurring_expense_id,
    merchant_name,
    -amount as amount,
    cadence,
    occurrence_count,
    first_seen_on,
    last_seen_on,
    avg_gap_days,
    gap_days_stddev
from cadenced
where cadence is not null
and gap_days_stddev <= avg_gap_days * 0.25
