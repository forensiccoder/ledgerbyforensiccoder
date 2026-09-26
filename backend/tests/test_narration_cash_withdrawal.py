"""Regression test for a real-world extraction bug: at least one real bank (Fincare) prints a
bare "WTHDRL" as the entire narration for an over-the-counter cash withdrawal, with none of the
"cash"/"atm" wording the classifier otherwise requires. That row was silently dropped into the
"Other/Unclassified" bucket instead of being counted as a Cash withdrawal.

The same bank also writes "WTHDRL,CLG/<ref>/<payee name>" when a cheque is cleared to a third
party - that money didn't go to the account holder as cash, so it must stay out of Cash
withdrawal even though it also starts with "WTHDRL".

No real statement or personal data is used here - this reproduces the narration pattern with a
synthetic payee name.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.narration import parse_narration


def test_bare_wthdrl_is_classified_as_cash_withdrawal():
    info = parse_narration("WTHDRL", direction="Debit")
    assert info.category == "Cash withdrawal"
    assert info.channel == "Cash"
    assert info.confidence == "high"


def test_wthdrl_with_clearing_reference_is_not_cash_withdrawal():
    info = parse_narration("WTHDRL,CLG/000006/JOHN DOE", direction="Debit")
    assert info.category != "Cash withdrawal"
    assert info.channel == "Cheque"


def test_cash_handling_charges_are_fees_not_cash_withdrawals():
    from app.narration import parse_narration

    for text in ("BRN CASH TXN CHGS INCL GST 260825-MIR2626408043201", "INTER-BRN CASH CHG INCL GST 20-08-25 MIR2625261083355"):
        info = parse_narration(text, direction="Debit")
        assert info.category != "Cash withdrawal", text
        assert info.channel == "Charges", text
