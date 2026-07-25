{% macro read_parquet_or_empty(glob_pattern, empty_columns) %}
{#-
  read_parquet() throws immediately — even just to CREATE VIEW, since the
  view's schema must be resolved eagerly — when a glob matches zero files.
  Fine for bronze.transactions (every build ingests at least one bank
  export before running `pf transform`) but not for an optional source like
  Amazon order-history, which most builds never ingest at all. Checks file
  existence at compile time via glob() (itself tolerant of zero matches,
  unlike read_parquet) and falls back to a correctly-typed empty relation
  instead of failing the whole build — the same "resolves to zero rows when
  its upstream hasn't run yet" contract every other cascade stage in this
  project already follows.

  `empty_columns` is a list of [name, type] pairs for the empty-relation
  fallback's schema — Jinja can't infer parquet column types, so this must
  be kept in sync with the real source's columns.
-#}
{%- set file_count_query -%}
  select count(*) as n from glob('{{ glob_pattern }}')
{%- endset -%}
{%- if execute -%}
  {%- set file_count = run_query(file_count_query).columns[0].values()[0] -%}
{%- else -%}
  {%- set file_count = 0 -%}
{%- endif -%}
{%- if file_count > 0 -%}
read_parquet('{{ glob_pattern }}', union_by_name = true, filename = true)
{%- else -%}
(select {% for name, type in empty_columns %}cast(null as {{ type }}) as {{ name }}{% if not loop.last %}, {% endif %}{% endfor %} where false)
{%- endif -%}
{% endmacro %}
