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
--
-- The proportional allocation itself is done in integer cents, not
-- DECIMAL/DOUBLE division: `charge * item_total / shipment_total` can land
-- exactly on a half-cent boundary (e.g. 126.945), and neither DOUBLE nor
-- DuckDB's DECIMAL division (which falls back to DOUBLE internally when a
-- quotient isn't exactly representable at the target scale) reliably
-- round-trips that boundary — the binary approximation can come out a hair
-- below the true value, rounding a non-last item's split the wrong way by a
-- cent with nothing catching it (the remainder step still forces the
-- *total* to reconcile exactly, so assert_amazon_splits_sum_to_transaction_amount
-- passes even when a per-item allocation is off). `//` (BIGINT floor
-- division) has no such failure mode: every intermediate value is an exact
-- integer, so `(numerator + divisor // 2) // divisor` is a provably correct
-- round-half-up on the true rational ratio, not an approximation of it. The
-- final `/ 100` back to a decimal dollar amount is safe precisely because,
-- unlike the ratio above, it's dividing an exact integer number of cents by
-- a fixed power of ten — no realistic amount comes close to exhausting a
-- double's 52-bit mantissa doing that.

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

cents as (
    select
        *,
        -- amount/item_total/shipment_total are decimal(18,2) — already exact
        -- to the cent, so *100 and CAST to BIGINT is an exact conversion,
        -- never a DOUBLE approximation of one.
        sign(charge_amount) as charge_sign,
        cast(round(abs(charge_amount) * 100) as bigint) as charge_cents,
        cast(round(item_total * 100) as bigint) as item_cents,
        cast(round(shipment_total * 100) as bigint) as shipment_cents
    from allocated
),

proportional as (
    select
        *,
        -- shipment_cents = 0 (every item in the shipment free/promotional)
        -- with a nonzero charge — e.g. a $0 item whose shipment's total_owed
        -- is entirely shipping_charge — falls back to an equal split instead
        -- of dividing by zero. Not reachable by the synth fixture (its items
        -- always have a nonzero subtotal), so this branch has no dedicated
        -- test; the remainder step below still keeps the total exact either way.
        case
            when shipment_cents = 0 then charge_cents // item_count
            else (charge_cents * item_cents + shipment_cents // 2) // shipment_cents
        end as proportional_cents
    from cents
),

with_remainder as (
    select
        *,
        sum(proportional_cents) over (partition by transaction_id) as allocated_cents
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
    -- DuckDB's `/` always yields DOUBLE regardless of operand types, so the
    -- division back to dollars must itself be cast to decimal(18,2) — not
    -- just its integer-cents input — or the column comes back as DOUBLE.
    cast(
        cast(
            charge_sign * (
                case
                    when item_rank = item_count
                        then proportional_cents + (charge_cents - allocated_cents)
                    else proportional_cents
                end
            ) as decimal(18, 2)
        ) / 100 as decimal(18, 2)
    ) as amount
from with_remainder
