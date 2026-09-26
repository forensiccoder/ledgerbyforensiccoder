"""Regression tests for a Rajasthan Marudhara Gramin Bank scan: narrations look like
"WDL TFR:UPI <rrn> <handle>" and "By Transfer:UPI <rrn> <name> <handle>" - the bank's own
transaction-type prefix ("WDL TFR", often misread as "WOL TER") was taken for the counterparty, the
money columns had lost their decimal point ("5000 00"), and handles came out with a stray space and
look-alike suffixes ("8058332931 @ybI"). Synthetic references only.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import Word, _merge_split_money
from app.narration import parse_narration
from app.normalize import _repair_vpa


def test_bank_type_prefix_is_not_the_counterparty():
    for text in ("WDL TFR:UPI 330508467599 Q209442456@ybl", "WOL TER UPI 330508467599 Q209442456@ybl", "WODL TFR.UPI 330508467599 Q209442456@ybl"):
        info = parse_narration(text, direction="Debit")
        assert (info.category, info.counterparty, info.reference) == ("UPI", "Q209442456@ybl", "330508467599"), text


def test_credit_transfers_and_bare_labels():
    assert parse_narration("By Transfer:UPI 329722740919 rajesh nice1992@ybl", "Credit").counterparty == "rajesh"
    assert parse_narration("By Transfer:NEFT ICIC0SF0002 34129517521DC PRACHI DAIRY", "Credit").counterparty == "PRACHI DAIRY"
    named = parse_narration("By Transfer:PRACHI DAIRY", "Credit")
    assert (named.category, named.counterparty) == ("Other", "PRACHI DAIRY")
    assert parse_narration("WDL TFR", "Debit").counterparty == ""


def test_handle_spacing_and_lookalike_suffixes_are_repaired():
    assert _repair_vpa("WDL TFR:UPI 334393181565 8058332931 @ybI:") == "WDL TFR:UPI 334393181565 8058332931@ybl:"
    assert _repair_vpa("x Q985205763@ypbI 5") == "x Q985205763@ybl 5"


def test_lost_decimal_point_is_restored_in_the_money_columns_only():
    def w(text, x0, y=100):
        return Word(text, x0, x0 + 4 * len(text), y - 4, y + 4)

    words = [w("5000", 400), w("00", 421), w("803", 520), w("27Cr", 540), w("WDL", 100), w("12", 130)]
    merged = [x.text for x in _merge_split_money(words)]
    assert "5000.00" in merged and "803.27Cr" in merged
    assert "WDL" in merged and "12" in merged  # narration column untouched
