"""Regression test for a real-world OCR bug: a table's vertical ruling line next to a numeric
column is often misread by Tesseract as a stray "|" (or an "l"/"I" look-alike) glued directly onto
the end of the number with no space, e.g. "801601.67|" instead of "801601.67".

Two separate places needed to tolerate this:
  - ``parse_money``/``is_money_like`` used to reject the value outright (it didn't match the
    money pattern at all), silently dropping the row's amount.
  - ``_fix_ocr_number`` already had a *deliberate* rule translating a "|" into the digit "1", for
    the different case of OCR misreading an actual "1" digit as "|" in the middle of a number -
    but it applied that blindly even when the amount was already complete (two paise digits) and
    the "|" was trailing noise, turning "801601.67|" into the wrong value "801601.671" instead of
    the correct "801601.67".

No real statement or personal data is used here - this reproduces the artifact with a synthetic
amount.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from decimal import Decimal

from app.layout import _fix_ocr_number
from app.money import is_money_like, parse_money


def test_trailing_ruling_line_pipe_does_not_break_money_parsing():
    assert is_money_like("801601.67|")
    assert parse_money("801601.67|").value == Decimal("801601.67")


def test_fix_ocr_number_strips_trailing_ruling_line_artifact_instead_of_treating_it_as_a_digit():
    assert _fix_ocr_number("801601.67|") == "801601.67"
    assert _fix_ocr_number("801601.67l") == "801601.67"
    assert _fix_ocr_number("801601.67I") == "801601.67"


def test_fix_ocr_number_still_treats_an_interior_pipe_as_a_misread_digit():
    # A "|" *inside* the number (not trailing, and not already a complete two-paise amount right
    # before it) is still the pre-existing digit-confusable case, e.g. "l,234.00" -> "1,234.00".
    assert _fix_ocr_number("|,234.00") == "1,234.00"
