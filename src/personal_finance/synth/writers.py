"""Render synthetic transactions as realistic export files.

Each writer reproduces one verified layout from docs/source-schemas.md,
INCLUDING its quirks — headerless quoted Wells Fargo rows, the Bank of America
summary preamble, BMO's integer dates and leading-space header, Venmo's
``+ $32.00`` amount strings, Ally's ``$`` symbols, inverted sign conventions,
and split Debit/Credit columns. The quirks are the point: ingestion code that
survives these fixtures survives the real files.

Lines are built by hand (not the csv module) because several formats are not
well-formed CSV; scenario descriptions are comma-free by construction.
"""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from datetime import date
    from pathlib import Path

    from personal_finance.synth.scenario import Scenario, SynthTransaction


def _mdy(day: date) -> str:
    return day.strftime("%m/%d/%Y")


def _time_of(txn: SynthTransaction) -> str:
    """Deterministic pseudo-time derived from the transaction ID."""
    seconds = (int(txn.external_id[3:]) * 4271) % 86400
    hour, rem = divmod(seconds, 3600)
    minute, second = divmod(rem, 60)
    return f"{hour:02d}:{minute:02d}:{second:02d}"


def chase_checking_csv(txns: Sequence[SynthTransaction]) -> str:
    type_map = {
        "purchase": "DEBIT_CARD",
        "deposit": "ACH_CREDIT",
        "payment_out": "ACH_DEBIT",
        "transfer_in": "QUICKPAY_CREDIT",
        "transfer_out": "QUICKPAY_DEBIT",
        "payment_in": "ACH_CREDIT",
    }
    lines = ["Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #"]
    lines += [
        f"{'DEBIT' if t.amount < 0 else 'CREDIT'},{_mdy(t.posted_on)},{t.description},"
        f"{t.amount},{type_map.get(t.txn_type, 'MISC')},{t.balance if t.balance is not None else ''},"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def wells_fargo_csv(txns: Sequence[SynthTransaction]) -> str:
    """No header row; five fully-quoted columns; '*' status marker."""
    return (
        "\n".join(f'"{_mdy(t.posted_on)}","{t.amount}","*","","{t.description}"' for t in txns)
        + "\n"
    )


def bofa_checking_csv(txns: Sequence[SynthTransaction]) -> str:
    """Summary preamble before the real header — ingestion must skip to it."""
    first = txns[0]
    opening = (first.balance - first.amount) if first.balance is not None else 0
    lines = [
        "Description,,Summary Amt.",
        f'Beginning balance as of {_mdy(first.posted_on)},,"{opening:,.2f}"',
        ",,",
        "Date,Description,Amount,Running Bal.",
    ]
    lines += [
        f'{_mdy(t.posted_on)},"{t.description}",{t.amount},"{t.balance:,.2f}"'
        for t in txns
        if t.balance is not None
    ]
    return "\n".join(lines) + "\n"


def us_bank_csv(txns: Sequence[SynthTransaction]) -> str:
    lines = ["Date,Transaction,Name,Memo,Amount"]
    lines += [
        f"{_mdy(t.posted_on)},{'DEBIT' if t.amount < 0 else 'CREDIT'},{t.description},"
        f"{t.external_id},{t.amount}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def ally_csv(txns: Sequence[SynthTransaction]) -> str:
    """ISO dates and '$' symbols in amounts (the currency-cleanup fixture)."""
    lines = ["Date,Time,Amount,Type,Description"]
    lines += [
        f"{t.posted_on.isoformat()},{_time_of(t)},"
        f"{'-' if t.amount < 0 else ''}${abs(t.amount)},"
        f"{'Withdrawal' if t.amount < 0 else 'Deposit'},{t.description}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def usaa_csv(txns: Sequence[SynthTransaction]) -> str:
    lines = ["Date,Description,Original Description,Amount,Balance"]
    lines += [
        f"{_mdy(t.posted_on)},{t.description.title()},{t.description},{t.amount},"
        f"{t.balance if t.balance is not None else ''}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def bmo_csv(txns: Sequence[SynthTransaction]) -> str:
    """Masked card number, YYYYMMDD integer dates, leading-space header cell."""
    lines = [
        "Following data is valid as of 20260711343021",
        "",
        "First Bank Card,Transaction Type,Date Posted, Transaction Amount,Description",
    ]
    lines += [
        f"'5191830112345678',{'DEBIT' if t.amount < 0 else 'CREDIT'},"
        f"{t.posted_on.strftime('%Y%m%d')},{t.amount},"
        f"[{'SO' if t.amount < 0 else 'DN'}]{t.description}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def chase_credit_csv(txns: Sequence[SynthTransaction]) -> str:
    lines = ["Transaction Date,Post Date,Description,Category,Type,Amount,Memo"]
    for t in txns:
        txn_type = "Payment" if t.txn_type == "payment_in" else "Sale"
        category = "" if txn_type == "Payment" else t.category_hint
        lines.append(
            f"{_mdy(t.posted_on)},{_mdy(t.posted_on)},{t.description},{category},"
            f"{txn_type},{t.amount},"
        )
    return "\n".join(lines) + "\n"


def capital_one_csv(txns: Sequence[SynthTransaction]) -> str:
    """ISO dates; Debit/Credit split, both positive; Card No. last-4."""
    lines = ["Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit"]
    for t in txns:
        debit = abs(t.amount) if t.amount < 0 else ""
        credit = t.amount if t.amount > 0 else ""
        category = t.category_hint or ("Payment" if t.txn_type == "payment_in" else "Other")
        lines.append(
            f"{t.posted_on.isoformat()},{t.posted_on.isoformat()},1234,"
            f"{t.description},{category},{debit},{credit}"
        )
    return "\n".join(lines) + "\n"


def amex_csv(txns: Sequence[SynthTransaction]) -> str:
    """Inverted sign convention: charges positive, payments negative."""
    lines = ["Date,Description,Amount"]
    lines += [f"{_mdy(t.posted_on)},{t.description},{-t.amount}" for t in txns]
    return "\n".join(lines) + "\n"


def discover_csv(txns: Sequence[SynthTransaction]) -> str:
    """Charges positive; issuer category column included."""
    lines = ["Trans. Date,Post Date,Description,Amount,Category"]
    lines += [
        f"{_mdy(t.posted_on)},{_mdy(t.posted_on)},{t.description},{-t.amount},"
        f"{'Payments and Credits' if t.txn_type == 'payment_in' else t.category_hint}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def citi_csv(txns: Sequence[SynthTransaction]) -> str:
    """Debit/Credit split, both positive, one populated per row."""
    lines = ["Status,Date,Description,Debit,Credit"]
    lines += [
        f"Cleared,{_mdy(t.posted_on)},{t.description},"
        f"{abs(t.amount) if t.amount < 0 else ''},{t.amount if t.amount > 0 else ''}"
        for t in txns
    ]
    return "\n".join(lines) + "\n"


def apple_card_csv(txns: Sequence[SynthTransaction]) -> str:
    lines = ["Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)"]
    for t in txns:
        txn_type = "Payment" if t.txn_type == "payment_in" else "Purchase"
        merchant = t.description.split("#")[0].strip().title()
        lines.append(
            f"{_mdy(t.posted_on)},{_mdy(t.posted_on)},{t.description},{merchant},"
            f"{t.category_hint},{txn_type},{-t.amount}"
        )
    return "\n".join(lines) + "\n"


def venmo_csv(txns: Sequence[SynthTransaction]) -> str:
    """Signed '+ $32.00' amount strings; Standard Transfer rows for cash-outs."""
    header = (
        "ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (fee),"
        "Funding Source,Destination"
    )
    lines = [header]
    for t in txns:
        amount = f"{'+' if t.amount > 0 else '-'} ${abs(t.amount)}"
        stamp = f"{t.posted_on.isoformat()}T{_time_of(t)}"
        if t.txn_type == "transfer_out":
            lines.append(
                f"{t.external_id},{stamp},Standard Transfer,Issued,,,,{amount},,"
                f"Venmo balance,Chase Checking x1234"
            )
        else:
            lines.append(
                f"{t.external_id},{stamp},Payment,Complete,{t.description},"
                f"{t.counterparty},Sample User,{amount},,Venmo balance,"
            )
    return "\n".join(lines) + "\n"


def ofx(txns: Sequence[SynthTransaction]) -> str:
    """Minimal OFX 1.02 SGML statement (the ugliest common case)."""
    first, last = txns[0], txns[-1]
    stmttrns = "".join(
        "<STMTTRN>"
        f"<TRNTYPE>{'DEBIT' if t.amount < 0 else 'CREDIT'}"
        f"<DTPOSTED>{t.posted_on.strftime('%Y%m%d')}120000"
        f"<TRNAMT>{t.amount}"
        f"<FITID>{t.external_id}"
        f"<NAME>{t.description}"
        "</STMTTRN>\n"
        for t in txns
    )
    return (
        "OFXHEADER:100\nDATA:OFXSGML\nVERSION:102\nSECURITY:NONE\nENCODING:USASCII\n"
        "CHARSET:1252\nCOMPRESSION:NONE\nOLDFILEUID:NONE\nNEWFILEUID:NONE\n\n"
        "<OFX><SIGNONMSGSRSV1><SONRS><STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        "<DTSERVER>20260711120000<LANGUAGE>ENG</SONRS></SIGNONMSGSRSV1>"
        "<BANKMSGSRSV1><STMTTRNRS><TRNUID>1<STATUS><CODE>0<SEVERITY>INFO</STATUS>"
        "<STMTRS><CURDEF>USD<BANKACCTFROM><BANKID>021000021<ACCTID>000001234"
        "<ACCTTYPE>CHECKING</BANKACCTFROM><BANKTRANLIST>"
        f"<DTSTART>{first.posted_on.strftime('%Y%m%d')}"
        f"<DTEND>{last.posted_on.strftime('%Y%m%d')}\n"
        f"{stmttrns}"
        "</BANKTRANLIST></STMTRS></STMTTRNRS></BANKMSGSRSV1></OFX>\n"
    )


FORMATS: dict[str, Callable[[Sequence[SynthTransaction]], str]] = {
    "chase_checking": chase_checking_csv,
    "wells_fargo": wells_fargo_csv,
    "bofa_checking": bofa_checking_csv,
    "us_bank": us_bank_csv,
    "ally": ally_csv,
    "usaa": usaa_csv,
    "bmo": bmo_csv,
    "chase_credit": chase_credit_csv,
    "capital_one": capital_one_csv,
    "amex": amex_csv,
    "discover": discover_csv,
    "citi": citi_csv,
    "apple_card": apple_card_csv,
    "venmo": venmo_csv,
    "ofx": ofx,
}

CHECKING_FORMATS = (
    "chase_checking",
    "wells_fargo",
    "bofa_checking",
    "us_bank",
    "ally",
    "usaa",
    "bmo",
    "ofx",
)
CREDIT_FORMATS = ("chase_credit", "capital_one", "amex", "discover", "citi", "apple_card")


def render(format_name: str, txns: Sequence[SynthTransaction]) -> str:
    """Render transactions in the named format.

    Raises:
        KeyError: If the format is unknown (see ``FORMATS``).
    """
    return FORMATS[format_name](txns)


def write_scenario(scenario: Scenario, out_dir: Path) -> list[Path]:
    """Write the scenario as one export file per applicable format.

    Checking activity renders in every bank format, card activity in every
    card format, Venmo in its own — the full quirk gauntlet for ingestion.

    Returns:
        The written file paths.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    jobs = [
        *((name, scenario.checking.transactions) for name in CHECKING_FORMATS),
        *((name, scenario.credit.transactions) for name in CREDIT_FORMATS),
        ("venmo", scenario.venmo.transactions),
    ]
    for name, txns in jobs:
        suffix = "ofx" if name == "ofx" else "csv"
        path = out_dir / f"{name}.{suffix}"
        path.write_text(render(name, txns), encoding="utf-8")
        written.append(path)
    return written
