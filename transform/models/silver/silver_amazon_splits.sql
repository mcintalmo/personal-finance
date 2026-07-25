-- Transaction decomposition into splits, keyed off matched Amazon order line
-- items (see docs/ARCHITECTURE.md's transaction_splits concept: a receipt or
-- order decomposes one transaction into N splits so "spend on apples this
-- year" is answerable). Only matched shipments decompose here — an
-- unmatched Amazon shipment has no charge to attach its items to yet, and a
-- transaction's "implicit single split" for everything else is a gold-layer
-- concern (unioning every split source plus unsplit transactions), not built
-- yet.
--
-- Line items don't naturally sum to the exact charge amount: Amazon rounds
-- each item's price/tax independently, and the charge also folds in
-- shipping/discounts (see assert_amazon_shipment_totals_reconcile). To make
-- splits a true decomposition (sum of splits == transaction amount, to the
-- cent — the invariant that makes rollups exact), each item's share of the
-- charge is allocated proportionally to its (subtotal + tax), and the last
-- item (by order_item_id, a stable tiebreak) absorbs whatever rounding
-- remainder is left so the sum lands exactly on the charge amount.

with matches as (
    select website_order_id, ship_date, transaction_id
    from {{ ref('silver_amazon_order_matches') }}
),

items as (
    select
        i.order_item_id,
        i.asin,
        i.product_name,
        i.quantity,
        i.unit_price,
        i.currency,
        i.shipment_item_subtotal + i.shipment_item_subtotal_tax as item_total,
        m.transaction_id
    from {{ ref('stg_amazon_order_items') }} as i
    inner join matches as m
        on m.website_order_id = i.website_order_id
        and m.ship_date = i.ship_date
),

charges as (
    select transaction_id, amount as charge_amount
    from {{ ref('silver_transactions') }}
),

allocated as (
    select
        items.*,
        charges.charge_amount,
        sum(items.item_total) over (partition by items.transaction_id) as shipment_total,
        count(*) over (partition by items.transaction_id) as item_count,
        row_number() over (
            partition by items.transaction_id order by items.order_item_id
        ) as item_rank
    from items
    inner join charges using (transaction_id)
),

proportional as (
    select
        *,
        case
            when shipment_total = 0 then charge_amount / item_count
            else round(charge_amount * item_total::double / shipment_total::double, 2)
        end as proportional_amount
    from allocated
),

with_remainder as (
    select
        *,
        sum(proportional_amount) over (partition by transaction_id) as allocated_sum
    from proportional
)

select
    order_item_id as split_id,
    transaction_id,
    asin,
    product_name,
    quantity,
    unit_price,
    upper(currency) as currency,
    cast(
        case
            when item_rank = item_count then proportional_amount + (charge_amount - allocated_sum)
            else proportional_amount
        end as decimal(18, 2)
    ) as amount
from with_remainder
