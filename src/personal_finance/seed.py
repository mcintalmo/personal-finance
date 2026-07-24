"""Seed the ``categories``, ``rules``, and ``merchant_aliases`` tables from user config.

Category identity is the taxonomy path (see
:func:`personal_finance.user_config.category_id_for_path`), so seeding is an
idempotent upsert: re-running after a config edit inserts new categories and
updates descriptions in place, without duplicating rows.

Rows are never deleted here: a category removed from the taxonomy may still be
referenced by transactions, splits, budgets, or labels. Pruning orphaned
categories is a deliberate, separate operation for a later phase.

User-authored ``note`` values are never touched by seeding — notes belong to
the user, not to the config.

Rules and merchant aliases are different: nothing else references their ids,
and neither carries user-editable state, so re-seeding fully replaces each
table — removing or reordering an entry in ``rules.yaml``/``merchants.yaml``
takes effect immediately.
"""

from typing import TYPE_CHECKING

from personal_finance.models import MerchantAlias, Rule
from personal_finance.user_config import (
    MerchantAliasConfig,
    RuleConfig,
    TaxonomyNode,
    category_id_for_path,
    taxonomy_to_categories,
)

if TYPE_CHECKING:
    import duckdb

    from personal_finance.models import Category

_UPSERT_CATEGORY = """
INSERT INTO categories (id, created_at, name, parent_id, description, note)
VALUES ($id, $created_at, $name, $parent_id, $description, $note)
ON CONFLICT (id) DO UPDATE SET
    name = excluded.name,
    parent_id = excluded.parent_id,
    description = excluded.description
"""


def seed_categories(
    conn: duckdb.DuckDBPyConnection, nodes: list[TaxonomyNode]
) -> dict[str, Category]:
    """Upsert the taxonomy into the ``categories`` table (idempotent).

    Args:
        conn: An open DuckDB connection with the core schema created.
        nodes: The taxonomy roots, e.g. ``load_user_config().taxonomy``.

    Returns:
        The seeded categories keyed by taxonomy path.
    """
    categories = taxonomy_to_categories(nodes)
    # Insertion order is depth-first from the walk, so parents precede children
    # and the self-referential foreign key is always satisfiable.
    for category in categories.values():
        conn.execute(_UPSERT_CATEGORY, category.model_dump())
    return categories


_INSERT_RULE = """
INSERT INTO rules (id, created_at, pattern, applies_to, category_id, priority, note)
VALUES ($id, $created_at, $pattern, $applies_to, $category_id, $priority, $note)
"""


def seed_rules(conn: duckdb.DuckDBPyConnection, rules: list[RuleConfig]) -> list[Rule]:
    """Replace the ``rules`` table with the current ``rules.yaml`` config.

    Args:
        conn: An open DuckDB connection with the core schema created.
        rules: The rule list, e.g. ``load_user_config().rules`` — already
            validated (compiling pattern, existing category path) by
            :class:`~personal_finance.user_config.RuleConfig`.

    Returns:
        The seeded rules, in priority order (first match wins).
    """
    conn.execute("DELETE FROM rules")
    seeded = [
        Rule(
            pattern=rule.pattern,
            applies_to=rule.applies_to.value,
            category_id=category_id_for_path(rule.category),
            priority=priority,
        )
        for priority, rule in enumerate(rules)
    ]
    for rule in seeded:
        conn.execute(_INSERT_RULE, rule.model_dump())
    return seeded


_INSERT_MERCHANT_ALIAS = """
INSERT INTO merchant_aliases (id, created_at, pattern, canonical_name, priority, note)
VALUES ($id, $created_at, $pattern, $canonical_name, $priority, $note)
"""


def seed_merchant_aliases(
    conn: duckdb.DuckDBPyConnection, aliases: list[MerchantAliasConfig]
) -> list[MerchantAlias]:
    """Replace the ``merchant_aliases`` table with the current ``merchants.yaml`` config.

    Same full-replace contract as :func:`seed_rules`: nothing else references
    an alias's id and it carries no user-editable state, so re-seeding fully
    replaces the table.

    Args:
        conn: An open DuckDB connection with the core schema created.
        aliases: The alias list, e.g. ``load_user_config().merchant_aliases``.

    Returns:
        The seeded aliases, in priority order (first match wins).
    """
    conn.execute("DELETE FROM merchant_aliases")
    seeded = [
        MerchantAlias(
            pattern=alias.pattern,
            canonical_name=alias.canonical_name,
            priority=priority,
        )
        for priority, alias in enumerate(aliases)
    ]
    for alias in seeded:
        conn.execute(_INSERT_MERCHANT_ALIAS, alias.model_dump())
    return seeded
