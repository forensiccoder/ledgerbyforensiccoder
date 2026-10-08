"""Regression tests for an SBI statement layout that earlier code turned to nonsense: the Date
columns print "26 Aug" with the year ("2024") on the line below, the Value Date header wraps
("Value" over "Date") and used to be read as an amount column, and the narration is hard-wrapped at
25 characters, sometimes in the middle of a word ("RAKES" / "H K").

Synthetic names and numbers only.
"""
from __future__ import annotations

import sys
from datetime import date

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import (
    LayoutState, Word, _date_spans, _fill_missing_day, _repair_month_tokens, _split_glued_day_month,
    find_header, group_lines, layout_page,
)
from app.models import RawRow
from app.normalize import build_transactions
from app.ocr_cleanup import _tidy_transfer_narration

DATE_X, VAL_X, DESC_X, REF_X, DEBIT_X, CREDIT_X, BAL_X = 30.0, 100.0, 160.0, 300.0, 400.0, 460.0, 520.0


def w(text: str, x0: float, y: float, width: float | None = None) -> Word:
    return Word(text, x0, x0 + (width if width is not None else 5.0 * len(text)), y - 4, y + 4)


def _header(y: float) -> list[Word]:
    return [
        w("Txn", 30, y), w("Date", 52, y), w("Value", 100, y), w("Description", 160, y), w("Ref", 300, y), w("No./Cheque", 318, y),
        w("Debit", 400, y), w("Credit", 460, y), w("Balance", 520, y),
        w("Date", 102, y + 10), w("No.", 300, y + 10),
    ]


def _row(y: float, day: str, month: str, desc: tuple[str, ...], amount: str, balance: str) -> list[Word]:
    words = [w(day, 30, y), w(month, 42, y), w(day, 100, y), w(month, 112, y)]
    words += [w("TO", 160, y), w("TRANSFER-", 175, y), w("TRANSFER", 300, y), w("TO", 345, y), w(amount, 400, y), w(balance, 520, y)]
    words += [w("2024", 38, y + 10), w("2024", 108, y + 10), w("4897691162095", 300, y + 10)]
    for k, text in enumerate(desc):
        words.append(w(text, 160, y + 10 + 10 * k, 5.0 * len(text)))
    return words


def test_wrapped_value_date_header_is_not_an_amount_column():
    lines = group_lines(_header(10))
    found = find_header(lines)
    assert found is not None
    roles = {c.role for c in found[2]}
    assert "value_date" in roles and "amount" not in roles
    assert {"date", "narration", "debit", "credit", "balance"} <= roles


def test_yearless_day_month_is_a_date_and_the_next_columns_day_is_not_its_year():
    words = [w("26", 30, 0), w("Aug", 42, 0), w("26", 100, 0), w("Aug", 112, 0)]
    spans = _date_spans(group_lines(words)[0].words)
    assert spans == [(0, 1), (2, 3)]
    assert _date_spans(group_lines([w("26", 30, 0), w("Aug", 42, 0), w("2024", 58, 0)])[0].words) == [(0, 2)]


def test_year_printed_under_the_day_and_month_completes_the_date_and_wrapped_name_is_joined():
    words = _header(10)
    y = 40.0
    first = ("UPI/DR/424053107045/RAKES", "H K/YESB/paytmqr14i/UPI-")  # 25 characters: cut mid-name
    second = ("UPI/DR/424023982357/ANGAD", "KUM/YESB/paytmqr281/UPI-")  # 25 characters: ends at a word
    for k in range(8):  # enough lines for the 25-character ceiling to show
        words += _row(y, "27", "Aug", first if k % 2 == 0 else second, "50.00", f"{27000 - k * 50}.00")
        y += 50
    rows = layout_page(words, page=1, state=LayoutState(), ocr=False)
    narr_i = rows[0].cells.index("Narration")
    date_i = rows[0].cells.index("Date")
    assert rows[1].cells[date_i] == "27 Aug 2024"
    assert "RAKESH K/YESB" in rows[1].cells[narr_i]
    assert "ANGAD KUM/YESB" in rows[2].cells[narr_i]
    assert "2024" not in rows[1].cells[narr_i]


def test_month_and_day_damage_from_ocr_is_repaired():
    words = [w("4Nov", 30, 0), w("2024", 58, 0), w("10", 30, 40), w("Oct!", 42, 40), w("12", 30, 80), w("OctITO", 42, 80, 30), w("-22Sep|", 30, 120)]
    texts = [x.text for x in _split_glued_day_month(_repair_month_tokens(words))]
    assert texts[:3] == ["4", "Nov", "2024"]
    assert "Oct" in texts and "Sep" in texts and "TO" in texts


def test_missing_day_is_borrowed_from_the_value_date():
    words = [w("Oct", 42, 0), w("2024", 58, 0), w("5", 100, 0), w("Oct", 112, 0), w("2024", 128, 0)]
    filled = _fill_missing_day(words)
    assert any(x.text == "5" and x.x0 < 42 for x in filled)


def test_date_without_a_year_is_completed_from_the_previous_row_or_the_value_date():
    header = RawRow(["Date", "Value Date", "Narration", "Debit", "Credit", "Balance"])
    rows = [
        header,
        RawRow(["26 Aug 2024", "26 Aug 2024", "UPI x", "10.00", "", "90.00"]),
        RawRow(["22 Sep", "", "UPI y", "10.00", "", "80.00"]),
        RawRow(["45 Oct 2024", "15 Oct", "UPI z", "10.00", "", "70.00"]),
    ]
    txns = build_transactions(rows, "t.pdf").transactions
    assert [t.date for t in txns] == [date(2024, 8, 26), date(2024, 9, 22), date(2024, 10, 15)]


def test_transfer_narration_drops_date_fragments_and_repairs_the_reference():
    assert _tidy_transfer_narration("«22. TO TRANSFER- 2024 UPI/DR/4241989990S7/MUNNI DEVI/YESB/p/Pay-") == \
        "TO TRANSFER- UPI/DR/424198999057/MUNNI DEVI/YESB/p/Pay-"
    assert _tidy_transfer_narration("TO TRANSFER- UPI/DR/432141373747VIJAYP AL/KKBK/8860/Pay-") == \
        "TO TRANSFER- UPI/DR/432141373747/VIJAYP AL/KKBK/8860/Pay-"
