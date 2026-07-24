-- One row per shipment — matching card-charge granularity, not per order or
-- per item (see docs/source-schemas.md's matching note): a multi-item order
-- that ships in two boxes produces two separate card charges, one per
-- shipment. `total_owed`/`shipping_charge`/`total_discounts` are
-- shipment-level values Amazon repeats identically on every item row of that
-- shipment (see stg_amazon_order_items) — taken once per shipment via
-- any_value, never summed across items (summing would multiply a
-- shipment-wide total by its item count).
--
-- Order↔charge matching against silver_transactions (amount + date window,
-- the same shape as silver_transfers' deterministic matching) and split
-- decomposition are later Phase 5 stages, not built yet — this model is the
-- ingestion-side artifact they'll read from.

with items as (
    select * from {{ ref('stg_amazon_order_items') }}
)

select
    website_order_id,
    ship_date,
    min(order_date) as order_date,
    any_value(total_owed) as total_owed,
    any_value(shipping_charge) as shipping_charge,
    any_value(total_discounts) as total_discounts,
    sum(shipment_item_subtotal) as items_subtotal,
    sum(shipment_item_subtotal_tax) as items_subtotal_tax,
    count(*) as item_count,
    any_value(currency) as currency,
    any_value(order_status) as order_status,
    any_value(shipment_status) as shipment_status
from items
group by website_order_id, ship_date
