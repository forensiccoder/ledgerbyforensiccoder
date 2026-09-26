"""Regression test for a real-world extraction bug: a narration-only line immediately before a
dated row could be swallowed by whichever transaction happened to be open, even when that open
transaction's own date line already carried complete narration of its own and had no use for more.

At least one real bank (RTGS entries on an Axis-linked statement) wraps a long narration across two
lines *before* its date line, with the date line itself carrying only the trailing fragment (e.g.
"...LIMITED//URGENT/CASH HIGH") - so the date line always has *some* narration of its own, and the
old "does the next row already have narration?" check got this backwards: it kept the prefix lines
attached to the previous (already self-contained) row instead of the one that needed them, silently
corrupting that previous row's narration and leaving the real row's narration truncated to just its
trailing fragment - which then couldn't be recognised as an RTGS transfer at all.

The fix looks at whether the *open* row already has its own narration (it doesn't need more,
so free the prefix run for what follows) rather than whether the *upcoming* row does.

No real statement or personal data is used here - this reproduces the structural pattern with
synthetic narration text.
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
    # a normal, complete, self-narrated row (own date line carries its own full narration)
    line(
        _word("15/04/2022", DATE_X, DATE_X + 70, top),
        _word("AXISDIRECT/REF001", NARR_X, NARR_X + 90, top),
        _word("20,000.00", DEBIT_X, DEBIT_X + 70, top),
        _word("1,60,468.00", BAL_X, BAL_X + 70, top),
    )
    # the RTGS row's narration wraps across two lines *before* its own date line. In a real
    # bottom-aligned table the previous row's cell ends with padding before the next row's text
    # starts - that extra gap above the run (versus the tight spacing to its own date line below it)
    # is what says the run belongs to the row that follows.
    top += 14.0
    line(_word("RTGS/REF002", NARR_X, NARR_X + 60, top))
    line(_word("JOHN DOE/BANK", NARR_X, NARR_X + 70, top))
    # its own date line still carries the trailing fragment of that same narration
    line(
        _word("16/04/2022", DATE_X, DATE_X + 70, top),
        _word("LIMITED/URGENT", NARR_X, NARR_X + 60, top),
        _word("5,000.00", CREDIT_X, CREDIT_X + 70, top),
        _word("1,65,468.00", BAL_X, BAL_X + 70, top),
    )
    return words


def test_prefix_run_attaches_to_the_row_that_needs_it_not_the_self_narrated_one_before_it():
    rows = layout_page(_statement_words(), page=1, state=LayoutState(), ocr=False)
    header = rows[0].cells
    narr_i = header.index("Narration")

    first, second = rows[1], rows[2]
    assert first.cells[narr_i] == "AXISDIRECT/REF001"
    assert second.cells[narr_i] == "RTGS/REF002 JOHN DOE/BANK LIMITED/URGENT"


def _plain_statement_words(*, second_page: bool = False) -> list[Word]:
    """A text statement: every row's continuation lines are printed *below* its date line, and
    line spacing is perfectly uniform - nothing but the convention says who owns a wrapped line."""
    words: list[Word] = []
    top = 0.0

    def line(*ws: Word) -> None:
        nonlocal top
        words.extend(ws)
        top += 17.0

    if second_page:
        line(_word("ECOMPANYLIMITED", NARR_X, NARR_X + 90, top))  # spill-over from the previous page
    else:
        line(
            _word("Date", DATE_X, DATE_X + 40, top),
            _word("Narration", NARR_X, NARR_X + 60, top),
            _word("Debit", DEBIT_X, DEBIT_X + 40, top),
            _word("Credit", CREDIT_X, CREDIT_X + 40, top),
            _word("Balance", BAL_X, BAL_X + 50, top),
        )
    line(
        _word("03/07/25", DATE_X, DATE_X + 70, top),
        _word("CASHDEPOSIT-XXXX1641-GOVIND", NARR_X, NARR_X + 150, top),
        _word("8,000.00", CREDIT_X, CREDIT_X + 70, top),
        _word("28,020.65", BAL_X, BAL_X + 70, top),
    )
    line(_word("NAGAR", NARR_X, NARR_X + 40, top))
    line(_word("JORAWARSINGHGATE JPR", NARR_X, NARR_X + 120, top))
    line(
        _word("03/07/25", DATE_X, DATE_X + 70, top),
        _word("UPI-JITENDRAKUMAR", NARR_X, NARR_X + 110, top),
        _word("15,000.00", DEBIT_X, DEBIT_X + 70, top),
        _word("13,020.65", BAL_X, BAL_X + 70, top),
    )
    line(_word("SHARM-9785355777-3@YB", NARR_X, NARR_X + 130, top))
    return words


def test_uniformly_spaced_continuation_lines_stay_with_the_row_above_them():
    rows = layout_page(_plain_statement_words(), page=1, state=LayoutState(), ocr=False)
    narr_i = rows[0].cells.index("Narration")
    assert rows[1].cells[narr_i] == "CASHDEPOSIT-XXXX1641-GOVIND NAGAR JORAWARSINGHGATE JPR"
    assert rows[2].cells[narr_i] == "UPI-JITENDRAKUMAR SHARM-9785355777-3@YB"


def test_narration_spilling_onto_the_next_page_rejoins_the_row_it_belongs_to():
    state = LayoutState()
    first = layout_page(_plain_statement_words(), page=1, state=state, ocr=False)
    narr_i = first[0].cells.index("Narration")
    layout_page(_plain_statement_words(second_page=True), page=2, state=state, ocr=False)
    # page 2's leading line is the tail of page 1's last row, not the start of page 2's first row
    assert first[-1].cells[narr_i] == "UPI-JITENDRAKUMAR SHARM-9785355777-3@YB ECOMPANYLIMITED"
