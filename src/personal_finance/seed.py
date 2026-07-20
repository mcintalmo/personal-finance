"""Seed the ``categories`` and ``rules`` tables from user config.

Category identity is the taxonomy path (see
:func:`personal_finance.user_config.category_id_for_path`), so seeding is an
idempotent upsert: re-running after a config edit inserts new categories and
updates descriptions in place, without duplicating rows.

Rows are never deleted here: a category removed from the taxonomy may still be
referenced by transactions, splits, budgets, or labels. Pruning orphaned
categories is a deliberate, separate operation for a later phase.

User-authored ``note`` values are never touched by seeding — notes belong to
the user, not to the config.

Rules are different: nothing else references a rule's id, and a rule carries
no user-editable state, so re-seeding fully replaces the table — removing or
reordering a rule in ``rules.yaml`` takes effect immediately.
"""

from typing import TYPE_CHECKING

from personal_finance.models import Rule
from personal_finance.user_config import (
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
