"""Regression test for real-world OCR bugs where the decimal point in an amount is misread:

  - as a comma (a genuine Indian-formatted integer's right-most group is always three digits,
    e.g. "1,50,000" - so a comma followed by exactly two digits at the very end is never valid
    grouping, e.g. "324928,39" should read as Rs 3,24,928.39, not Rs 3,24,92,839);
  - duplicated, so an actual thousands-grouping mark also comes out as a period (a real amount
    never has more than one, e.g. "1021.42.93" should read as Rs 1,02,142.93, not two decimals).

Both silently inflated the parsed amount by roughly 100x, which is worse than dropping the row -
a forensic reviewer trusts the number instead of being told to check it.

No real statement or personal data is used here - this reproduces the OCR artifact with synthetic
amounts.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import _fix_ocr_number


def test_comma_misread_as_decimal_point_is_corrected():
    assert _fix_ocr_number("324928,39") == "324928.39"


def test_duplicated_decimal_point_keeps_only_the_last_two_digits_as_paise():
    assert _fix_ocr_number("1021.42.93") == "102142.93"


def test_normal_amounts_are_left_unchanged():
    assert _fix_ocr_number("300000.00") == "300000.00"
    assert _fix_ocr_number("14996.00") == "14996.00"


def test_comma_misread_combines_correctly_with_a_trailing_ruling_line_artifact():
    assert _fix_ocr_number("301839,39|") == "301839.39"
