"""Tests for personal_finance.models."""

from datetime import UTC, date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from personal_finance.models import (
    Account,
    AccountType,
    Budget,
    BudgetPeriod,
    CategorizationSource,
    Category,
    Document,
    DocumentStatus,
    DocumentType,
    EntityKind,
    Label,
    Link,
    LinkType,
    Merchant,
    Transaction,
    TransactionSplit,
)


def make_transaction(**overrides):
    defaults = {
        "account_id": "acct1",
        "posted_on": date(2026, 7, 1),
        "amount": Decimal("-42.50"),
        "description_raw": "TRADER JOES #123",
    }
    return Transaction(**{**defaults, **overrides})


class TestEntityDefaults:
    def test_ids_are_unique(self):
        a = Account(name="Checking", account_type=AccountType.CHECKING)
        b = Account(name="Checking", account_type=AccountType.CHECKING)
        assert a.id != b.id

    def test_created_at_is_timezone_aware_utc(self):
        merchant = Merchant(canonical_name="Trader Joe's")
        assert merchant.created_at.tzinfo is not None
        assert merchant.created_at.utcoffset() == UTC.utcoffset(None)

    def test_note_defaults_none_on_every_entity(self):
        assert make_transaction().note is None
        assert Merchant(canonical_name="X").note is None

    def test_user_note_accepted_on_any_entity(self):
        budget = Budget(
            name="Groceries",
            category_id="cat1",
            period=BudgetPeriod.MONTHLY,
            amount=Decimal("500"),
            starts_on=date(2026, 1, 1),
            note="includes the farmers market runs",
        )
        doc = Document(
            doc_type=DocumentType.RECEIPT,
            file_path="receipts/img001.jpg",
            note="crumpled receipt, totals may be misread",
        )
        assert budget.note == "includes the farmers market runs"
        assert doc.note == "crumpled receipt, totals may be misread"


class TestAccount:
    @pytest.mark.parametrize("account_type", list(AccountType))
    def test_all_account_types_construct(self, account_type):
        account = Account(name="X", account_type=account_type)
        assert account.account_type == account_type
        assert account.currency == "USD"

    def test_empty_name_rejected(self):
        with pytest.raises(ValidationError):
            Account(name="", account_type=AccountType.CASH)

    def test_name_whitespace_stripped(self):
        account = Account(name="  Checking  ", account_type=AccountType.CHECKING)
        assert account.name == "Checking"


class TestCategoryHierarchy:
    def test_root_category_has_no_parent(self):
        root = Category(name="essentials")
        assert root.parent_id is None

    def test_child_references_parent(self):
        root = Category(name="essentials")
        groceries = Category(name="groceries", parent_id=root.id)
        apples = Category(name="apples", parent_id=groceries.id)
        assert apples.parent_id == groceries.id
        assert groceries.parent_id == root.id


class TestTransaction:
    def test_signed_amounts_preserved_as_decimal(self):
        txn = make_transaction(amount=Decimal("-42.50"))
        assert txn.amount == Decimal("-42.50")
        assert isinstance(txn.amount, Decimal)

    def test_optional_provenance_fields_default_none(self):
        txn = make_transaction()
        assert txn.merchant_id is None
        assert txn.external_id is None
        assert txn.source is None


class TestTransactionSplit:
    def test_categorization_provenance(self):
        split = TransactionSplit(
            transaction_id="t1",
            amount=Decimal("-3.99"),
            description="HONEYCRISP APPLES",
            quantity=Decimal("2.15"),
            unit_price=Decimal("1.86"),
            category_id="cat-apples",
            categorization_source=CategorizationSource.EMBEDDING,
            categorization_confidence=0.92,
        )
        assert split.categorization_source == CategorizationSource.EMBEDDING

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_out_of_bounds_rejected(self, confidence):
        with pytest.raises(ValidationError):
            TransactionSplit(
                transaction_id="t1",
                amount=Decimal("1.00"),
                categorization_confidence=confidence,
            )


class TestDocument:
    def test_defaults_to_pending(self):
        doc = Document(doc_type=DocumentType.RECEIPT, file_path="receipts/img001.jpg")
        assert doc.status == DocumentStatus.PENDING
        assert doc.parsed_payload is None


class TestLink:
    def test_transfer_link_between_transactions(self):
        link = Link(
            link_type=LinkType.TRANSFER,
            from_kind=EntityKind.TRANSACTION,
            from_id="venmo-txn",
            to_kind=EntityKind.TRANSACTION,
            to_id="bank-txn",
            confidence=0.99,
        )
        assert link.link_type == LinkType.TRANSFER

    def test_receipt_match_links_document_to_transaction(self):
        link = Link(
            link_type=LinkType.RECEIPT_MATCH,
            from_kind=EntityKind.DOCUMENT,
            from_id="doc1",
            to_kind=EntityKind.TRANSACTION,
            to_id="txn1",
        )
        assert link.confidence is None

    @pytest.mark.parametrize("confidence", [-0.5, 2.0])
    def test_confidence_out_of_bounds_rejected(self, confidence):
        with pytest.raises(ValidationError):
            Link(
                link_type=LinkType.TRANSFER,
                from_kind=EntityKind.TRANSACTION,
                from_id="a",
                to_kind=EntityKind.TRANSACTION,
                to_id="b",
                confidence=confidence,
            )


class TestBudget:
    def test_positive_amount_required(self):
        with pytest.raises(ValidationError):
            Budget(
                name="Groceries",
                category_id="cat1",
                period=BudgetPeriod.MONTHLY,
                amount=Decimal("-100"),
                starts_on=date(2026, 1, 1),
            )

    @pytest.mark.parametrize("period", list(BudgetPeriod))
    def test_all_periods_construct(self, period):
        budget = Budget(
            name="B",
            category_id="cat1",
            period=period,
            amount=Decimal("500"),
            starts_on=date(2026, 1, 1),
        )
        assert budget.period == period


class TestLabel:
    def test_label_records_human_correction(self):
        label = Label(
            subject_kind=EntityKind.SPLIT,
            subject_id="split1",
            category_id="cat-apples",
            note="was misfiled as snacks",
        )
        assert label.subject_kind == EntityKind.SPLIT


class TestEnumsAreStr:
    @pytest.mark.parametrize(
        ("member", "value"),
        [
            (AccountType.CREDIT_CARD, "credit_card"),
            (LinkType.RECEIPT_MATCH, "receipt_match"),
            (CategorizationSource.HUMAN, "human"),
            (DocumentStatus.PARSED, "parsed"),
            (BudgetPeriod.MONTHLY, "monthly"),
            (EntityKind.DOCUMENT, "document"),
        ],
    )
    def test_enum_values_serialize_as_plain_strings(self, member, value):
        assert member == value
