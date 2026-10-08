"""Regression tests for payee names damaged by OCR on a flatbed-scanned HDFC statement: a stray
quote inside a name, a symbol where the last letter belongs, a letter pair read as one ("VUAY" for
"VIJAY"), a fund-transfer row that lost its "FT -" prefix, and an amount whose leading 5 came out as
"$". Synthetic names and numbers only.
"""
from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import Word, _fix_symbol_for_five
from app.models import Transaction
from app.narration import parse_narration
from app.normalize import _repair_vpa
from app.ocr_cleanup import clean_ocr_narrations


def _txn(i: int, narration: str) -> Transaction:
    return Transaction(id=f"t{i}", date=date(2026, 5, 1 + i % 28), amount=Decimal("10.00"), narration=narration,
                       source="s.pdf", direction="Debit", direction_source="column", page=1, row=i)


def _cleaned(*narrations: str) -> list[str]:
    txns = [_txn(i, n) for i, n in enumerate(narrations)]
    clean_ocr_narrations(txns)
    return [t.narration for t in txns]


def test_stray_quote_before_a_word_is_dropped_but_a_real_apostrophe_stays():
    out = _cleaned("UPI-ASHA 'RAO-98@YBL-HDFC0001-1-PAYMENT", "UPI-ASHA ‘RAO-98@YBL-HDFC0001-2-PAYMENT", "UPI-JOHN D'SOUZA-98@YBL-HDFC0001-3-PAYMENT")
    assert out[0].startswith("UPI-ASHA RAO-") and out[1].startswith("UPI-ASHA RAO-")
    assert out[2].startswith("UPI-JOHN D'SOUZA-")


def test_trailing_symbol_becomes_the_letter_other_rows_prove_or_is_dropped():
    out = _cleaned("UPI-MEERA SHAH S-Q1@YBL-A-1-PAY", "UPI-MEERA SHAH $-Q1@YBL-A-2-PAY", "UPI-MEERA SHAH 8-Q1@YBL-A-3-PAY",
                   "UPI-LONE NAME $-Q2@YBL-A-4-PAY")
    assert [o.split("-")[1] for o in out] == ["MEERA SHAH S", "MEERA SHAH S", "MEERA SHAH S", "LONE NAME"]


def test_lookalike_letters_are_corrected_from_the_rest_of_the_statement():
    out = _cleaned("UPI-VUAY KUMAR-91@PTYES-BARB0-1-PAY", "CHQDEP CLG VIJAY PAL BHATI", "UPI-MUKESH SHAH-91@YBL-BARB0-2-PAY")
    assert out[0].split("-")[1] == "VIJAY KUMAR"
    assert out[2].split("-")[1] == "MUKESH SHAH"  # a name that is simply itself is left alone


def test_leading_punctuation_is_stripped_from_a_row():
    assert _cleaned(".UPI-RAKESH CHAUHAN SBIN001-PAYMENT")[0].startswith("UPI-RAKESH")


def test_fund_transfer_without_its_ft_prefix_still_names_the_party():
    for text in ("DR-50100238192281 SHAHID ANSARI", "FT - DR - 50100238192281 - SHAHID ANSARI"):
        info = parse_narration(text, direction="Debit")
        assert info.counterparty == "SHAHID ANSARI", text


def test_amount_with_a_symbol_for_its_leading_five_is_restored():
    def w(text, x0):
        return Word(text, x0, x0 + 30, 0, 8)

    fixed = [x.text for x in _fix_symbol_for_five([w("$48.00", 430), w("S1,200.00", 430), w("$HDFC", 100), w("48.00", 430)])]
    assert fixed == ["548.00", "51,200.00", "$HDFC", "48.00"]


def test_gap_inside_a_handle_number_is_closed():
    assert _repair_vpa("UPI-MUMTAZ ANSARI-971885784 1@KOTAK-KKBK") == "UPI-MUMTAZ ANSARI-9718857841@KOTAK-KKBK"


def _sbi(i: int, name: str, kind: str = "DR") -> Transaction:
    return _txn(i, f"TO TRANSFER- UPI/{kind}/{400000000000 + i}/{name}/YESB/paytmqr281/Payme-")


def test_names_cut_by_the_25_column_wrap_are_rejoined_only_when_the_pieces_say_so():
    rows = [_sbi(i, "SHAKE EL") for i in range(4)] + [_sbi(10 + i, "HIMAC HAL") for i in range(3)]
    rows += [_sbi(20 + i, "MUNNI DEVI") for i in range(3)] + [_sbi(30 + i, "ZAFAR HA") for i in range(3)]
    rows += [_sbi(40, "ASHIS HK"), _sbi(41, "KASHM 5, IR"), _sbi(42, 'PRASH “ANT')]
    clean_ocr_narrations(rows)
    names = [t.narration.split("/")[3] for t in rows]
    assert names[:4] == ["SHAKEEL"] * 4 and names[4:7] == ["HIMACHAL"] * 3
    assert names[7:10] == ["MUNNI DEVI"] * 3          # a real two-word name stays two words
    assert names[10:13] == ["ZAFAR HA"] * 3           # nothing proves ZAFAR HA is one word
    assert names[13] == "ASHISH K"                    # a name and the initial that ran into it
    assert names[14] == "KASHMIR" and names[15] == "PRASHANT"  # junk marks inside a name are dropped first


def test_bank_code_glued_onto_a_name_and_a_misread_upi_tag_are_repaired():
    rows = [_sbi(i, "SHAKE EL") for i in range(12)]
    rows.append(_txn(50, "TO TRANSFER- UPI/DR/412345678901/IRFAN ALIYESB/paytm/Pay-"))
    rows.append(_txn(51, "BY TRANSFER- UPUCR/4307 54565232/AAKASH C/PUNB/931/NA-"))
    clean_ocr_narrations(rows)
    assert rows[12].narration.startswith("TO TRANSFER- UPI/DR/412345678901/IRFAN ALI/YESB")
    assert rows[13].narration.startswith("BY TRANSFER- UPI/CR/430754565232/AAKASH C/PUNB")
