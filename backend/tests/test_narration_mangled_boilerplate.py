"""Regression test for real-world counterparty-extraction bugs found auditing a 2600+ transaction
statement: generic boilerplate remark text (not a counterparty) was winning the "first name-shaped
token" slot before the real payee later in the same narration, because the boilerplate didn't look
like the words it actually was.

  - At least one bank's PDF text extraction mangles its own remark phrases seemingly at random -
    the same "payment from phone" comes out as "PAYMENTFROMPHONE", "PAYMENTFROMPH ONE",
    "P AYMENTFROMPHONE" and more across a single statement - so a fixed check for normally-spaced
    boilerplate missed all of these.
  - A masked account/card number ("XXXX9542") isn't a name either, but nothing previously
    recognised the "XXXX<digits>" shape as non-name content.

No real statement or personal data is used here - this reproduces the patterns with synthetic
names and references.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.narration import parse_narration


def test_glued_payment_from_phone_does_not_shadow_the_real_payee():
    info = parse_narration("YADAV-JOHNDOE9999@YBL-AUBL0002191-105526161947-PAYMENTFROMPHONE UPI-JANEDOE", direction="Debit")
    assert info.counterparty == "YADAV"


def test_randomly_spaced_payment_from_phone_variants_are_all_recognised():
    variants = [
        "PAYMENTFROMPH ONE",
        "P AYMENTFROMPHONE",
        "PAYMENTFROMPHON E",
        "PAYMENTFROMP HONE",
        "PA YMENTFROMPHONE",
    ]
    for variant in variants:
        info = parse_narration(f"SHARMA-{variant} UPI-JOHNDOE", direction="Debit")
        assert info.counterparty == "SHARMA", f"failed for variant: {variant!r}"


def test_masked_account_number_is_not_treated_as_the_counterparty():
    info = parse_narration("XXXX9542-IMPSTRANSACTION UPI-GHANSHYAM", direction="Debit")
    assert info.counterparty == "GHANSHYAM"


def test_money_transfer_and_truncated_collect_request_boilerplate_are_recognised():
    assert parse_narration("XXXXXXXXX8888-MONEYTRANSFER UPI-PRIYANKA SHARMA", direction="Credit").counterparty == "PRIYANKA SHARMA"
    # the bank's own text is truncated to "COLLECTREQUESTFR" (missing "OM") - not just mis-spaced
    assert parse_narration("6784-COLLECTREQUESTFR UPI-GROFERSINDIA", direction="Debit").counterparty == "GROFERSINDIA"
