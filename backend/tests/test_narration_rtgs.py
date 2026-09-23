"""Regression test for a real-world classification bug: an RTGS transfer's priority/urgency code
(e.g. "URGENT/CASH HIGH", printed by at least one real bank for a same-day RTGS) contains the bare
word "CASH" but is not a cash deposit - the money moved bank-to-bank, not into the account
holder's hand as cash. The narration classifier checked NEFT explicitly before falling back to a
bare "CASH" heuristic, but never checked RTGS the same way, so an RTGS row tripped that fallback
and was misclassified as a Cash deposit.

No real statement or personal data is used here - this reproduces the narration pattern with a
synthetic reference and payee name.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.narration import parse_narration


def test_rtgs_with_cash_priority_code_is_not_classified_as_cash_deposit():
    info = parse_narration(
        "RTGS/ICICR12026012208640152/JOHN DOE/ICICI BANK LIMITED//URGENT/CASH HIGH",
        direction="Credit",
    )
    assert info.category != "Cash deposit"
    assert info.channel == "RTGS"
