"""Tests for personal_finance.user_config."""

from decimal import Decimal
from pathlib import Path

import pytest

from personal_finance.exceptions import ConfigurationError
from personal_finance.models import AccountType, BudgetPeriod
from personal_finance.user_config import (
    MerchantAliasConfig,
    RuleConfig,
    SourceConfig,
    SourceKind,
    TaxonomyNode,
    UserConfig,
    config_file_names,
    load_user_config,
    read_config_file,
    taxonomy_to_categories,
    write_config_file,
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
        assert config.merchant_aliases
        assert config.known_cities == ["Bellevue"]

    def test_missing_directory_yields_empty_config(self, tmp_path):
        config = load_user_config(tmp_path / "does-not-exist")
        assert config == UserConfig()

    def test_missing_files_are_empty_sections(self, tmp_path):
        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)
        config = load_user_config(tmp_path)
        assert config.category_paths() == {"essentials", "essentials/groceries"}
        assert config.sources == []
        assert config.rules == []
        assert config.merchant_aliases == []
        assert config.known_cities == []

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

    def test_known_cities_loads_a_plain_string_list(self, tmp_path):
        write_config(tmp_path, places="- Bellevue\n- Seattle\n")
        config = load_user_config(tmp_path)
        assert config.known_cities == ["Bellevue", "Seattle"]

    def test_merchant_aliases_load(self, tmp_path):
        write_config(
            tmp_path,
            merchants='- pattern: "(?i)^costco"\n  canonical_name: "COSTCO"\n',
        )
        config = load_user_config(tmp_path)
        assert config.merchant_aliases[0].canonical_name == "COSTCO"

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
        source = (
            "- name: dup\n"
            "  kind: csv\n"
            "  account_name: A\n"
            "  account_type: checking\n"
            "  column_map:\n"
            "    posted_on: date_col\n"
            "    amount: amt_col\n"
            "    description_raw: desc_col\n"
        )
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

    def test_merchant_alias_invalid_regex_rejected(self):
        with pytest.raises(ValueError, match="invalid regular expression"):
            MerchantAliasConfig(pattern="([unclosed", canonical_name="a")

    def test_merchant_alias_valid(self):
        alias = MerchantAliasConfig(pattern="(?i)^costco", canonical_name="COSTCO")
        assert alias.canonical_name == "COSTCO"

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

    def test_amazon_kind_needs_no_account_fields(self):
        source = SourceConfig(name="amazon", kind=SourceKind.AMAZON)
        assert source.account_name is None
        assert source.account_type is None

    def test_csv_kind_requires_account_fields(self):
        with pytest.raises(ValueError, match="requires account_name/account_type"):
            SourceConfig(
                name="s",
                kind=SourceKind.CSV,
                column_map={"posted_on": "Date", "description_raw": "Desc", "amount": "Amt"},
            )

    def test_ofx_kind_requires_account_fields(self):
        with pytest.raises(ValueError, match="requires account_name/account_type"):
            SourceConfig(name="s", kind=SourceKind.OFX)

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


class TestConfigFileEditing:
    """Phase 6's config-editing API reads/writes single files through these
    helpers rather than the whole-directory load_user_config."""

    def test_config_file_names_matches_load_user_config_keys(self):
        names = config_file_names()
        assert names["rules"] == "rules.yaml"
        assert names["taxonomy"] == "taxonomy.yaml"
        assert names["budgets"] == "budgets.yaml"

    def test_read_missing_file_is_empty_string(self, tmp_path):
        assert read_config_file("rules", tmp_path) == ""

    def test_write_then_read_round_trips(self, tmp_path):
        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)
        content = "- pattern: '(?i)netflix'\n  category: essentials/groceries\n"
        write_config_file("rules", content, tmp_path)
        assert read_config_file("rules", tmp_path) == content
        assert (tmp_path / "rules.yaml").read_text(encoding="utf-8") == content

    def test_invalid_yaml_raises_and_does_not_write(self, tmp_path):
        with pytest.raises(ConfigurationError, match="invalid YAML"):
            write_config_file("rules", "not: [valid", tmp_path)
        assert not (tmp_path / "rules.yaml").exists()

    def test_referential_integrity_violation_raises_and_does_not_write(self, tmp_path):
        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)
        bad = "- pattern: '(?i)netflix'\n  category: no/such/category\n"
        with pytest.raises(ConfigurationError):
            write_config_file("rules", bad, tmp_path)
        assert not (tmp_path / "rules.yaml").exists()

    def test_valid_write_is_visible_to_load_user_config(self, tmp_path):
        write_config(tmp_path, taxonomy=MINIMAL_TAXONOMY)
        write_config_file(
            "rules", "- pattern: '(?i)netflix'\n  category: essentials/groceries\n", tmp_path
        )
        config = load_user_config(tmp_path)
        assert len(config.rules) == 1
        assert config.rules[0].category == "essentials/groceries"
