"""Regression tests for a photographed (phone-camera) bank statement, where the OCR text is noisy,
the table drifts vertically across the page, and some figures and dates are misread.

Synthetic data only.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import Word, _dewarp, _is_ocr_junk, _repair_ocr_dates, plan_ocr_columns
from app.models import Transaction
from app.reconcile import repair_from_balances


def _w(text: str, x0: float, y: float, width: float | None = None) -> Word:
    return Word(text, x0, x0 + (width if width is not None else 4.0 * len(text)), y - 4, y + 4)


def test_misread_dates_are_repaired_from_the_previous_row():
    words = [_w("15-06-2024", 20, 10), _w("46-06-2024", 20, 60), _w("1206-2024", 20, 110), _w("98-07-2024", 20, 160)]
    fixed, last = _repair_ocr_dates(words, None)
    assert fixed[1].text == "16-06-2024"
    assert fixed[2].text == "12-06-2024"
    assert fixed[3].text == "08-07-2024"
    assert last == date(2024, 7, 8)


def test_right_hand_columns_are_pulled_back_onto_their_rows_lines():
    words = []
    for k in range(8):
        y = 100 + k * 60
        drift = 8 - k * 2.5  # the right side of the photo sits lower, then higher
        words += [_w("1%d-06-2024" % k, 20, y), _w("x%d" % k, 100, y, 60)]
        words += [_w("%d,000.00" % (k + 1), 400, y + drift), _w("%d,500.45CR" % (k + 1), 520, y + drift)]
    fixed = _dewarp(words)
    for k in range(8):
        y = 100 + k * 60
        money = [w for w in fixed if w.text.startswith("%d," % (k + 1))]
        assert all(abs((w.top + w.bottom) / 2 - y) <= 1.0 for w in money)


def test_junk_scraps_are_recognised_but_bank_codes_and_references_are_not():
    assert all(_is_ocr_junk(t) for t in ("ei", "rn.", "vik", "La", "|"))
    assert not any(_is_ocr_junk(t) for t in ("WDL", "TFR", "AT", "UPI/DR/4530/SANJAY", "09278", "HANSAKA"))


def _txn(i: int, amount: str, balance: str | None, direction: str) -> Transaction:
    t = Transaction(id=f"t{i}", date=date(2024, 6, i + 1), amount=Decimal(amount), narration="x", source="s.pdf",
                    direction=direction, direction_source="column",
                    balance=Decimal(balance) if balance is not None else None, page=1, row=i)
    return t


def test_balance_chain_repairs_a_lookalike_digit_but_not_a_missing_row():
    rows = [_txn(0, "20.00", "4980.00", "Debit"), _txn(1, "4000.00", "3980.00", "Debit"), _txn(2, "10.00", "3970.00", "Debit")]
    # the second row's amount was read 4000 for 1000 (1 <-> 4); the balances say 1000
    repair_from_balances(rows, Decimal("5000.00"))
    assert rows[1].amount == Decimal("1000.00")

    gap = [_txn(0, "2000.00", "126.45", "Debit"), _txn(1, "10.00", "116.45", "Debit"), _txn(2, "10.00", "106.45", "Debit")]
    repair_from_balances(gap, Decimal("2626.45"))  # 500 + 2000 between the balances: a row is missing
    assert gap[0].amount == Decimal("2000.00")
