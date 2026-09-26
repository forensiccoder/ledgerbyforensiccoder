"""Regression test for real-world bugs on a Kotak statement: "Recd:IMPS/<rrn>/<name>/<bank>/<acct>/
<remark>" narrations named their counterparty "Recd:IMPS" (the first field) instead of the payer in
the third field, the printed cell wrapped inside the short bank-code / masked-account fields
("KKB K", "X26 50"), and UPI credit adjustments took their whole code as the counterparty.

Synthetic names and references only.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.models import RawRow
from app.narration import parse_narration
from app.normalize import build_transactions

HEADER = RawRow(["Date", "Description", "Chq/Ref. No.", "Debit", "Credit", "Balance"])


def test_slash_imps_counterparty_is_the_payer_name_field():
    info = parse_narration("Recd:IMPS/515623009576/ACME PAYME/KKBK/X0961/IMPS", direction="Credit")
    assert info.category == "IMPS"
    assert info.counterparty == "ACME PAYME"
    assert info.reference == "515623009576"


def test_wrap_inside_bank_code_and_masked_account_is_repaired():
    rows = [
        HEADER,
        RawRow(["07 Jun 2025", "Recd:IMPS/515623009576/ACME PAYME/KKB K/X0961/IMPS", "IMPS-1", "", "2,468.55", "2,733.86"]),
        RawRow(["08 Jun 2025", "Recd:IMPS/523915627554/joginder/KKBK/X26 50/Payou", "IMPS-2", "", "100.00", "2,833.86"]),
    ]
    txns = build_transactions(rows, "t.pdf").transactions
    assert txns[0].narration == "Recd:IMPS/515623009576/ACME PAYME/KKBK/X0961/IMPS"
    assert txns[1].narration == "Recd:IMPS/523915627554/joginder/KKBK/X2650/Payou"


def test_upi_credit_adjustment_has_no_counterparty_but_keeps_the_rrn():
    info = parse_narration("UPI_CRADJ_U2_TDT_050825_521761161884_ 06AUG2025_9C", direction="Credit")
    assert info.category == "UPI"
    assert info.reference == "521761161884"
    assert not info.counterparty
