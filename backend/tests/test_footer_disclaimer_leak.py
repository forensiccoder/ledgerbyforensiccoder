"""Regression test for a real-world extraction bug: at least one bank (HDFC) prints a standard
legal disclaimer on every page ("Closing balance includes funds earmarked for hold and uncleared
funds. Contents of this statement will be considered correct if no error is reported within 30
days...") immediately after the page's own content, with no row/line boundary to separate it. PDF
text extraction sometimes drops the spaces between its words entirely
("Closingbalanceincludesfunds..."), which is also why layout.py's own _FOOTER pattern (built for
normally-spaced footer text) never recognised it - so the whole disclaimer ended up glued onto a
real transaction's narration (and from there, corrupting the counterparty pulled from it) on every
affected page.

No real statement or personal data is used here - this reproduces the pattern with a synthetic
narration and the bank's own recurring (space-stripped) wording.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.models import RawRow
from app.normalize import build_transactions

HEADER = RawRow(["Date", "Narration", "Reference", "Debit", "Credit", "Balance"])


def test_space_stripped_footer_disclaimer_is_cut_from_the_narration():
    narration = (
        "SHARMA-JOHNDOE99@OKICICI-ICIC0003618-051792657028-PAYMENTFROMPHONE UPI-AMITBELWANI "
        "HDFC BANK LIMITED *Closingbalanceincludesfundsearmarkedforholdandunclearedfunds "
        "Contentsofthisstatementwillbeconsideredcorrectifnoerrorisreportedwithin30daysofreceiptofstatement."
    )
    rows = [HEADER, RawRow(["01/07/25", narration, "01/07/25", "", "5,000.00", "12,921.23"])]
    result = build_transactions(rows, "test.pdf")
    assert len(result.transactions) == 1
    txn = result.transactions[0]
    assert "Closing" not in txn.narration
    assert "closingbalance" not in txn.narration.lower()
    assert txn.narration.startswith("SHARMA-JOHNDOE99@OKICICI")
