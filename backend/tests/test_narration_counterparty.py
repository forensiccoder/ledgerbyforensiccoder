"""Regression test for a real-world counterparty-extraction bug: at least one real bank (Fincare)
prints "NEFT OUT NEFT/<UTR>/<payee name>/<IFSC>/<account>/" - repeating the channel word "NEFT"
around the direction word "OUT" rather than saying it once. The narration decomposer only ever
stripped a single contiguous run of channel keywords (NEFT/RTGS/...) *or* a single contiguous run
of direction markers (TO/BY/OUT/...), not an interleaving of both, so the leftover "OUT NEFT" was
mistaken for the counterparty name - and since the first name-shaped token found wins, the real
payee later in the same narration never got a chance.

No real statement or personal data is used here - this reproduces the phrasing with a synthetic
name, reference and IFSC.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.narration import parse_narration


def test_repeated_channel_word_around_direction_marker_does_not_steal_the_counterparty_slot():
    info = parse_narration(
        "NEFT OUT NEFT/FSFBH22601183446/John Doe/IOBA0000442/044201000001213/",
        direction="Debit",
    )
    assert info.category == "NEFT"
    assert info.counterparty == "John Doe"
    assert info.reference == "FSFBH22601183446"


def test_leading_direction_word_before_a_name_is_still_stripped():
    info = parse_narration("NEFT CR-SBIN0001234-TO ACME TRADERS-SBINN52024040212345678", direction="Credit")
    assert info.counterparty == "ACME TRADERS"
