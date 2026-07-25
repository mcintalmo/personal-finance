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

from pydantic import BaseModel, ConfigDict, Field, model_validator


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


class Flow(StrEnum):
    """Which direction money moved, matching silver_transactions.flow.

    A closed set that dbt derives from the sign of the amount and guards with
    an `accepted_values` test. Parsed into this enum on the way out of the
    warehouse so an unexpected value fails at the row that is wrong: code that
    partitions on flow (see forecast.load_series) would otherwise drop an
    unrecognized group from *both* halves, and money silently going missing
    from a forecast is invisible to every downstream invariant.
    """

    INFLOW = "inflow"
    OUTFLOW = "outflow"


class ForecastSeriesKind(StrEnum):
    """Which kind of series a forecast row belongs to."""

    TOTAL_INFLOW = "total_inflow"
    TOTAL_OUTFLOW = "total_outflow"
    BUDGET_CATEGORY = "budget_category"


class TrendDirection(StrEnum):
    """Direction of the fitted trend over the observed history.

    Answers "is this climbing month over month, or was last month just
    expensive?" — a level shift shows up as FLAT, a sustained climb as RISING.
    """

    RISING = "rising"
    FALLING = "falling"
    FLAT = "flat"


class MergeStatus(StrEnum):
    """A human decision on a candidate merchant-identity merge."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"


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


class MerchantMerge(Entity):
    """A human decision on a candidate merchant-identity merge.

    Surfaced by :mod:`personal_finance.merchant_merge` (embedding-similarity
    over cached ``merchant_embeddings``) for descriptor variants the
    deterministic ``normalize_merchant`` macro and ``merchants.yaml`` aliases
    couldn't collapse. Unlike ``MerchantAlias`` (config-seeded, full-replace
    each run), this is a runtime human decision — same pattern as
    :class:`Label` for categorization corrections: kept in a table read
    directly by silver_transactions.sql, latest decision per
    ``merchant_name`` wins if reviewed more than once. ``canonical_name`` is
    the candidate that was proposed, whether accepted or rejected (kept for
    audit even on rejection).
    """

    merchant_name: str
    canonical_name: str
    similarity: float | None = Field(default=None, ge=0.0, le=1.0)
    status: MergeStatus


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


class ProductEmbedding(Entity):
    """A cached embedding vector for a distinct split ``product_name``.

    The split-categorization cascade's analog of :class:`MerchantEmbedding` —
    kept in its own table (not merged into ``merchant_embeddings``) since a
    product name and a merchant name are different vocabularies; comparing a
    product's embedding against merchant embeddings (or vice versa) would be
    a nonsensical nearest-neighbor match.
    """

    product_name: str
    model: str
    embedding: list[float]


class ProductLlmCategory(Entity):
    """A cached LLM category choice for a distinct split ``product_name``.

    The split-categorization cascade's analog of :class:`MerchantLlmCategory`.
    """

    product_name: str
    model: str
    category_id: str
    confidence: float


class Forecast(Entity):
    """One forecast month for one series, decomposed into its two components.

    ``predicted_amount`` is always ``committed_amount + variable_amount``:

    * **committed** — recurring flows due that month (rent and subscriptions
      for a spend series, salary for the income one), projected forward
      deterministically from ``gold_recurring_flows`` on each group's own
      observed cadence. Known, not estimated.
    * **variable** — everything else, from a statistical model fit to the
      history with the committed component removed.

    The interval covers the **variable component only**, so a category that is
    mostly subscriptions gets a tight band and a mostly-discretionary one gets
    an honest wide band. See :mod:`personal_finance.forecast`.
    """

    series_kind: ForecastSeriesKind
    series_key: str  # 'total_inflow' | 'total_outflow' | a budget id
    series_label: str
    category_id: str | None = None  # set for BUDGET_CATEGORY series
    period_start: date  # first day of the forecast month
    horizon: int = Field(ge=1)  # months ahead of trained_through
    committed_amount: Decimal
    variable_amount: Decimal
    predicted_amount: Decimal
    lower_bound: Decimal
    upper_bound: Decimal
    interval_level: int = Field(ge=1, le=99)
    model_name: str
    # Backtest error vs. naive; < 1 beats naive. None means there was no
    # meaningful scale to divide by (a perfectly flat series has zero naive
    # error), or that every candidate failed to fit and `model_name` fell back
    # to "mean" — the two cases are distinguishable by `model_name`.
    mase: float | None = None
    trend: TrendDirection
    trained_through: date  # last COMPLETE month used to fit

    @model_validator(mode="after")
    def _check_invariants(self) -> Forecast:
        """Enforce what the docstring, the mart and the dbt test all assume.

        These held only by construction before, so a rounding slip in
        `personal_finance.forecast` surfaced as a failed dbt build after the
        rows were already written. Checking here fails at the row that is
        wrong, which is where the bug actually is.
        """
        if self.predicted_amount != self.committed_amount + self.variable_amount:
            message = (
                f"predicted_amount {self.predicted_amount} != committed "
                f"{self.committed_amount} + variable {self.variable_amount}"
            )
            raise ValueError(message)
        if not (self.lower_bound <= self.predicted_amount <= self.upper_bound):
            message = (
                f"interval [{self.lower_bound}, {self.upper_bound}] does not "
                f"bracket predicted_amount {self.predicted_amount}"
            )
            raise ValueError(message)
        is_total = self.series_kind is not ForecastSeriesKind.BUDGET_CATEGORY
        if is_total and self.series_key != self.series_kind.value:
            message = f"{self.series_kind} must use its own name as series_key"
            raise ValueError(message)
        if not is_total and self.category_id is None:
            message = "a budget_category forecast must carry a category_id"
            raise ValueError(message)
        return self
