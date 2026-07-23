"""Core domain models for the personal-finance schema.

These Pydantic models are the validated, in-memory representation of the nine
core entities described in docs/ARCHITECTURE.md. The matching DuckDB DDL lives
in `personal_finance.ddl`.

Conventions:
    - Monetary amounts are signed ``Decimal``: negative = outflow, positive = inflow.
    - All IDs are hex UUID strings generated client-side.
    - ``Transaction.external_id`` carries the source system's identifier and is
      unique per account, enabling idempotent re-ingestion.
"""

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field


def _new_id() -> str:
    """Return a new hex UUID string."""
    return uuid4().hex


def _utcnow() -> datetime:
    """Return the current UTC time (timezone-aware)."""
    return datetime.now(UTC)


class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT_CARD = "credit_card"
    CASH = "cash"
    PAYMENT_APP = "payment_app"  # Venmo, PayPal, ...
    INVESTMENT = "investment"
    LOAN = "loan"
    OTHER = "other"


class DocumentType(StrEnum):
    RECEIPT = "receipt"
    STATEMENT = "statement"
    EXPORT = "export"


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    MATCHED = "matched"
    FAILED = "failed"


class LinkType(StrEnum):
    TRANSFER = "transfer"  # paired movements across accounts (Venmo +320 / bank -320)
    RECEIPT_MATCH = "receipt_match"  # document tied to the charge it explains


class EntityKind(StrEnum):
    """Kinds of entities that links and labels may reference."""

    TRANSACTION = "transaction"
    SPLIT = "split"
    DOCUMENT = "document"


class CategorizationSource(StrEnum):
    """Which stage of the enrichment cascade assigned a category."""

    RULE = "rule"
    EMBEDDING = "embedding"
    LLM = "llm"
    HUMAN = "human"


class BudgetPeriod(StrEnum):
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    YEARLY = "yearly"


class Entity(BaseModel):
    """Base for all persisted entities: client-generated ID + creation timestamp.

    ``note`` is user-provided free-text context, available on every entity. It is
    distinct from source data (``description_raw``, split ``description``) and from
    definitional text (``Category.description``).
    """

    model_config = ConfigDict(str_strip_whitespace=True)

    id: str = Field(default_factory=_new_id)
    created_at: datetime = Field(default_factory=_utcnow)
    note: str | None = None


class Account(Entity):
    """A financial account: bank, credit card, payment app, cash, ..."""

    name: str = Field(min_length=1)
    account_type: AccountType
    institution: str | None = None
    currency: str = "USD"


class Merchant(Entity):
    """A normalized merchant entity; raw descriptors map to it via aliases."""

    canonical_name: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)


class Category(Entity):
    """A node in the hierarchical taxonomy (e.g. apples → groceries → essentials)."""

    name: str = Field(min_length=1)
    parent_id: str | None = None  # None = root category
    description: str | None = None


class Transaction(Entity):
    """One statement/export line from a source account."""

    account_id: str
    posted_on: date
    amount: Decimal  # signed: negative = outflow, positive = inflow
    currency: str = "USD"
    description_raw: str
    merchant_id: str | None = None
    external_id: str | None = None  # source system ID; unique per account when present
    source: str | None = None  # provenance: source name / originating file


class TransactionSplit(Entity):
    """A line item decomposing a transaction; unsplit transactions get one implicit split."""

    transaction_id: str
    amount: Decimal
    description: str | None = None
    quantity: Decimal | None = None
    unit_price: Decimal | None = None
    category_id: str | None = None
    categorization_source: CategorizationSource | None = None
    categorization_confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Document(Entity):
    """A source artifact (receipt image, statement PDF) and its parsing state."""

    doc_type: DocumentType
    file_path: str
    status: DocumentStatus = DocumentStatus.PENDING
    parsed_payload: dict[str, object] | None = None  # structured output from the vision LLM


class Link(Entity):
    """A typed correlation edge between two entities (transfer pair, receipt ↔ charge)."""

    link_type: LinkType
    from_kind: EntityKind
    from_id: str
    to_kind: EntityKind
    to_id: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class Budget(Entity):
    """A budget bucket over a category subtree for a recurring period."""

    name: str = Field(min_length=1)
    category_id: str
    period: BudgetPeriod
    amount: Decimal = Field(gt=0)
    starts_on: date


class Label(Entity):
    """A human categorization correction; training data for the embedding classifier."""

    subject_kind: EntityKind
    subject_id: str
    category_id: str


class Rule(Entity):
    """A deterministic pattern → category rule, seeded from ``rules.yaml``.

    ``priority`` is the rule's position in the config file (first match wins);
    seeding fully replaces this table each time, so it has no note to preserve.
    """

    pattern: str
    applies_to: str
    category_id: str
    priority: int


class MerchantAlias(Entity):
    """A deterministic pattern → canonical merchant name, seeded from ``merchants.yaml``.

    Resolves brand variants and other aliases the generic ``normalize_merchant``
    macro can't (see transform/models/silver/silver_transactions.sql).
    ``priority`` is the rule's position in the config file (first match wins);
    seeding fully replaces this table each time, so it has no note to preserve.
    """

    pattern: str
    canonical_name: str
    priority: int


class MerchantEmbedding(Entity):
    """A cached embedding vector for a distinct ``merchant_name``.

    Computed once per (merchant_name, model) via a local Ollama call — see
    :mod:`personal_finance.embed` — and reused across runs so re-running the
    embedding stage doesn't re-call Ollama for merchants already embedded.
    """

    merchant_name: str
    model: str
    embedding: list[float]


class MerchantLlmCategory(Entity):
    """A cached LLM category choice for a distinct ``merchant_name``.

    Stage 3 of the categorization cascade (:mod:`personal_finance.llm_categorize`)
    — the local-LLM fallback for merchants neither rules nor embedding
    similarity could place. Cached per (merchant_name, model), like
    :class:`MerchantEmbedding`, so re-running never re-asks the LLM about a
    merchant it already classified.
    """

    merchant_name: str
    model: str
    category_id: str
    confidence: float
