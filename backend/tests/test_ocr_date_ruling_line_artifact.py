"""Regression test for a real-world OCR bug: the same ruling-line misread that glues a stray
"|"/"l"/"I" onto an amount (see test_ocr_ruling_line_artifact.py) can also glue a stray "]" or
"[" onto a date, e.g. "12-01-2026]" instead of "12-01-2026". Strict date matching required the
whole token to be nothing but the date, so that one stray character made the token fail to parse
as a date at all - and since the row builder treats a line with no date as a continuation of
whatever transaction came before it, the entire row (its narration AND its amount) was silently
lost: the narration got glued onto the wrong transaction, and its amount was dropped outright as
"a line with amounts but no date".

No real statement or personal data is used here - this reproduces the OCR artifact with a
synthetic date.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from datetime import date

from app.money import parse_date


def test_trailing_ruling_line_artifact_does_not_break_strict_date_matching():
    assert parse_date("12-01-2026]", strict=True) == date(2026, 1, 12)
    assert parse_date("12-01-2026[", strict=True) == date(2026, 1, 12)
    assert parse_date("12-01-2026|", strict=True) == date(2026, 1, 12)


def test_plain_date_is_unaffected():
    assert parse_date("12-01-2026", strict=True) == date(2026, 1, 12)


def test_genuinely_non_date_text_is_still_rejected():
    assert parse_date("AXISDIRECT/12-01-2026/extra", strict=True) is None
