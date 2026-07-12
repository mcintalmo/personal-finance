"""Tests for personal_finance.synth."""

from decimal import Decimal

import pytest

from personal_finance.synth import (
    FORMATS,
    generate_scenario,
    render,
    write_scenario,
)
from personal_finance.synth.writers import CHECKING_FORMATS, CREDIT_FORMATS

EXPECTED_HEADERS = {
    "chase_checking": "Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #",
    "us_bank": "Date,Transaction,Name,Memo,Amount",
    "ally": "Date,Time,Amount,Type,Description",
    "usaa": "Date,Description,Original Description,Amount,Balance",
    "chase_credit": "Transaction Date,Post Date,Description,Category,Type,Amount,Memo",
    "capital_one": "Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit",
    "amex": "Date,Description,Amount",
    "discover": "Trans. Date,Post Date,Description,Amount,Category",
    "citi": "Status,Date,Description,Debit,Credit",
    "apple_card": "Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)",
    "venmo": (
        "ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (fee),"
        "Funding Source,Destination"
    ),
}


@pytest.fixture(scope="module")
def scenario():
    return generate_scenario(seed=42, months=3)


class TestScenario:
    def test_deterministic_for_same_seed(self):
        a, b = generate_scenario(seed=7, months=2), generate_scenario(seed=7, months=2)
        assert a == b

    def test_different_seed_differs(self):
        a, b = generate_scenario(seed=7, months=2), generate_scenario(seed=8, months=2)
        assert a != b

    def test_venmo_cashout_pairs_match_checking_deposits(self, scenario):
        cashouts = [t for t in scenario.venmo.transactions if t.txn_type == "transfer_out"]
        deposits = [t for t in scenario.checking.transactions if t.txn_type == "transfer_in"]
        assert len(cashouts) == 3  # one per month
        for cashout, deposit in zip(cashouts, deposits, strict=True):
            assert cashout.posted_on == deposit.posted_on
            assert cashout.amount == -deposit.amount

    def test_card_payment_pairs_match(self, scenario):
        payments_in = [t for t in scenario.credit.transactions if t.txn_type == "payment_in"]
        autopays = [
            t for t in scenario.checking.transactions if t.description == "CHASE CREDIT CRD AUTOPAY"
        ]
        for received, paid in zip(payments_in, autopays, strict=True):
            assert received.amount == -paid.amount
            assert received.posted_on == paid.posted_on

    def test_card_payment_covers_monthly_spend(self, scenario):
        purchases = sum(
            (t.amount for t in scenario.credit.transactions if t.amount < 0),
            Decimal("0"),
        )
        payments = sum(
            (t.amount for t in scenario.credit.transactions if t.amount > 0),
            Decimal("0"),
        )
        assert payments == -purchases  # paid in full each month

    def test_running_balance_consistent(self, scenario):
        txns = scenario.checking.transactions
        running = Decimal("3000.00")
        for txn in txns:
            running += txn.amount
            assert txn.balance == running

    def test_transactions_sorted_and_ids_unique(self, scenario):
        for account in scenario.accounts:
            dates = [t.posted_on for t in account.transactions]
            assert dates == sorted(dates)
            ids = [t.external_id for t in account.transactions]
            assert len(ids) == len(set(ids))


class TestWriters:
    @pytest.mark.parametrize(("format_name", "header"), sorted(EXPECTED_HEADERS.items()))
    def test_exact_header(self, scenario, format_name, header):
        txns = (
            scenario.venmo.transactions
            if format_name == "venmo"
            else scenario.credit.transactions
            if format_name in CREDIT_FORMATS
            else scenario.checking.transactions
        )
        assert render(format_name, txns).splitlines()[0] == header

    @pytest.mark.parametrize("format_name", sorted(EXPECTED_HEADERS))
    def test_row_count(self, scenario, format_name):
        txns = (
            scenario.venmo.transactions
            if format_name == "venmo"
            else scenario.credit.transactions
            if format_name in CREDIT_FORMATS
            else scenario.checking.transactions
        )
        assert len(render(format_name, txns).splitlines()) == len(txns) + 1

    def test_wells_fargo_headerless_and_quoted(self, scenario):
        lines = render("wells_fargo", scenario.checking.transactions).splitlines()
        assert len(lines) == len(scenario.checking.transactions)  # no header
        assert all(line.startswith('"') and line.endswith('"') for line in lines)
        assert lines[0].split('","')[2] == "*"

    def test_bofa_preamble_before_header(self, scenario):
        lines = render("bofa_checking", scenario.checking.transactions).splitlines()
        assert lines[0] == "Description,,Summary Amt."
        assert lines[1].startswith("Beginning balance as of ")
        assert lines[3] == "Date,Description,Amount,Running Bal."

    def test_bmo_quirks(self, scenario):
        lines = render("bmo", scenario.checking.transactions).splitlines()
        header = lines[2]
        assert " Transaction Amount" in header  # leading space preserved
        first_row = lines[3].split(",")
        assert first_row[0] == "'5191830112345678'"
        assert len(first_row[2]) == 8 and first_row[2].isdigit()  # YYYYMMDD

    def test_ally_amounts_contain_dollar_signs(self, scenario):
        lines = render("ally", scenario.checking.transactions).splitlines()[1:]
        assert all("$" in line.split(",")[2] for line in lines)

    @pytest.mark.parametrize("format_name", ["amex", "discover", "apple_card"])
    def test_inverted_sign_convention(self, scenario, format_name):
        """Charges positive, payments negative on these issuers."""
        output = render(format_name, scenario.credit.transactions)
        amount_col = {"amex": 2, "discover": 3, "apple_card": 6}[format_name]
        for line, txn in zip(output.splitlines()[1:], scenario.credit.transactions, strict=True):
            rendered = Decimal(line.split(",")[amount_col])
            assert rendered == -txn.amount

    @pytest.mark.parametrize(
        ("format_name", "debit_col", "credit_col"),
        [("capital_one", 5, 6), ("citi", 3, 4)],
    )
    def test_debit_credit_split(self, scenario, format_name, debit_col, credit_col):
        for line in render(format_name, scenario.credit.transactions).splitlines()[1:]:
            cells = line.split(",")
            debit, credit = cells[debit_col], cells[credit_col]
            assert (debit == "") != (credit == "")  # exactly one populated
            populated = debit or credit
            assert Decimal(populated) > 0  # both conventions positive

    def test_venmo_amount_strings_and_transfer_rows(self, scenario):
        lines = render("venmo", scenario.venmo.transactions).splitlines()[1:]
        assert all(" $" in line.split(",")[7] for line in lines)
        transfers = [line for line in lines if "Standard Transfer" in line]
        assert len(transfers) == 3
        assert all("Chase Checking x1234" in line for line in transfers)

    def test_ofx_structure(self, scenario):
        output = render("ofx", scenario.checking.transactions)
        assert output.startswith("OFXHEADER:100")
        assert output.count("<STMTTRN>") == len(scenario.checking.transactions)
        assert output.count("<FITID>") == len(scenario.checking.transactions)


class TestWriteScenario:
    def test_writes_all_files(self, scenario, tmp_path):
        written = write_scenario(scenario, tmp_path / "exports")
        names = {p.name for p in written}
        assert len(written) == len(CHECKING_FORMATS) + len(CREDIT_FORMATS) + 1
        assert "ofx.ofx" in names
        assert "venmo.csv" in names
        assert all(p.exists() and p.stat().st_size > 0 for p in written)

    def test_every_format_in_registry_is_exercised(self):
        assert set(FORMATS) == {*CHECKING_FORMATS, *CREDIT_FORMATS, "venmo"}
