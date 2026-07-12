# Source Export Schemas

Real-world export formats that `personal_finance.synth` replicates and the dlt
pipelines ingest. Dummy data must match these column layouts exactly so the
ingestion code is exercised against realistic shapes.

> Formats verified against public documentation July 2026 (sources at bottom).
> Banks change exports occasionally — when a real file disagrees with this doc,
> trust the file and update this doc.

## Chase checking (CSV)

Downloaded from the account Activity page (desktop site only). MM/DD/YYYY dates;
single signed Amount column (negative = outflow).

```csv
Details,Posting Date,Description,Amount,Type,Balance,Check or Slip #
DEBIT,07/01/2026,TRADER JOE'S #123 SEATTLE WA,-42.50,DEBIT_CARD,1234.56,
CREDIT,07/03/2026,PAYROLL ACME CORP DIRECT DEP,2500.00,ACH_CREDIT,3734.56,
```

## Chase credit card (CSV)

Both a transaction date and a post date; Chase's own `Category` column ships in
the export (useful ground truth to compare our categorization against).

```csv
Transaction Date,Post Date,Description,Category,Type,Amount,Memo
06/30/2026,07/01/2026,TRADER JOE'S #123,Groceries,Sale,-42.50,
07/02/2026,07/02/2026,Payment Thank You - Web,,Payment,350.00,
```

## Venmo (CSV)

Statement page download, max 90 days per file. ISO-ish datetimes. Signed amounts
formatted like `+ $320.00` / `- $15.00` in `Amount (total)`. Transfers to a bank
appear as `Standard Transfer` rows — the transfer-detection case.

```csv
ID,Datetime,Type,Status,Note,From,To,Amount (total),Amount (fee),Funding Source,Destination
3187...001,2026-07-01T18:22:01,Payment,Complete,Dinner 🍜,Jane Doe,Sample User,+ $32.00,,Venmo balance,
3187...002,2026-07-02T09:10:44,Standard Transfer,Issued,,,,- $320.00,,Venmo balance,Chase Checking x1234
```

## BMO chequing (CSV)

From "Download Transactions" (also offers OFX). Note the quirks: masked card
number as first column, and dates as bare `YYYYMMDD` integers. Amount signed.

```csv
First Bank Card,Transaction Type,Date Posted, Transaction Amount,Description
'5191830112345678',DEBIT,20260701,-42.50,[SO]TRADER JOES #123
'5191830112345678',CREDIT,20260703,2500.00,[DN]ACME CORP PAYROLL
```

## Wells Fargo — checking / savings / credit (CSV)

One "Download Account Activity" flow for all account types. The distinctive
quirk: **no header row at all**, five fully-quoted columns —
date, signed amount, a status/cleared marker column (`*`), check number
(usually empty), description. Ingestion must supply column names itself.

```csv
"07/01/2026","-42.50","*","","TRADER JOE'S #123 SEATTLE WA"
"07/03/2026","2500.00","*","","ACME CORP PAYROLL 260703"
```

## Capital One credit card (CSV)

ISO dates and **separate Debit/Credit columns, both positive** — the first
source in our set with that convention. `Card No.` (last 4) distinguishes
cards on one account. Export window is only ~90 days.

```csv
Transaction Date,Posted Date,Card No.,Description,Category,Debit,Credit
2026-06-30,2026-07-01,1234,TRADER JOE'S #123,Merchandise,42.50,
2026-07-02,2026-07-02,1234,CAPITAL ONE AUTOPAY PYMT,Payment,,350.00
```

(The Capital One 360 *bank* CSV differs from the card export; verify against a
real file before building that source.)

## Bank of America — checking (CSV)

The parser-hostile one: the file opens with a **summary preamble** (description
line, beginning-balance rows, total credits/debits) before a blank line and the
real header. Ingestion must skip to the header row. MM/DD/YYYY, signed amounts.

```csv
Description,,Summary Amt.
Beginning balance as of 07/01/2026,,"1,234.56"
...
Date,Description,Amount,Running Bal.
07/01/2026,"TRADER JOE'S #123 07/01 PURCHASE",-42.50,"1,192.06"
```

Credit card exports use `Posted Date, Reference Number, Payee, Address, Amount`.

## American Express (CSV)

Multiple unversioned CSV layouts in the wild; the current basic activity export
is `Date, Description, Amount` (extended variants add Category and more).
**Sign convention is inverted vs. banks: charges are POSITIVE, payments
negative** — a deliberate test case for per-source sign normalization config.

