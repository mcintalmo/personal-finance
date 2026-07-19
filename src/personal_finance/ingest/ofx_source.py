"""Parse an OFX/QFX export into canonical bronze rows.

OFX is a structured format (SGML in 1.x, XML in 2.x) parsed by ofxtools, so —
unlike CSV — no column mapping or sign convention is needed: ``TRNAMT`` is
already signed with negative = outflow (our convention), and every transaction
carries a ``FITID`` we surface as ``external_id`` (the natural idempotency key).
A single file may bundle several account statements; all are ingested.

Account name/type/currency for provenance come from the SourceConfig (the
user's chosen labels), consistent with the CSV path — the OFX file's own
CURDEF/ACCTTYPE are the bank's and may differ from what the user calls them.
"""

from collections.abc import Iterator
from datetime import UTC, datetime

# Path must be a REAL import (not TYPE_CHECKING-only): dlt's @dlt.resource
# introspects the decorated function's signature at import time. See
# csv_source.py for the full explanation.
from pathlib import Path

import dlt
from ofxtools.Parser import OFXTree

from personal_finance.exceptions import IngestionError
from personal_finance.ingest.dedup import compute_row_hash
from personal_finance.user_config import SourceConfig

BronzeRow = dict[str, object]


def read_ofx_transactions(source_name: str, file_path: Path) -> Iterator[dict[str, object]]:
    """Yield one canonical dict per transaction across all statements in a file.

    ``source_name`` keys each row's ``row_hash`` (the idempotency key), so it
    is scoped to this source and never collides with another account's
    identical activity.

    Raises:
        IngestionError: If the file cannot be parsed as OFX/QFX.
    """
    tree = OFXTree()
    try:
        tree.parse(str(file_path))
        ofx = tree.convert()
    except Exception as exc:  # ofxtools raises a family of parse/spec errors
        msg = f"{file_path}: not a parseable OFX/QFX file: {exc}"
        raise IngestionError(msg) from exc

    for statement in ofx.statements:
        # FITID is unique only within an account; scope the hash by the
        # statement's account id so two statements bundled in one file can't
        # collide on a shared id.
        account_id = getattr(statement.account, "acctid", None)
        for txn in statement.transactions:
            name = (txn.name or "").strip()
            memo = (txn.memo or "").strip()
            # NAME is the payee; MEMO is extra detail. Combine into the raw
            # description when MEMO adds something beyond NAME.
            description_raw = f"{name} {memo}".strip() if memo and memo != name else name
            posted_on = txn.dtposted.date()
            external_id = txn.fitid
            yield {
                "posted_on": posted_on,
                "amount": txn.trnamt,  # already signed: negative = outflow
                "description_raw": description_raw,
                "external_id": external_id,
                "row_hash": compute_row_hash(
                    source_name, posted_on, txn.trnamt, description_raw, external_id, account_id
                ),
            }


@dlt.resource(name="transactions", write_disposition="append")
def ofx_transactions(source: SourceConfig, file_path: Path) -> Iterator[BronzeRow]:
    """dlt resource yielding canonical bronze rows for one OFX/QFX file.

    Raises:
        IngestionError: If the file cannot be parsed (raised eagerly by
            ``read_ofx_transactions`` on the first pull).
    """
    ingested_at = datetime.now(UTC)
    for parsed in read_ofx_transactions(source.name, file_path):
        yield {
            "source": source.name,
            "account_name": source.account_name,
            "account_type": source.account_type.value,
            "currency": source.currency,
            "source_file": str(file_path),
            "ingested_at": ingested_at,
            **parsed,
        }
