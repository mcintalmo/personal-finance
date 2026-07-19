-- Transfer detection: money moved between the user's own accounts (a credit-card
-- payment from checking, a Venmo cash-out to the bank, a checking→savings sweep)
-- shows up as two transactions — an outflow in one account and an inflow in
-- another — that should NOT count as spend or income.
--
-- A pair is a candidate when the amounts negate (equal magnitude, opposite
-- sign), the currencies match, the accounts differ, and the postings fall
-- within `transfer_window_days` of each other (ACH can settle a day or two
-- after it leaves).
--
-- We also look for a NAME corroboration: a real transfer leg often names the
-- other account in its descriptor ("VENMO CASHOUT" landing in checking names
-- the Venmo account it came from). When a leg's description contains a
-- significant token of the counterparty account, `name_match` is true and the
-- pair is `high` confidence. This both raises confidence and breaks ties in the
-- 1:1 matching so a corroborated pair wins over a coincidental same-amount one.
-- It is a bonus signal, not a requirement — transfers with no descriptive hint
-- still match on amount + date (`medium` confidence).

with tx as (
    select * from {{ ref('stg_transactions') }}
),

-- A regex of each account's significant name tokens (drop generic banking words
-- and short/ambiguous tokens like "ONE" from "Capital One"). Empty when a name
-- has no distinctive token.
account_patterns as (
    select
        account_name,
        array_to_string(
            list_transform(
                list_filter(
                    string_split(upper(account_name), ' '),
                    x -> length(x) >= 4
                    and x not in ('CHECKING', 'SAVINGS', 'ACCOUNT', 'CARD', 'CREDIT', 'DEBIT', 'BANK')
                ),
                -- account_name is free-text from user config, not controlled —
                -- escape before it's spliced into a regex (a name like
                -- "401(k) Rollover" would otherwise break/misparse the pattern).
                x -> regexp_escape(x)
            ),
            '|'
        ) as name_pattern
    from (select distinct account_name from tx)
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
        abs(o.posted_on - i.posted_on) as day_gap,
        -- The inflow names the from-account, or the outflow names the to-account.
        coalesce(
            from_pat.name_pattern <> ''
            and regexp_matches(upper(coalesce(i.description_raw, '')), '\b(' || from_pat.name_pattern || ')\b'),
            false
        )
        or coalesce(
            to_pat.name_pattern <> ''
            and regexp_matches(upper(coalesce(o.description_raw, '')), '\b(' || to_pat.name_pattern || ')\b'),
            false
        ) as name_match
    from outflows as o
    inner join inflows as i
        on i.amount = -o.amount
        and i.currency = o.currency
        and i.account_name <> o.account_name
        and abs(o.posted_on - i.posted_on) <= {{ var('transfer_window_days', 3) }}
    left join account_patterns as from_pat on from_pat.account_name = o.account_name
    left join account_patterns as to_pat on to_pat.account_name = i.account_name
),

-- 1:1 matching: keep only mutually-best pairs. A name-corroborated candidate
-- outranks a coincidental one at the same date distance, so it wins the leg.
ranked as (
    select
        *,
        row_number() over (
            partition by outflow_id
            order by name_match desc, day_gap, inflow_id
        ) as out_rank,
        row_number() over (
            partition by inflow_id
            order by name_match desc, day_gap, outflow_id
        ) as in_rank
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
    day_gap,
    name_match,
    case when name_match then 'high' else 'medium' end as confidence
from ranked
where out_rank = 1 and in_rank = 1
