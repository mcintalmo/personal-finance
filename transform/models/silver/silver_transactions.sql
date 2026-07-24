-- Public cleaned transaction grain: the staged/cleaned transactions, with
-- merchant_name resolved through config-driven aliases (merchants.yaml) on
-- top of the generic normalize_merchant cleanup, plus an `is_transfer` flag
-- marking legs of a detected inter-account transfer (see silver_transfers).
-- Spend/income measures should exclude is_transfer rows so moving money
-- between your own accounts doesn't look like activity.

with base as (
    select * from {{ ref('stg_transactions') }}
),

aliases as (
    select pattern, canonical_name, priority
    from {{ source('app', 'merchant_aliases') }}
),

-- First-match-wins by priority (file order), same macro as stage 1 of the
-- categorization cascade (silver_transaction_categories.sql) — one row per
-- transaction whose merchant_name matched some alias pattern.
matched_aliases as (
    select
        base.transaction_id,
        aliases.canonical_name
    from base
    inner join aliases
        on base.merchant_name is not null
        and regexp_matches(base.merchant_name, aliases.pattern)
    qualify {{ first_match_wins('base.transaction_id', 'aliases.priority') }}
),

transfer_legs as (
    select outflow_id as transaction_id from {{ ref('silver_transfers') }}
    union
    select inflow_id as transaction_id from {{ ref('silver_transfers') }}
)

select
    base.* exclude (merchant_name),
    coalesce(matched_aliases.canonical_name, base.merchant_name) as merchant_name,
    legs.transaction_id is not null as is_transfer
from base
left join matched_aliases using (transaction_id)
left join transfer_legs as legs using (transaction_id)
