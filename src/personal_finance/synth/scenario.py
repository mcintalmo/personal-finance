"""Generate a coherent synthetic financial life.

One scenario = three accounts (checking, credit card, Venmo) with a few months
of correlated activity: biweekly payroll, monthly rent and subscriptions,
random grocery/gas/dining spend on the card, a monthly card payment from
checking, and Venmo payments that periodically cash out to checking.

The correlated pairs are deliberate: the Venmo cash-out (-X on Venmo, +X in
checking, same day) and the card payment (-X checking, +X card) are the fixture
cases for transfer detection in Phase 3.

Everything is driven by a seeded ``random.Random`` — the same seed yields an
identical scenario. Descriptions never contain commas, which keeps the quirky
CSV writers (which build lines by hand to reproduce real formats) safe.
"""

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from itertools import count
from random import Random

_CENT = Decimal("0.01")

GROCERY_MERCHANTS: tuple[tuple[str, str], ...] = (
    ("TRADER JOE'S #0552 SEATTLE WA", "Groceries"),
    ("KROGER #718", "Groceries"),
    ("SAFEWAY STORE 1442", "Groceries"),
    ("ALDI 73011", "Groceries"),
)
GAS_MERCHANTS: tuple[tuple[str, str], ...] = (
    ("SHELL OIL 57444", "Gas"),
    ("CHEVRON 0093 BELLEVUE WA", "Gas"),
)
DINING_MERCHANTS: tuple[tuple[str, str], ...] = (
    ("CHIPOTLE 1220", "Dining"),
    ("STARBUCKS #9985", "Dining"),
    ("THAI GINGER BELLEVUE", "Dining"),
)
# (description, category, monthly amount, day of month)
SUBSCRIPTIONS: tuple[tuple[str, str, Decimal, int], ...] = (
    ("NETFLIX.COM", "Entertainment", Decimal("15.49"), 7),
    ("SPOTIFY USA", "Entertainment", Decimal("11.99"), 12),
)
VENMO_FRIENDS: tuple[str, ...] = ("Jane Doe", "Sam Lee", "Priya Patel", "Diego Ruiz")
VENMO_NOTES: tuple[str, ...] = ("Dinner 🍜", "Rent split", "Concert tix", "Coffee ☕")

PAYROLL_DESCRIPTION = "ACME CORP PAYROLL"
RENT_DESCRIPTION = "CITYLINE APARTMENTS RENT"
CHECKING_OPENING_BALANCE = Decimal("3000.00")


@dataclass
class SynthTransaction:
    """One synthetic statement line, from the owning account's perspective."""

    posted_on: date
    amount: Decimal  # signed: negative = outflow
    description: str
    txn_type: str  # purchase | deposit | payment_out | payment_in | transfer_out | transfer_in
    external_id: str
    category_hint: str = ""  # issuer-style category, used by formats that ship one
    counterparty: str = ""  # payment-app peer (Venmo From/To)
    balance: Decimal | None = None  # running balance, set for cash accounts


@dataclass
class SynthAccount:
    """A synthetic account and its transactions."""

    name: str
    kind: str  # checking | credit_card | payment_app
    transactions: list[SynthTransaction] = field(default_factory=list)


@dataclass
class Scenario:
    """The full synthetic financial life keyed by account role."""

    checking: SynthAccount
    credit: SynthAccount
    venmo: SynthAccount

    @property
    def accounts(self) -> tuple[SynthAccount, ...]:
        return (self.checking, self.credit, self.venmo)


def _cents(rng: Random, low: int, high: int) -> Decimal:
    """Random amount between low and high cents, as a two-place Decimal."""
    return (Decimal(rng.randrange(low, high)) * _CENT).quantize(_CENT)


def _add_months(day: date, months: int) -> date:
    year_delta, month = divmod(day.month - 1 + months, 12)
    return date(day.year + year_delta, month + 1, 1)


