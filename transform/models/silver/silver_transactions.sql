-- Public cleaned transaction grain: the staged/cleaned transactions, with
-- merchant_name resolved through config-driven aliases (merchants.yaml) and
-- then human-confirmed embedding-similarity merges (merchant_merges — see
-- personal_finance.merchant_merge) on top of the generic normalize_merchant
-- cleanup, plus an `is_transfer` flag marking legs of a detected
-- inter-account transfer (see silver_transfers). Spend/income measures
-- should exclude is_transfer rows so moving money between your own accounts
-- doesn't look like activity.

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

aliased as (
    select
        base.transaction_id,
        coalesce(matched_aliases.canonical_name, base.merchant_name) as merchant_name
    from base
    left join matched_aliases using (transaction_id)
),

-- Human-confirmed merges (pf review merge-candidates / merge), keyed by
-- merchant_name (exact match, not regex) — latest decision wins if a
-- merchant_name was reviewed more than once. Single-hop only: a merge
-- target that is itself later merged elsewhere is not chased further.
merges as (
    select merchant_name, canonical_name
    from (
        select
            *,
            row_number() over (
                partition by merchant_name order by created_at desc
            ) as rnk
        from {{ source('app', 'merchant_merges') }}
        where status = 'accepted'
    )
    where rnk = 1
),

resolved as (
    select
        aliased.transaction_id,
        coalesce(merges.canonical_name, aliased.merchant_name) as merchant_name
    from aliased
    left join merges on merges.merchant_name = aliased.merchant_name
),

transfer_legs as (
    select outflow_id as transaction_id from {{ ref('silver_transfers') }}
    union
    select inflow_id as transaction_id from {{ ref('silver_transfers') }}
)

select
    base.* exclude (merchant_name),
    resolved.merchant_name,
    legs.transaction_id is not null as is_transfer
from base
left join resolved using (transaction_id)
left join transfer_legs as legs using (transaction_id)
