"""Tests for personal_finance.user_config."""

from decimal import Decimal
from pathlib import Path

import pytest

from personal_finance.exceptions import ConfigurationError
from personal_finance.models import AccountType, BudgetPeriod
from personal_finance.user_config import (
    RuleConfig,
    SourceConfig,
    SourceKind,
    TaxonomyNode,
    UserConfig,
    load_user_config,
    taxonomy_to_categories,
)

EXAMPLES_CONFIG_DIR = Path(__file__).parent.parent / "config" / "examples"


def write_config(tmp_path, **files):
    for name, content in files.items():
        (tmp_path / f"{name}.yaml").write_text(content, encoding="utf-8")
    return tmp_path


MINIMAL_TAXONOMY = """
- name: essentials
  children:
    - name: groceries
"""


class TestLoadUserConfig:
    def test_repo_sample_config_is_valid(self):
        config = load_user_config(EXAMPLES_CONFIG_DIR)
        assert {source.name for source in config.sources} >= {"chase_checking", "venmo"}
        assert "essentials/groceries/apples" in config.category_paths()
        assert config.rules and config.budgets

    def test_missing_directory_yields_empty_config(self, tmp_path):
        config = load_user_config(tmp_path / "does-not-exist")
        assert config == UserConfig()

    def test_missing_files_are_empty_sections(self, tmp_path):
        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)
        config = load_user_config(tmp_path)
        assert config.category_paths() == {"essentials", "essentials/groceries"}
        assert config.sources == []
        assert config.rules == []

    def test_empty_file_is_empty_section(self, tmp_path):
        write_config(tmp_path, taxonomy="")
        assert load_user_config(tmp_path).taxonomy == []

    def test_invalid_yaml_raises_configuration_error(self, tmp_path):
        write_config(tmp_path, taxonomy="- name: [unclosed")
        with pytest.raises(ConfigurationError, match="invalid YAML"):
            load_user_config(tmp_path)

    def test_non_list_top_level_raises_configuration_error(self, tmp_path):
        write_config(tmp_path, taxonomy="name: essentials")
        with pytest.raises(ConfigurationError, match="expected a top-level list"):
            load_user_config(tmp_path)

    def test_unknown_key_raises_configuration_error(self, tmp_path):
        write_config(tmp_path, taxonomy="- name: a\n  colour: red")
        with pytest.raises(ConfigurationError, match="colour"):
            load_user_config(tmp_path)

    def test_default_dir_comes_from_settings(self, monkeypatch, tmp_path):
        from personal_finance import user_config as module

        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)

        class FakeSettings:
            config_dir = tmp_path

        monkeypatch.setattr(module, "get_settings", lambda: FakeSettings())
        config = load_user_config()
        assert "essentials" in config.category_paths()


class TestReferentialIntegrity:
    def test_rule_with_unknown_category_rejected(self, tmp_path):
        write_config(
            tmp_path,
            taxonomy=MINIMAL_TAXONOMY,
            rules='- pattern: "kroger"\n  category: essentials/nope',
        )
        with pytest.raises(ConfigurationError, match="unknown category"):
            load_user_config(tmp_path)

    def test_budget_with_unknown_category_rejected(self, tmp_path):
        write_config(
            tmp_path,
            taxonomy=MINIMAL_TAXONOMY,
            budgets="- name: B\n  category: nope\n  period: monthly\n  amount: 10",
        )
        with pytest.raises(ConfigurationError, match="unknown category"):
            load_user_config(tmp_path)

    def test_duplicate_source_names_rejected(self, tmp_path):
        source = "- name: dup\n  kind: csv\n  account_name: A\n  account_type: checking\n"
        write_config(tmp_path, sources=source + source.replace("A", "B"))
        with pytest.raises(ConfigurationError, match="duplicate source names"):
            load_user_config(tmp_path)

    def test_duplicate_category_paths_rejected(self, tmp_path):
        write_config(tmp_path, taxonomy="- name: a\n- name: a")
        with pytest.raises(ConfigurationError, match="duplicate category paths"):
            load_user_config(tmp_path)

    def test_same_child_name_under_different_parents_allowed(self):
        config = UserConfig(
            taxonomy=[
                TaxonomyNode(name="essentials", children=[TaxonomyNode(name="groceries")]),
                TaxonomyNode(name="non-essentials", children=[TaxonomyNode(name="groceries")]),
            ]
        )
        assert "essentials/groceries" in config.category_paths()
        assert "non-essentials/groceries" in config.category_paths()


class TestModelValidation:
    def test_invalid_regex_rejected(self):
        with pytest.raises(ValueError, match="invalid regular expression"):
            RuleConfig(pattern="([unclosed", category="a")

    def test_category_name_with_separator_rejected(self):
        with pytest.raises(ValueError, match="must not contain"):
            TaxonomyNode(name="a/b")

    def test_source_kinds(self):
        source = SourceConfig(
            name="s",
            kind=SourceKind.OFX,
            account_name="A",
            account_type=AccountType.CREDIT_CARD,
        )
        assert source.column_map == {}
        assert source.currency == "USD"

    def test_budget_amount_must_be_positive(self, tmp_path):
        write_config(
            tmp_path,
            taxonomy=MINIMAL_TAXONOMY,
            budgets="- name: B\n  category: essentials\n  period: monthly\n  amount: -5",
        )
        with pytest.raises(ConfigurationError):
            load_user_config(tmp_path)


class TestTaxonomyToCategories:
    def test_parent_links_and_paths(self):
        nodes = [
            TaxonomyNode(
                name="essentials",
                description="Necessary spending",
                children=[
                    TaxonomyNode(name="groceries", children=[TaxonomyNode(name="apples")]),
                ],
            )
        ]
        categories = taxonomy_to_categories(nodes)

        root = categories["essentials"]
        groceries = categories["essentials/groceries"]
        apples = categories["essentials/groceries/apples"]

        assert root.parent_id is None
        assert root.description == "Necessary spending"
        assert groceries.parent_id == root.id
        assert apples.parent_id == groceries.id
        assert apples.name == "apples"

    def test_repo_taxonomy_flattens_completely(self):
        config = load_user_config(EXAMPLES_CONFIG_DIR)
        categories = taxonomy_to_categories(config.taxonomy)
        assert set(categories) == config.category_paths()

    def test_budget_period_from_yaml_is_enum(self):
        config = load_user_config(EXAMPLES_CONFIG_DIR)
        assert all(isinstance(budget.period, BudgetPeriod) for budget in config.budgets)
        assert all(
            isinstance(budget.amount, Decimal) and budget.amount > 0 for budget in config.budgets
        )
