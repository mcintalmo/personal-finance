-- Transfer detection: money moved between the user's own accounts (a credit-card
-- payment from checking, a Venmo cash-out to the bank, a checking→savings sweep)
-- shows up as two transactions — an outflow in one account and an inflow in
-- another — that should NOT count as spend or income.
--
-- A pair is a candidate when the amounts negate (equal magnitude, opposite
-- sign), the currencies match, the accounts differ, and the postings fall
-- within `transfer_window_days` of each other (ACH can settle a day or two
-- after it leaves). Candidates are matched 1:1 by keeping only mutually-best
-- pairs (each leg is the other's closest match), so a repeated amount can't
-- double-count. Ambiguous many-way matches are left for a later, stronger
-- matcher (see TODO.md).

with tx as (
    select * from {{ ref('stg_transactions') }}
),

outflows as (select * from tx where amount < 0),
inflows as (select * from tx where amount > 0),

candidates as (
    select
        o.transaction_id as outflow_id,
        i.transaction_id as inflow_id,
        o.account_name as from_account,
        i.account_name as to_account,
        o.currency as currency,
        -o.amount as amount,
        o.posted_on as sent_on,
        i.posted_on as received_on,
        abs(o.posted_on - i.posted_on) as day_gap
    from outflows as o
    inner join inflows as i
        on i.amount = -o.amount
        and i.currency = o.currency
        and i.account_name <> o.account_name
        and abs(o.posted_on - i.posted_on) <= {{ var('transfer_window_days', 3) }}
),

ranked as (
    select
        *,
        row_number() over (partition by outflow_id order by day_gap, inflow_id) as out_rank,
        row_number() over (partition by inflow_id order by day_gap, outflow_id) as in_rank
    from candidates
)

select
    md5(outflow_id || '|' || inflow_id) as transfer_id,
    outflow_id,
    inflow_id,
    from_account,
    to_account,
    currency,
    amount,
    sent_on,
    received_on,
    day_gap
from ranked
where out_rank = 1 and in_rank = 1
