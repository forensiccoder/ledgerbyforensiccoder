"""Regression test for a real-world extraction bug: a scan can genuinely have no header row on
any page - e.g. the page that carried it (a cover/summary page) wasn't included in the scan. The
word-layout engine used to require a header (or a header carried over from an earlier page) before
it would treat anything as a transaction row, so a headerless scan produced zero rows no matter
how good the OCR was.

It now falls back to inferring Date/Narration/Debit/Credit/Balance purely from word position and
the data itself when no header text can be found anywhere - and self-verifies the guess against
the running-balance math (``balance[i] == balance[i-1] -/+ amount[i]``) before trusting it, so a
table that doesn't actually reconcile is left unrecognised rather than mislabelled.

No real statement or personal data is used here - this reproduces the structural pattern (a
bordered table with no header, Date | Narration | Debit | Credit | Balance) with synthetic rows.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import LayoutState, Word, layout_page


def _word(text: str, x0: float, x1: float, top: float) -> Word:
    return Word(text, x0, x1, top, top + 10)


# Column bands: Date | Narration | Debit | Credit | Balance - no header row above any of them.
DATE_X, NARR_X, DEBIT_X, CREDIT_X, BAL_X = 20.0, 100.0, 310.0, 420.0, 530.0

# (debit, credit) per row; opening balance 1000.00. Six of each so both numeric columns clear the
# minimum-sample bar on their own.
_MOVEMENTS = [
    (100, None), (None, 200), (50, None), (None, 300), (150, None), (None, 400),
    (600, None), (None, 100), (250, None), (None, 500), (350, None), (None, 250),
]


def _headerless_words() -> list[Word]:
    words: list[Word] = []
    top = 0.0
    balance = 1000.0
    for day, (debit, credit) in enumerate(_MOVEMENTS, start=1):
        balance += (credit or 0) - (debit or 0)
        words.append(_word(f"{day:02d}/01/2024", DATE_X, DATE_X + 70, top))
        words.append(_word("PAYMENT REF" + str(day), NARR_X, NARR_X + 90, top))
        if debit is not None:
            words.append(_word(f"{debit:.2f}", DEBIT_X, DEBIT_X + 70, top))
        if credit is not None:
            words.append(_word(f"{credit:.2f}", CREDIT_X, CREDIT_X + 70, top))
        words.append(_word(f"{balance:.2f}", BAL_X, BAL_X + 70, top))
        top += 20.0
    return words


def test_headerless_table_infers_columns_and_keeps_debit_credit_separate():
    rows = layout_page(_headerless_words(), page=1, state=LayoutState(), ocr=True)
    assert rows, "expected the balance-verified fallback to recover rows with no header present"

    header = rows[0].cells
    body = rows[1:]
    assert len(body) == len(_MOVEMENTS)

    debit_i, credit_i, balance_i = header.index("Debit"), header.index("Credit"), header.index("Balance")
    for row, (debit, credit) in zip(body, _MOVEMENTS):
        assert row.cells[debit_i] == (f"{debit:.2f}" if debit is not None else "")
        assert row.cells[credit_i] == (f"{credit:.2f}" if credit is not None else "")
        assert row.cells[balance_i] != ""


def test_non_reconciling_headerless_table_is_left_unrecognised():
    """A column in the Balance position that never actually reconciles against the numbers next
    to it (unlike a real running balance) must not be guessed at - a wrong Debit/Credit/Balance
    label is worse than reporting nothing."""
    words: list[Word] = []
    top = 0.0
    for day, (debit, credit) in enumerate(_MOVEMENTS, start=1):
        words.append(_word(f"{day:02d}/01/2024", DATE_X, DATE_X + 70, top))
        words.append(_word("PAYMENT REF" + str(day), NARR_X, NARR_X + 90, top))
        if debit is not None:
            words.append(_word(f"{debit:.2f}", DEBIT_X, DEBIT_X + 70, top))
        if credit is not None:
            words.append(_word(f"{credit:.2f}", CREDIT_X, CREDIT_X + 70, top))
        words.append(_word("999.99", BAL_X, BAL_X + 70, top))  # constant - never reconciles
        top += 20.0

    rows = layout_page(words, page=1, state=LayoutState(), ocr=True)
    assert rows == []