```csv
Date,Description,Amount
07/01/2026,TRADER JOE'S #123 SEATTLE,42.50
07/05/2026,ONLINE PAYMENT - THANK YOU,-350.00
```

## Discover (CSV)

```csv
Trans. Date,Post Date,Description,Amount,Category
06/30/2026,07/01/2026,TRADER JOE'S #123,42.50,Supermarkets
07/05/2026,07/05/2026,DIRECTPAY FULL BALANCE,-350.00,Payments and Credits
```

Like Amex: purchases positive, payments negative. Ships a `Category` column
(more categorization ground truth). No OFX offered.

## Citi credit card (CSV)

`Status, Date, Description, Debit, Credit` (some exports add Member Name /
Post Date). Debit and Credit both positive, one populated per row; pending
transactions are excluded. Shortest export window of the majors (~90 days).

```csv
Status,Date,Description,Debit,Credit
Cleared,07/01/2026,TRADER JOES #123 SEATTLE WA,42.50,
Cleared,07/05/2026,ONLINE PAYMENT THANK YOU,,350.00
```

## US Bank (CSV)

`Date, Transaction, Name, Memo, Amount` — the `Transaction` column labels each
row `DEBIT`/`CREDIT` alongside a signed amount; payee in `Name`, detail in
`Memo`. MM/DD/YYYY. Download works in 90-day increments, ~18 months back.

## PNC (CSV)

Export button on the transaction list; includes a separate debit/credit type
column. Layout less consistently documented than the majors — verify against a
real file. ~90-day window per download.

## Ally (CSV)

`Date, Time, Amount, Type, Description` on checking; savings variants drop
Time. Notable quirk: amounts may include `$` symbols — ingestion must strip
currency symbols before numeric parsing. Also offers QFX.

## USAA (CSV)

`Date, Description, Original Description, Amount, Balance` (extra header rows
appear in some exports — another skip-to-header case). Signed single amount
column. ~18 months history; OFX also offered.

## Apple Card (CSV, monthly per-statement export from Wallet)

`Transaction Date, Clearing Date, Description, Merchant, Category, Type,
Amount`. Exported one statement month at a time from the iPhone Wallet app
(CSV/OFX/QFX/QBO). Clean format; charges positive, `Type` distinguishes
purchases from payments.

```csv
Transaction Date,Clearing Date,Description,Merchant,Category,Type,Amount (USD)
07/01/2026,07/02/2026,TRADER JOE'S #123 SEATTLE WA,Trader Joe's,Grocery,Purchase,42.50
```

## PayPal (Activity Download CSV)

The richest payment-app export: `Date, Time, TimeZone, Name, Type, Status,
Currency, Gross, Fee, Net, From Email Address, To Email Address, Transaction
ID, ...` (configurable field set; up to 7 years, 12 months per request).
Fee-aware (`Gross`/`Fee`/`Net`) and multi-currency — the test case for
currency handling and fee splits. Like Venmo, bank withdrawals appear as
transfer rows for transfer-detection.

## Cash App / Chime / smaller fintechs

Export layouts are poorly documented publicly; treat as **config-first
targets**: obtain a real file, add a `sources.yaml` entry, no code changes
expected. (Cash App exports a CSV with transaction ID/date/net amount fields;
Chime offers statement PDFs and limited CSV.) Do not ship synth fixtures for
these until verified against real exports.

## Amazon order history (Privacy Central export)

Amazon removed the old Order History Reports; data now comes from the
"Request your data" flow (privacy central) as a zip containing
`Retail.OrderHistory.1/Retail.OrderHistory.1.csv`. One row per *shipment item*
(not per order) — this is our line-item source for Amazon charges. Key columns:

```
Website Order ID, Order Date, Purchase Order Number, Currency, Unit Price,
Unit Price Tax, Shipping Charge, Total Discounts, Total Owed, Shipment Item
Subtotal, Shipment Item Subtotal Tax, ASIN, Product Condition, Quantity,
Payment Instrument Type, Order Status, Shipment Status, Ship Date, Shipping
Option, Shipping Address, Billing Address, Carrier Name & Tracking Number,
Product Name, Gift Message, Gift Sender Name, Gift Recipient Contact Details
```

Matching note: card statements show one charge per *shipment* (`Total Owed`
aggregated), so Amazon-order ↔ card-charge matching must group rows by order +
ship date.

## OFX / QFX