def generate_scenario(seed: int = 42, start: date | None = None, months: int = 6) -> Scenario:
    """Generate a deterministic scenario.

    Args:
        seed: RNG seed; identical seeds yield identical scenarios.
        start: First day of the first month (defaults to 2026-01-01).
        months: Number of months of activity to generate.
    """
    if start is None:
        start = date(2026, 1, 1)
    start = start.replace(day=1)
    rng = Random(seed)

    checking = SynthAccount("Chase Checking", "checking")
    credit = SynthAccount("Chase Sapphire", "credit_card")
    venmo = SynthAccount("Venmo", "payment_app")
    ids = {"CHK": count(1), "CRD": count(1), "VEN": count(1)}

    def add(
        account: SynthAccount,
        prefix: str,
        posted_on: date,
        amount: Decimal,
        description: str,
        txn_type: str,
        category_hint: str = "",
        counterparty: str = "",
    ) -> SynthTransaction:
        txn = SynthTransaction(
            posted_on=posted_on,
            amount=amount,
            description=description,
            txn_type=txn_type,
            external_id=f"{prefix}{next(ids[prefix]):06d}",
            category_hint=category_hint,
            counterparty=counterparty,
        )
        account.transactions.append(txn)
        return txn

    for month_index in range(months):
        month_start = _add_months(start, month_index)
        next_month = _add_months(start, month_index + 1)
        days_in_month = (next_month - month_start).days

        # Income and fixed costs (checking).
        for payday in (1, 15):
            add(
                checking,
                "CHK",
                month_start.replace(day=payday),
                Decimal("2500.00"),
                PAYROLL_DESCRIPTION,
                "deposit",
                "Income",
            )
        add(
            checking,
            "CHK",
            month_start.replace(day=1),
            Decimal("-1800.00"),
            RENT_DESCRIPTION,
            "payment_out",
            "Housing",
        )

        # Subscriptions (credit card).
        for description, category, amount, day in SUBSCRIPTIONS:
            add(
                credit,
                "CRD",
                month_start.replace(day=day),
                -amount,
                description,
                "purchase",
                category,
            )

        # Variable card spend.
        for day_offset in range(days_in_month):
            day = month_start.replace(day=day_offset + 1)
            if rng.random() < 0.14:
                description, category = rng.choice(GROCERY_MERCHANTS)
                add(credit, "CRD", day, -_cents(rng, 800, 14000), description, "purchase", category)
            if rng.random() < 0.10:
                description, category = rng.choice(GAS_MERCHANTS)
                add(credit, "CRD", day, -_cents(rng, 3000, 7000), description, "purchase", category)
            if rng.random() < 0.12:
                description, category = rng.choice(DINING_MERCHANTS)
                add(credit, "CRD", day, -_cents(rng, 900, 6500), description, "purchase", category)

        # Card payment on the 25th: pay this month's purchases in full.
        month_spend = -sum(
            (t.amount for t in credit.transactions if month_start <= t.posted_on < next_month),
            Decimal("0.00"),
        )
        payment_day = month_start.replace(day=25)
        add(
            checking,
            "CHK",
            payment_day,
            -month_spend,
            "CHASE CREDIT CRD AUTOPAY",
            "payment_out",
            "Payment",
        )
        add(
            credit,
            "CRD",
            payment_day,
            month_spend,
            "Payment Thank You - Web",
            "payment_in",
            "Payment",
        )

        # Venmo: a few incoming payments, then a month-end cash-out to checking.
        month_venmo_total = Decimal("0.00")
        for _ in range(rng.randrange(2, 5)):
            day = month_start.replace(day=rng.randrange(2, 26))
            amount = _cents(rng, 800, 9000)
            month_venmo_total += amount
            add(
                venmo,
                "VEN",
                day,
                amount,
                rng.choice(VENMO_NOTES),
                "payment_in",
                counterparty=rng.choice(VENMO_FRIENDS),
            )
        cashout_day = month_start.replace(day=28)
        add(venmo, "VEN", cashout_day, -month_venmo_total, "", "transfer_out")
        add(
            checking,
            "CHK",
            cashout_day,
            month_venmo_total,
            "VENMO CASHOUT PPD ID: 5264681992",
            "transfer_in",
            "Transfer",
        )

    for account, opening in ((checking, CHECKING_OPENING_BALANCE), (venmo, Decimal("0.00"))):
        account.transactions.sort(key=lambda t: (t.posted_on, t.external_id))
        running = opening
        for txn in account.transactions:
            running += txn.amount
            txn.balance = running
    credit.transactions.sort(key=lambda t: (t.posted_on, t.external_id))

    return Scenario(checking=checking, credit=credit, venmo=venmo)
