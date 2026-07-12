"""User-editable domain configuration loaded from YAML files.

Four files in the config directory (``Settings.config_dir``, default ``config/``)
drive the pipeline without code changes:

    sources.yaml    data sources to ingest (custom names, column mappings)
    taxonomy.yaml   the hierarchical category tree (apples → groceries → essentials)
    rules.yaml      deterministic merchant/pattern → category rules
    budgets.yaml    budget buckets over category subtrees

Categories are referenced across files by slash-separated path from the taxonomy
root, e.g. ``essentials/groceries/apples``. Referential integrity is validated at
load time: a rule or budget naming an unknown category path fails immediately.

Missing files are treated as empty (all sections are optional); malformed files
raise :class:`~personal_finance.exceptions.ConfigurationError`.

Live files in ``config/`` are gitignored — they may describe real accounts and
finances. Committed dummy templates live in ``config/examples/``; copy them in
with ``cp config/examples/*.yaml config/`` (see ``config/README.md``).
"""

import re
from decimal import Decimal
from enum import StrEnum
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from personal_finance.config import get_settings
from personal_finance.exceptions import ConfigurationError
from personal_finance.models import AccountType, BudgetPeriod, Category

CATEGORY_PATH_SEPARATOR = "/"

_CONFIG_FILES: dict[str, str] = {
    "sources": "sources.yaml",
    "taxonomy": "taxonomy.yaml",
    "rules": "rules.yaml",
    "budgets": "budgets.yaml",
}


class SourceKind(StrEnum):
    CSV = "csv"
    OFX = "ofx"


class _ConfigModel(BaseModel):
    """Base for config models: unknown keys are typos and must fail loudly."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SourceConfig(_ConfigModel):
    """One data source to ingest, e.g. a bank's CSV export format."""

    name: str = Field(min_length=1)  # stable identifier, used in provenance
    kind: SourceKind
    account_name: str = Field(min_length=1)  # display name of the backing account
    account_type: AccountType
    currency: str = "USD"
    column_map: dict[str, str] = Field(default_factory=dict)  # model field -> source column
    date_format: str | None = None  # strptime format for CSV date columns


class TaxonomyNode(_ConfigModel):
    """A category in the hierarchy; nesting defines parent/child relationships."""

    name: str = Field(min_length=1)
    description: str | None = None
    children: list["TaxonomyNode"] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_has_no_separator(cls, value: str) -> str:
        if CATEGORY_PATH_SEPARATOR in value:
            msg = f"category name {value!r} must not contain {CATEGORY_PATH_SEPARATOR!r}"
            raise ValueError(msg)
        return value


class RuleConfig(_ConfigModel):
    """A deterministic categorization rule: regex match → category path."""

    pattern: str = Field(min_length=1)  # regular expression, matched case-sensitively
    category: str  # slash-separated taxonomy path
    applies_to: str = "description_raw"  # transaction field the pattern runs against

    @field_validator("pattern")
    @classmethod
    def _pattern_compiles(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"invalid regular expression {value!r}: {exc}"
            raise ValueError(msg) from exc
        return value


class BudgetConfig(_ConfigModel):
    """A budget bucket over a category subtree."""

    name: str = Field(min_length=1)
    category: str  # slash-separated taxonomy path
    period: BudgetPeriod
    amount: Decimal = Field(gt=0)


class UserConfig(_ConfigModel):
    """The full user configuration, cross-validated for referential integrity."""

    sources: list[SourceConfig] = Field(default_factory=list)
    taxonomy: list[TaxonomyNode] = Field(default_factory=list)
    rules: list[RuleConfig] = Field(default_factory=list)
    budgets: list[BudgetConfig] = Field(default_factory=list)

    def category_paths(self) -> set[str]:
        """Return every category path defined by the taxonomy."""
        return {path for path, _, _ in _walk_taxonomy(self.taxonomy)}

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> UserConfig:
        paths: list[str] = [path for path, _, _ in _walk_taxonomy(self.taxonomy)]
        duplicates = {path for path in paths if paths.count(path) > 1}
        if duplicates:
            msg = f"duplicate category paths in taxonomy: {sorted(duplicates)}"
            raise ValueError(msg)

        path_set = set(paths)
        for rule in self.rules:
            if rule.category not in path_set:
                msg = f"rule {rule.pattern!r} references unknown category {rule.category!r}"
                raise ValueError(msg)
        for budget in self.budgets:
            if budget.category not in path_set:
                msg = f"budget {budget.name!r} references unknown category {budget.category!r}"
                raise ValueError(msg)

        source_names = [source.name for source in self.sources]
        duplicate_sources = {name for name in source_names if source_names.count(name) > 1}
        if duplicate_sources:
            msg = f"duplicate source names: {sorted(duplicate_sources)}"
            raise ValueError(msg)
        return self


def _walk_taxonomy(
    nodes: list[TaxonomyNode], prefix: str = ""
) -> Iterator[tuple[str, TaxonomyNode, str]]:
    """Yield (path, node, parent_path) depth-first; parent_path is '' for roots."""
    for node in nodes:
        path = f"{prefix}{CATEGORY_PATH_SEPARATOR}{node.name}" if prefix else node.name
        yield path, node, prefix
        yield from _walk_taxonomy(node.children, prefix=path)


def taxonomy_to_categories(nodes: list[TaxonomyNode]) -> dict[str, Category]:
    """Flatten a taxonomy tree into Category models keyed by path.

    Parent/child relationships are preserved via ``Category.parent_id``, ready
    for insertion into the ``categories`` table.
    """
    categories: dict[str, Category] = {}
    for path, node, parent_path in _walk_taxonomy(nodes):
        parent_id = categories[parent_path].id if parent_path else None
        categories[path] = Category(
            name=node.name,
            parent_id=parent_id,
            description=node.description,
        )
    return categories


def _read_yaml_list(path: Path) -> list[object]:
    """Read a YAML file expected to hold a top-level list; missing file = empty."""
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        msg = f"{path}: invalid YAML: {exc}"
        raise ConfigurationError(msg) from exc
    if data is None:
        return []
    if not isinstance(data, list):
        msg = f"{path}: expected a top-level list, got {type(data).__name__}"
        raise ConfigurationError(msg)
    return data


def load_user_config(config_dir: Path | None = None) -> UserConfig:
    """Load and validate the full user configuration from a directory.

    Args:
        config_dir: Directory holding the YAML files. Defaults to
            ``Settings.config_dir``.

    Raises:
        ConfigurationError: If any file is malformed or validation fails
            (unknown keys, bad regexes, dangling category references, ...).
    """
    if config_dir is None:
        config_dir = get_settings().config_dir
    raw = {key: _read_yaml_list(config_dir / filename) for key, filename in _CONFIG_FILES.items()}
    try:
        return UserConfig.model_validate(raw)
    except ValidationError as exc:
        msg = f"invalid configuration in {config_dir}: {exc}"
        raise ConfigurationError(msg) from exc
