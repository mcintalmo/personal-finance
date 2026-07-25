-- Every shipment's item subtotal + tax + shipping - discounts must not
-- exceed its shipment-level total_owed (equality up to rounding; strictly
-- less would mean an item lost part of its price during aggregation). Fails
-- (returns a row) if silver_amazon_shipments.sql ever sums a shipment-level
-- column (total_owed/shipping_charge/total_discounts) across item rows
-- instead of taking it once via any_value — the bug this model's docstring
-- explicitly warns against, since a shipment-level value is repeated
-- identically on every item row upstream.

select *
from {{ ref('silver_amazon_shipments') }}
where items_subtotal + items_subtotal_tax + shipping_charge - total_discounts > total_owed + 0.01
