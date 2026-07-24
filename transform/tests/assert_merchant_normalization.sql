-- Unit test for the normalize_merchant macro. Each case pairs a raw descriptor
-- with the merchant it must clean to (including processor-prefix shapes the
-- synth fixtures don't emit). The test fails if any case doesn't match: dbt
-- flags a singular test that returns rows.
--
-- The bare-city case is conditional on the current build's known_cities var
-- actually including "Bellevue" (config/examples/places.yaml does; a
-- config-free build's default empty list does not) — this test runs under
-- many different build contexts, so it can't hardcode a case that only holds
-- for one of them.

with cases as (
    select * from (values
        ('STARBUCKS #9985', 'STARBUCKS'),
        ('ALDI 73011', 'ALDI'),
        ('CHEVRON 0093 BELLEVUE WA', 'CHEVRON'),
        ('SAFEWAY STORE 1442', 'SAFEWAY'),
        ('TRADER JOE''S #0552 SEATTLE WA', 'TRADER JOE''S'),
        ('NETFLIX.COM', 'NETFLIX'),
        ('SPOTIFY USA', 'SPOTIFY'),
        ('VENMO CASHOUT PPD ID: 5264681992', 'VENMO CASHOUT'),
        ('SQ *BLUE BOTTLE COFFEE', 'BLUE BOTTLE COFFEE'),
        ('PP*GRUBHUB', 'GRUBHUB'),
        ('PAYPAL *STEAM GAMES', 'STEAM GAMES'),
        ('TST* THE PINK DOOR', 'THE PINK DOOR'),
        ('KROGER #718', 'KROGER'),
        ('CHASE CREDIT CRD AUTOPAY', 'CHASE CREDIT CRD AUTOPAY')
        {%- if 'bellevue' in (var('known_cities', []) | map('lower') | list) %}
        , ('THAI GINGER BELLEVUE', 'THAI GINGER')
        {%- endif %}
    ) as t(raw, expected)
)

select
    raw,
    expected,
    {{ normalize_merchant('raw') }} as actual
from cases
where {{ normalize_merchant('raw') }} is distinct from expected
