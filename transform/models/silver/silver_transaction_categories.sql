-- Stage 1 of the categorization cascade: deterministic pattern → category
-- rules (config/rules.yaml, seeded into the `rules` table — see
-- personal_finance.seed.seed_rules). First matching rule wins per transaction
-- (rules are tried in `priority` order, i.e. file order).
--
-- Grain: at most one row per transaction_id — only transactions a rule
-- actually matched. A transaction absent here is uncategorized by this stage;
-- later stages (embedding classifier, LLM fallback — see TODO.md) are expected
-- to pick up exactly the transactions missing from this table, each unioning
-- in its own categorization_source/confidence.
--
-- `applies_to` (on each rule) selects which transaction field the pattern
-- runs against — merchant_name (normalize_merchant's cleaned key) is the
-- recommended default since it's far less noisy than the raw descriptor.
--
-- candidates is built as one UNION ALL branch per applies_to value (rather
-- than a single CASE picking one of four columns) deliberately: the CASE form
-- segfaulted DuckDB 1.5.4 (SIGSEGV, exit 139 — a real engine crash, not a
-- catchable error) when a value contains a multi-byte character (an emoji in
-- a Venmo note) flowed through regexp_matches inside this cross join. Each
-- branch below selects only its own real column, never merges differently-
-- sourced text through a CASE, and was stress-tested crash-free across
-- repeated real `dbt build` runs with the same emoji-containing fixture.
-- Revisit if a DuckDB upgrade fixes the underlying engine bug.

with tx as (
    select
        transaction_id,
        description_raw,
        merchant_name,
        source,
        account_name
    from {{ ref('silver_transactions') }}
),

rules as (
    select * from {{ source('app', 'rules') }}
),

-- applies_to is validated (Python RuleConfig) against a fixed enum matching
-- these four branches, so the union is exhaustive by construction — see
-- personal_finance.user_config.RuleApplyField.
candidates as (
    select t.transaction_id, r.id as rule_id, r.category_id, r.pattern, r.priority,
        t.description_raw as matched_field
    from tx as t inner join rules as r on r.applies_to = 'description_raw'

    union all

    select t.transaction_id, r.id as rule_id, r.category_id, r.pattern, r.priority,
        t.merchant_name as matched_field
    from tx as t inner join rules as r on r.applies_to = 'merchant_name'

    union all

    select t.transaction_id, r.id as rule_id, r.category_id, r.pattern, r.priority,
        t.source as matched_field
    from tx as t inner join rules as r on r.applies_to = 'source'

    union all

    select t.transaction_id, r.id as rule_id, r.category_id, r.pattern, r.priority,
        t.account_name as matched_field
    from tx as t inner join rules as r on r.applies_to = 'account_name'
),

-- First match wins: lowest priority (file order) per transaction — shared
-- with silver_transactions.sql's merchant_aliases resolution via this macro.
matched as (
    select *
    from candidates
    where matched_field is not null
    and regexp_matches(matched_field, pattern)
    qualify {{ first_match_wins('transaction_id', 'priority') }}
)

select
    transaction_id,
    category_id,
    rule_id,
    pattern as matched_pattern,
    'rule' as categorization_source,
    1.0 as categorization_confidence
from matched