Standard format per the OFX spec (SGML-style for 1.x, XML for 2.x); Chase and
BMO both offer it. Key elements per transaction: `<STMTTRN>` with `<TRNTYPE>`,
`<DTPOSTED>`, `<TRNAMT>`, `<FITID>` (the source of our `external_id`), `<NAME>`,
optional `<MEMO>`. Synth should emit OFX 1.02 SGML — the ugliest common case.

## Costco warehouse receipt (for Phase 5 vision-LLM fixtures)

No export exists; in-app digital receipts only — hence photo → vision LLM.
Anatomy for synthetic receipt fixtures: header (warehouse name/number, date,
time, membership number); line items as `item_number (6–7 digits)` +
`ABBREVIATED NAME` (e.g. `KS DICED TOM`) + price + tax flag (commonly `A` =
taxable, `E` = exempt, varies by state); discount lines as negative amounts
referencing the original item number; then SUBTOTAL, TAX, TOTAL, payment
method, and item count. Abbreviated names are why receipt line items need the
NLP disambiguation stage.

## Future sources (design notes only — not yet scheduled)

Deliberately light: enough to confirm the current schema absorbs them without
redesign. Deep-dive when their phase arrives; verify formats against real
exports first.

### Google Pay / Google Wallet

Export via Google Takeout ("Google Pay Send" folder → transactions CSV; rewards
and gift cards arrive as PDFs) or a date-ranged CSV from the app/site. Fits the
existing payment-app pattern (Venmo/PayPal): a `sources.yaml` CSV entry plus
transfer-detection for bank top-ups. No schema impact.

### Paychecks / paystubs

Paystubs (ADP, Workday, Gusto, ...) are PDFs, not CSVs — they enter through the
**document pipeline like receipts**: upload → parse (vision LLM) → match to the
net-pay deposit transaction → decompose into splits. The schema already
supports this because splits are signed: a net deposit of $2,500 decomposes
into +$3,400 gross salary, −$540 taxes, −$210 health insurance, −$150 401(k),
summing to the transaction amount. Answering "how much of my income goes to
health insurance" is then the same query as "how much did I spend on apples".
Needs only taxonomy additions (`income/salary/gross`, `deductions/taxes`,
`deductions/health-insurance`, `savings/retirement`) — no new tables.

### Brokerages (Robinhood, Fidelity, Charles Schwab)

All three export activity CSVs (buys, sells, dividends, interest, transfers).
Cash-flow rows (deposits, withdrawals, dividends, interest) map onto
`transactions` today — a brokerage is just another `Account` of type
`investment`, and transfers in/out feed the existing transfer-detection.
What the schema does NOT yet model is *positions/holdings* (lots, cost basis,
market value over time); if portfolio tracking becomes a feature, that is a
new bounded context (holdings tables + price data), not a change to the
transaction core. Until then, ingest cash flows only.

## Extensibility: the capability matrix

The point of cataloguing fifteen formats is not to hardcode fifteen parsers —
it's that together they exhaust the variation space. A new bank should be a
`sources.yaml` entry, never new code. The capabilities the CSV ingestion layer
must support, each motivated by at least one real format above:

| Capability | Motivating formats |
|---|---|
| Column name mapping | all |
| Headerless files (positional columns) | Wells Fargo |
| Skip preamble rows / find header row | Bank of America, USAA |
| Date format per source (`%m/%d/%Y`, `%Y-%m-%d`, `YYYYMMDD` int, ISO datetime) | Chase, Capital One, BMO, Venmo |
| Sign convention: signed amount | Chase, Wells Fargo, BofA, US Bank, USAA, Ally |
| Sign convention: inverted (charges positive) | Amex, Discover, Apple Card |
| Sign convention: split Debit/Credit columns | Capital One, Citi, BMO-style two-column |
| Currency symbol / formatting cleanup (`$`, `+ $320.00`, thousands commas, quoted) | Ally, Venmo, BofA |
| Fee-aware gross/net amounts | PayPal |
| Multi-currency `Currency` column | PayPal, Amazon |
| Status column (drop pending) | Citi, Venmo |
| Multi-card attribution (last-4 column) | Capital One |
| External transaction ID for idempotency | Venmo, PayPal, OFX `FITID` |
| Issuer-provided category (keep as ground truth, never trust as ours) | Chase credit, Discover, Apple Card, Amex extended |

OFX/QFX is the escape hatch: most banks offer it, it's standardized, and one
OFX reader covers them all — prefer it where available; CSV configs cover the
rest (Discover and some fintechs are CSV-only).

## Sources

