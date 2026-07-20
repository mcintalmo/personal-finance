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
   10. collapse whitespace + trim

  Produces an UPPERCASE normalized key. City-only suffixes (no state to anchor
  on) and per-merchant aliases are intentionally left to the config-driven
  step (see TODO.md) — this macro only does high-confidence, generic cleanup.
-#}
{%- set states -%}
AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY
{%- endset -%}
trim(regexp_replace(
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
  ),
  '\s+', ' ', 'g'
))
{% endmacro %}
