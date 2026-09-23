"""Regression test for a real-world extraction bug: at least one real bank's scan splits a
transaction's date+narration onto one OCR line and its Debit/Credit/Balance onto the very next
line (a baseline-jitter line-split, the same root cause as the narration-wrapping bug elsewhere in
this module, just affecting the amount instead of the narration).

That amount-only line used to always be treated as stray junk and discarded with a warning - which
silently dropped the row's Debit/Credit/Balance. Combined with normalize.py's "no amount, no
transaction" rule (a row needs *some* amount to become a Transaction at all), that made the entire
transaction disappear from the output with no trace - not even counted among the rows kept out of
the review list, unlike a merely-misclassified row.

It's now recovered: an amount-only line is only discarded when the open row already has amounts of
its own (still stray junk in that case); otherwise it supplies the value that row was missing.

No real statement or personal data is used here - this reproduces the structural pattern with
synthetic narration and amounts.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import LayoutState, Word, layout_page


def _word(text: str, x0: float, x1: float, top: float) -> Word:
    return Word(text, x0, x1, top, top + 10)


# Column bands: Date | Narration | Debit | Credit | Balance
DATE_X, NARR_X, DEBIT_X, CREDIT_X, BAL_X = 20.0, 100.0, 310.0, 380.0, 450.0


def _statement_words() -> list[Word]:
    words: list[Word] = []
    top = 0.0

    def line(*ws: Word) -> None:
        nonlocal top
        words.extend(ws)
        top += 20.0

    line(
        _word("Date", DATE_X, DATE_X + 40, top),
        _word("Narration", NARR_X, NARR_X + 60, top),
        _word("Debit", DEBIT_X, DEBIT_X + 40, top),
        _word("Credit", CREDIT_X, CREDIT_X + 40, top),
        _word("Balance", BAL_X, BAL_X + 50, top),
    )
    # a normal, complete row - own line carries its own narration and amount
    line(
        _word("15/04/2022", DATE_X, DATE_X + 70, top),
        _word("AXISDIRECT/REF001", NARR_X, NARR_X + 90, top),
        _word("20,000.00", DEBIT_X, DEBIT_X + 70, top),
        _word("1,60,468.00", BAL_X, BAL_X + 70, top),
    )
    # the next row's date+narration line has no amount of its own...
    line(
        _word("16/04/2022", DATE_X, DATE_X + 70, top),
        _word("AXISDIRECT/REF002", NARR_X, NARR_X + 90, top),
    )
    # ...it's split onto the following line instead
    line(
        _word("30,000.00", DEBIT_X, DEBIT_X + 70, top),
        _word("1,30,468.00", BAL_X, BAL_X + 70, top),
    )
    return words


def test_amount_only_line_supplies_the_value_the_open_row_was_missing():
    rows = layout_page(_statement_words(), page=1, state=LayoutState(), ocr=False)
    header = rows[0].cells
    debit_i, bal_i = header.index("Debit"), header.index("Balance")

    assert len(rows) == 3  # header + 2 transactions, not 3 (the split amount line adds no row)
    first, second = rows[1], rows[2]
    assert first.cells[debit_i] == "20,000.00"
    assert first.cells[bal_i] == "1,60,468.00"
    assert second.cells[debit_i] == "30,000.00"
    assert second.cells[bal_i] == "1,30,468.00"