- Chase checking/credit CSV: [capyparse.com](https://capyparse.com/blog/convert-chase-bank-statement-to-csv-excel-qbo), [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-chase-jpmorgan-chase-transactions-to-csv-or-excel), [mindsbudget.com](https://www.mindsbudget.com/guides/export-chase-transactions-csv)
- Venmo CSV: [Venmo-History-CSV (GitHub)](https://github.com/Ryderpro/Venmo-History-CSV), [capyparse.com](https://capyparse.com/blog/convert-venmo-statement-to-csv-excel-qbo), [help.venmo.com](https://help.venmo.com/cs/articles/transaction-history-vhel281)
- Amazon Privacy Central export: [tiller.com](https://tiller.com/how-to-download-your-amazon-order-history-report/), [orderproanalytics.com](https://www.orderproanalytics.com/blog/how-to-download-your-amazon-order-history-report), [excel-checkbook.com](https://excel-checkbook.com/exporting-your-amazon-purchase-history-to-excel/)
- BMO CSV: [docuclipper.com](https://www.docuclipper.com/blog/convert-bmo-bank-statement-to-excel/), [financialcrooks.com](https://financialcrooks.com/how-download-transaction-history-bmo-online-bank-account/), [neontra.com](https://neontra.com/support/import-csv/bmo/)
- Costco receipt anatomy: [pricematcher.app](https://pricematcher.app/resources/how-to-read-costco-receipt), [costco-importer (GitHub)](https://github.com/garyhtou/costco-importer/blob/main/README.md)
- Wells Fargo CSV (headerless): [excel-checkbook.com](https://excel-checkbook.com/exporting-transactions-from-a-wells-fargo-bank-account-to-import-load-into-the-excel-checkbook-register/), [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-wells-fargo-transactions-to-csv-or-excel)
- Capital One CSV: [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-capital-one-transactions-to-csv-or-excel), [capyparse.com](https://capyparse.com/blog/convert-capital-one-statement-to-csv-excel-qbo)
- Bank of America CSV: [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-bank-of-america-transactions-to-csv-or-excel), [excel-checkbook.com](https://excel-checkbook.com/exporting-transactions-from-a-bank-of-america-checking-account-to-import-load-into-the-excel-checkbook-register/)
- Amex CSV: [amex2qif (GitHub)](https://github.com/jmcameron/amex2qif), [americanexpress.com](https://www.americanexpress.com/us/customer-service/faq.download-export-transactions-software.html), [capyparse.com](https://capyparse.com/blog/convert-amex-statement-to-csv-excel-qbo)
- Discover CSV: [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-discover-card-transactions-to-csv-or-excel), [capyparse.com](https://capyparse.com/blog/convert-credit-card-statements-to-csv)
- Citi CSV: [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-citi-citibank-credit-card-transactions-to-csv-or-excel), [ardenmoney.com](https://ardenmoney.com/guide/export-csv/citi/)
- US Bank CSV: [capyparse.com](https://capyparse.com/blog/convert-us-bank-statement-to-csv-excel-qbo), [usbank.com](https://www.usbank.com/customer-service/knowledge-base/KB0069323.html)
- PNC CSV: [docuclipper.com](https://www.docuclipper.com/blog/convert-pnc-bank-statement-to-excel/), [a1websitepro.com](https://a1websitepro.com/post/pnc-bank-download-csv-files-beyond-90-days-beginners-guide)
- Ally CSV: [capyparse.com](https://capyparse.com/blog/convert-ally-bank-statement-to-csv-excel-qbo), [ally.com](https://www.ally.com/help/bank/quicken/)
- USAA CSV: [capyparse.com](https://capyparse.com/blog/convert-usaa-statement-to-csv-excel-qbo), [excel-checkbook.com](https://excel-checkbook.com/exporting-transactions-from-a-usaa-checking-account-to-import-load-into-the-excel-checkbook-register/)
- Apple Card CSV: [support.apple.com](https://support.apple.com/en-us/102284), [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-apple-card-transactions-to-csv-or-excel), [apple-card-csv (GitHub)](https://github.com/kgryte/apple-card-csv)
- PayPal Activity Download: [developer.paypal.com](https://developer.paypal.com/docs/reports/online-reports/activity-download/), [paypal.com](https://www.paypal.com/us/cshelp/article/how-do-i-view-and-download-statements-and-reports-help145)
- Google Pay / Wallet export: [support.google.com](https://support.google.com/googlepay/answer/9015738?hl=en), [bankxlsx.com](https://bankxlsx.com/blog/can-i-export-google-pay-google-wallet-transactions-to-csv-or-excel)
