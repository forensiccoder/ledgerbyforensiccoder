"""Regression test for a real-world extraction bug: at least one real bank (Fincare) prints a long
narration split around its own date/amount line instead of below it - the narration's opening
words appear on the line ABOVE the date+amount line, and its closing words on the line BELOW,
e.g.:

    15/04/2022 Opening Balance                              1,80,468.00
    NEFT OUT NEFT/FSFBH22601183446/Suresh
    15/04/2022                                20,000.00      1,60,468.00
    Khanna/IOBA0000442/044201000001213/
    30/04/2022 INT TFR IN,TFR-20750000444685                 7,669.00   1,68,137.00

The word-layout engine used to treat every narration-only line (one with no date) as a
continuation of the *previous* transaction, which glued "NEFT OUT ..." onto the Opening Balance
row and left the real NEFT transaction's narration column blank - hiding that transaction from
narration-based classification entirely. It now looks one line ahead: a narration-only line
immediately followed by a date+amount line that has no narration text of its own is treated as
the START of that next transaction instead.

No real statement or personal data is used here - this reproduces the structural pattern with
synthetic words.
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
    line(
        _word("15/04/2022", DATE_X, DATE_X + 70, top),
        _word("Opening", NARR_X, NARR_X + 50, top),
        _word("Balance", NARR_X + 55, NARR_X + 100, top),
        _word("1,80,468.00", BAL_X, BAL_X + 70, top),
    )
    # narration-only line: the START of the next transaction's narration, printed before its
    # own date+amount line
    line(_word("NEFT", NARR_X, NARR_X + 40, top), _word("OUT", NARR_X + 45, NARR_X + 75, top),
         _word("REF12345", NARR_X + 80, NARR_X + 150, top))
    # the transaction's own date+amount line - no narration text of its own
    line(
        _word("15/04/2022", DATE_X, DATE_X + 70, top),
        _word("20,000.00", DEBIT_X, DEBIT_X + 70, top),
        _word("1,60,468.00", BAL_X, BAL_X + 70, top),
    )
    # narration-only line: the continuation, printed after the date+amount line
    line(_word("XYZ", NARR_X, NARR_X + 30, top), _word("TRADERS", NARR_X + 35, NARR_X + 100, top))
    # a normal, unaffected row with its own narration
    line(
        _word("30/04/2022", DATE_X, DATE_X + 70, top),
        _word("INT", NARR_X, NARR_X + 30, top),
        _word("CREDIT", NARR_X + 35, NARR_X + 80, top),
        _word("7,669.00", CREDIT_X, CREDIT_X + 70, top),
        _word("1,68,137.00", BAL_X, BAL_X + 70, top),
    )
    return words


def test_narration_wrapped_around_amount_line_is_reassembled():
    rows = layout_page(_statement_words(), page=1, state=LayoutState())
    by_narration = {r.cells[1]: r.cells for r in rows[1:]}  # skip the synthetic header row

    assert "Opening Balance" in by_narration
    assert by_narration["Opening Balance"][2] == ""  # no debit
    assert by_narration["Opening Balance"][4] == "1,80,468.00"

    neft_key = "NEFT OUT REF12345 XYZ TRADERS"
    assert neft_key in by_narration, list(by_narration)
    neft_row = by_narration[neft_key]
    assert neft_row[0] == "15/04/2022"
    assert neft_row[2] == "20,000.00"  # debit
    assert neft_row[4] == "1,60,468.00"  # balance

    assert "INT CREDIT" in by_narration
    assert by_narration["INT CREDIT"][3] == "7,669.00"  # credit


if __name__ == "__main__":
    test_narration_wrapped_around_amount_line_is_reassembled()
    print("all tests passed")
