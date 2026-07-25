-- Cleaned Amazon order-history line items (staging). Ephemeral like
-- stg_transactions — inlined into silver_amazon_shipments, no extra
-- warehouse object.
--
-- Bronze is the raw Parquet landing (dlt, personal_finance.ingest.amazon_source).
-- Cleaning done here: dedup on row_hash (bronze append is already idempotent,
-- so this is defensive, same as stg_transactions); decimal typing for the
-- money columns.

{{ config(materialized='ephemeral') }}

with bronze as (
    -- Deliberately not the source() macro: dbt's sources.yml meta.external_location
    -- renders without custom project macros in scope (only dbt's own context
    -- globals, e.g. env_var), so the empty-glob-safe read has to be called
    -- directly here instead — see sources.yml's amazon_order_items entry and
    -- read_parquet_or_empty.sql for why a plain read_parquet() isn't safe:
    -- most builds never ingest an Amazon file, and read_parquet() throws
    -- immediately (even just to CREATE VIEW) on a non-matching glob.
    select * from {{ read_parquet_or_empty(
        env_var('DATA_BRONZE_PATH', 'data/bronze') ~ '/bronze_amazon/amazon/*.parquet',
        [
            ['row_hash', 'varchar'], ['ingested_at', 'timestamptz'],
            ['website_order_id', 'varchar'], ['order_date', 'date'], ['ship_date', 'date'],
            ['asin', 'varchar'], ['product_name', 'varchar'], ['quantity', 'bigint'],
            ['unit_price', 'decimal(18,2)'], ['unit_price_tax', 'decimal(18,2)'],
            ['shipping_charge', 'decimal(18,2)'], ['total_discounts', 'decimal(18,2)'],
            ['shipment_item_subtotal', 'decimal(18,2)'],
            ['shipment_item_subtotal_tax', 'decimal(18,2)'], ['total_owed', 'decimal(18,2)'],
            ['currency', 'varchar'], ['order_status', 'varchar'], ['shipment_status', 'varchar']
        ]
    ) }}
),

deduped as (
    select *
    from bronze
    qualify row_number() over (partition by row_hash order by ingested_at desc) = 1
)

select
    row_hash as order_item_id,
    website_order_id,
    order_date,
    ship_date,
    asin,
    product_name,
    quantity,
    cast(unit_price as decimal(18, 2)) as unit_price,
    cast(unit_price_tax as decimal(18, 2)) as unit_price_tax,
    cast(shipping_charge as decimal(18, 2)) as shipping_charge,
    cast(total_discounts as decimal(18, 2)) as total_discounts,
    cast(shipment_item_subtotal as decimal(18, 2)) as shipment_item_subtotal,
    cast(shipment_item_subtotal_tax as decimal(18, 2)) as shipment_item_subtotal_tax,
    cast(total_owed as decimal(18, 2)) as total_owed,
    upper(currency) as currency,
    order_status,
    shipment_status
from deduped
