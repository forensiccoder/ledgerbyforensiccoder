"""Regression tests for the miscellaneous ("Other") rows of an HDFC statement: they named no
counterparty at all, and one RTGS row lost the middle of its narration because a reference number
that sat wider than its column header was mistaken for an amount and swallowed its whole line.

Synthetic names and references only.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import LayoutState, Word, layout_page
from app.narration import parse_narration


def _cp(text: str, direction: str = "Debit") -> tuple[str, str, str]:
    info = parse_narration(text, direction=direction)
    return info.category, info.channel, info.counterparty


def test_own_account_transfer_names_the_party():
    assert _cp("59244444222222-TPT-P-ACME LEASING COMPANY LIMITED") == ("Other", "Transfer (TPT)", "ACME LEASING COMPANY LIMITED")


def test_ach_debit_names_the_collector_but_a_bare_txn_ref_does_not():
    assert _cp("ACH D- BEST CAPITAL SERVICE-319091")[2] == "BEST CAPITAL SERVICE"
    assert _cp("ACH D- NACH-TXNREF18625011614501")[2] == ""


def test_fund_transfers_and_loan_credits_name_the_party():
    assert _cp("FT- RETURN1992323-99910822334455 - POWERLOOK APPARELS PVT LTD VASAI -", "Credit")[2] == "POWERLOOK APPARELS PVT LTD VASAI"
    assert _cp("FT- SBI MUTUAL FUND - SBIRED 32156876 036G", "Credit")[2] == "SBI MUTUAL FUND"
    assert _cp("A2AINT01--BAJAJFIN DISBURSEMENT-57500001168467-BAJAJ FINANCE LIMITED", "Credit")[2] == "BAJAJ FINANCE LIMITED"
    assert _cp("HARISH SHARMA -LOAN REC FROM HFC", "Credit")[2] == "HARISH SHARMA"


def test_rtgs_credit_names_the_remitter():
    text = "RTGS CR-AUBL0002165-PATRON LEASING AND FINANCE PRIVATE LIMIT-ASHISH SHARMA-AUBLR62025101619308067"
    assert _cp(text, "Credit") == ("Other", "RTGS", "PATRON LEASING AND FINANCE PRIVATE LIMIT")


def test_charges_and_interest_stay_unnamed():
    assert _cp("BRN CASH TXN CHGS INCL GST 260825-MIR2625260971976")[2] == ""
    assert _cp("INTEREST PAID TILL 30-SEP-2025", "Credit")[2] == ""


def _w(text: str, x0: float, top: float, width: float | None = None) -> Word:
    return Word(text, x0, x0 + (width if width is not None else 4.0 * len(text)), top, top + 8)


def test_reference_wider_than_its_header_is_not_mistaken_for_an_amount():
    ws: list[Word] = []
    top = 0.0
    for text, x in (("Date", 20), ("Narration", 100), ("Reference", 284), ("Debit", 340), ("Credit", 420), ("Balance", 500)):
        ws.append(_w(text, x, top))
    top += 17
    ws += [_w("16/10/25", 20, top), _w("RTGS CR-AUBL0002165-PATRON LEASING AND F", 100, top, 160), _w("AUBLR62025101619", 290, top), _w("1,393,747.00", 420, top), _w("1,405,175.61", 500, top)]
    top += 17
    # the wrapped narration line and the reference's tail share a line; the tail overhangs the header
    ws += [_w("INANCE PRIVATE LIMIT-ASHISH", 100, top, 100), _w("308067", 329, top, 24)]
    top += 17
    ws += [_w("SHARMA-AUBLR", 100, top)]
    rows = layout_page(ws, page=1, state=LayoutState(), ocr=False)
    narr_i = rows[0].cells.index("Narration")
    assert "INANCE PRIVATE LIMIT-ASHISH" in rows[1].cells[narr_i]
