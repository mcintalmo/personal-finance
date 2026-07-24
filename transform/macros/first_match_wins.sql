{% macro first_match_wins(partition_by, order_by) %}
{#-
  "First match wins by priority" — shared by every regex-cascade stage that
  picks a single winning candidate per grain key (lowest `order_by` value,
  i.e. file order): silver_transaction_categories.sql (rules) and
  silver_transactions.sql (merchant_aliases). Use as a `qualify` predicate:

    qualify {{ first_match_wins('transaction_id', 'priority') }}
-#}
row_number() over (partition by {{ partition_by }} order by {{ order_by }}) = 1
{%- endmacro %}
