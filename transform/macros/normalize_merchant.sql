{% macro normalize_merchant(column) %}
{#-
  Deterministic merchant-name cleanup for a raw bank/card descriptor. Each
  regexp_replace sees the output of the previous, so ORDER MATTERS. Handles the
  common noise seen across sources (see docs/source-schemas.md):

    1. upper-case                          normalize to a case-insensitive key
    2. reference tail   "... PPD ID: 123"  ACH/Venmo trailing reference
    3. trailing rail token "... PPD"       leftover ACH rail label
    4. processor prefix "SQ *", "PP*"      Square / PayPal / Amazon marketplace
    5. domain suffix    "NETFLIX.COM"      drop the TLD
    6. store / ref no.  "#9985", "1442"    location / reference numbers
    7. "STORE" label    "SAFEWAY STORE 1"  generic store word
    8. trailing CITY ST "... BELLEVUE WA"  locality when anchored by a state
    9. trailing state / "USA"              lone locality token
   10. known bare city  "... BELLEVUE"     locality with no state to anchor on
       (config-driven — see places.yaml / `known_cities` dbt var; skipped
       entirely when empty, same as an unconfigured cascade stage)
   11. collapse whitespace + trim

  Produces an UPPERCASE normalized key. Per-merchant brand-variant aliases are
  a separate, config-driven step (merchants.yaml — see
  transform/models/silver/silver_transactions.sql); this macro only does
  high-confidence, generic cleanup.
-#}
{%- set states -%}
AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY
{%- endset -%}
{%- set known_cities = var('known_cities', []) -%}
{#- known_cities is free-text from the user's places.yaml, not controlled —
   escape with DuckDB's own regexp_escape (same precedent as
   silver_transfers.sql's account_name handling) rather than a hand-maintained
   list of metacharacters, so every RE2 special character is covered. -#}
{%- set known_cities_sql_items = known_cities | map('replace', "'", "''") | join("', '") -%}
{%- set cities_pattern_expr -%}
array_to_string(list_transform(list_value('{{ known_cities_sql_items }}'), x -> regexp_escape(upper(x))), '|')
{%- endset -%}
trim(regexp_replace(
  {%- if known_cities %}
  regexp_replace(
  {%- endif %}
    regexp_replace(
      regexp_replace(
        regexp_replace(
          regexp_replace(
            regexp_replace(
              regexp_replace(
                regexp_replace(
                  regexp_replace(
                    upper({{ column }}),
                    '\s+(PPD\s+|CO\s+|WEB\s+|TEL\s+|ARC\s+)?ID:.*$', ''
                  ),
                  '\s+(PPD|CCD|WEB|TEL|ARC|POS|PMT)$', ''
                ),
                '^(SQ|TST|PP|PY|PAYPAL|GOOGLE|AMZN MKTP US|AMZN MKTP)\s*\*+\s*', ''
              ),
              '\.(COM|NET|ORG|IO|CO|US)\b', '', 'g'
            ),
            '#?\s*\d{2,}', '', 'g'
          ),
          '\bSTORE\b', '', 'g'
        ),
        '\s+[A-Z][A-Z''&.-]*\s+({{ states }})$', ''
      ),
      '\s+(USA|US|{{ states }})$', ''
    )
  {%- if known_cities %}
  , '\s+(' || ({{ cities_pattern_expr }}) || ')$', ''
  )
  {%- endif -%}
  ,
  '\s+', ' ', 'g'
))
{% endmacro %}
