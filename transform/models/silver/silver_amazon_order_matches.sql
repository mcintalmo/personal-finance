-- Amazon order↔card-charge matching: card statements show one charge per
-- *shipment*, not per order or per line item (docs/source-schemas.md's
-- matching note) — silver_amazon_shipments already groups order-history rows
-- to that grain. A shipment and its card charge are two records of the same
-- real-world event, the same shape as silver_transfers' two legs of one
-- transfer, so matching is the same deterministic amount + date approach:
-- a shipment's `total_owed` must negate a transaction's `amount` (the charge
-- is an outflow), currencies must match, and the dates must fall within
-- `amazon_match_window_days` of each other (Amazon settles the charge around
-- when it ships, sometimes a day or two later).
--
-- 1:1 matching: keep only mutually-closest pairs, same ranking shape as
-- silver_transfers, so a shipment can't double-claim a charge (or vice versa)
-- when two candidates happen to tie on amount.

with shipments as (
    select * from {{ ref('silver_amazon_shipments') }}
),

transactions as (
    select * from {{ ref('silver_transactions') }}
    where flow = 'outflow'
),

candidates as (
    select
        s.website_order_id,
        s.ship_date,
        t.transaction_id,
        t.posted_on,
        t.account_name,
        s.total_owed,
        abs(s.ship_date - t.posted_on) as day_gap
    from shipments as s
    inner join transactions as t
        on t.amount = -s.total_owed
        and t.currency = s.currency
        and abs(s.ship_date - t.posted_on) <= {{ var('amazon_match_window_days', 5) }}
),

ranked as (
    select
        *,
        row_number() over (
            partition by website_order_id, ship_date
            order by day_gap, transaction_id
        ) as shipment_rank,
        row_number() over (
            partition by transaction_id
            order by day_gap, website_order_id, ship_date
        ) as transaction_rank
    from candidates
)

select
    md5(website_order_id || '|' || ship_date || '|' || transaction_id) as order_match_id,
    website_order_id,
    ship_date,
    transaction_id,
    posted_on,
    account_name,
    total_owed,
    day_gap
from ranked
where shipment_rank = 1 and transaction_rank = 1
