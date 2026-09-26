"""Regression test for a real-world extraction bug: at least one bank ("App Banking") prints a
zero-padded 16-digit UTR/reference number directly inside the Debit cell itself, ahead of the real
amount when the row is actually a debit ("0000120567891749 2,520.00"), and with nothing following
it at all when the row is really a credit - the bare reference still sits there alone
("0000105526161947").

This broke two ways at once:
  - On a credit row, the bare zero-padded reference parsed successfully as a huge bogus amount
    (six trillion-plus rupees for what was really a small transaction), which then won a
    "both debit and credit filled" tie-break over the real credit value.
  - On a debit row, the compound "<reference> <amount>" string didn't match the money pattern at
    all (the embedded space isn't valid inside a number), so the row's amount came back as
    completely unparseable and the whole transaction was silently dropped - not miscategorised,
    just gone, with no warning. This is why every "Cash withdrawal" row in the real file was
    invisible until this was fixed.

No real statement or personal data is used here - this reproduces the column pattern with a
synthetic reference and payee name.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.models import RawRow
from app.normalize import build_transactions

HEADER = RawRow(["Date", "Narration", "Reference", "Debit", "Credit", "Balance"])


def test_bare_padded_reference_does_not_become_a_bogus_amount():
    rows = [
        HEADER,
        RawRow(["01/07/25", "UPI-JOHN DOE-9999999999@ybl", "01/07/25", "0000105526161947", "40,000.00", "40,000.00"]),
    ]
    result = build_transactions(rows, "test.pdf")
    assert len(result.transactions) == 1
    txn = result.transactions[0]
    assert txn.direction == "Credit"
    assert txn.amount == 40000


def test_padded_reference_with_a_trailing_debit_amount_is_recovered():
    rows = [
        HEADER,
        RawRow(["01/07/25", "UPI-JANE DOE-8888888888@ybl", "01/07/25", "0000120567891749 2,520.00", "", "37,480.00"]),
    ]
    result = build_transactions(rows, "test.pdf")
    assert len(result.transactions) == 1
    txn = result.transactions[0]
    assert txn.direction == "Debit"
    assert txn.amount == 2520
