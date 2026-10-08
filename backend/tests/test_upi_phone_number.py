"""A UPI narration often names the payee's mobile number: as a field of its own between slashes
(SBI: "UPI/DR/<rrn>/<name>/<bank>/9540062540/Payme-") or as the user part of a handle (HDFC:
"UPI-<name>-9462013057-2@AXL-..."). Synthetic numbers only."""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.narration import extract_phone


def test_number_between_slashes_is_extracted():
    assert extract_phone("TO TRANSFER- UPI/DR/383799467017/NAME/BKID/9540062540/Paymen-") == "9540062540"
    assert extract_phone("UPI/CR/416481673349/NAME/INDB/8800525914/Pa") == "8800525914"


def test_number_in_a_handle_is_extracted():
    assert extract_phone("UPI-MAHESH KUMAR-9462013057-2@AXL-INDB0000867-351403068037-PAYMENT FROM PHONE") == "9462013057"
    assert extract_phone("UPI-AMAR CHAND-8696222999@YBL-AUBL0002206-467954362655") == "8696222999"


def test_number_split_by_an_ocr_space_is_joined():
    assert extract_phone("TO TRANSFER- UPI/DR/953935637560/Mr/IDIB/953681 6982/Pay-") == "9536816982"


def test_references_and_look_alikes_are_not_phone_numbers():
    assert extract_phone("UPI/Karan Singh/867413031535/UPI") == ""            # 12-digit RRN
    assert extract_phone("UPI/NAME/005332877354/Payment from Ph") == ""
    assert extract_phone("UPI-X-12345678901@YBL") == ""                       # 11 digits
    assert extract_phone("UPI/DR/239562345678/NAME/SBIN/1234567890/Pay") == ""  # does not start 6-9
    assert extract_phone("UPI-RAHUL-RAHULCHAUHAN7173.RC@OKICICI-ICIC0006793") == ""
    assert extract_phone("") == ""


def test_phone_reaches_the_api_and_only_for_upi():
    from app.models import Transaction
    from datetime import date
    from decimal import Decimal
    from app.service import apply_narration, txn_to_api

    upi = Transaction(id="t1", date=date(2026, 1, 1), amount=Decimal("5"), narration="UPI-A B-9462013057@YBL-HDFC0001-123456789012-PAY",
                      source="s", direction="Debit", direction_source="column", page=1, row=1)
    other = Transaction(id="t2", date=date(2026, 1, 1), amount=Decimal("5"), narration="NEFT-9462013057-ACME TRADERS",
                        source="s", direction="Debit", direction_source="column", page=1, row=2)
    apply_narration(upi); apply_narration(other)
    assert txn_to_api(upi)["phone"] == "9462013057"
    assert txn_to_api(other)["phone"] == ""
