"""Regression test for a real-world extraction bug: at least one bank (HDFC) chops each narration
into 40-character chunks before printing, so a printed line break often falls *inside* a word
("PAYMEN" / "T FROM PHONE") while a wrap at a space is a real space. Joining every line with a space
produced "PAYMEN T FROM PHONE" and "...-AU BL0002206-..." in the narration.

Synthetic text only - the structure, not any real statement's content.
"""
from __future__ import annotations

import sys

sys.path.insert(0, "/home/claude/ledgerlens/backend")

from app.layout import LayoutState, Word, layout_page


def _w(text: str, x0: float, top: float) -> Word:
    return Word(text, x0, x0 + 4.0 * len(text), top, top + 8)


def _words() -> list[Word]:
    ws: list[Word] = []
    top = 0.0
    ws += [_w("Date", 20, top), _w("Narration", 100, top), _w("Debit", 310, top), _w("Credit", 380, top), _w("Balance", 450, top)]
    top += 17
    for n in range(6):
        # 40-char first chunk on the date line, 40-char second chunk, then a short tail
        first = "UPI-AJIT SERVICE STATION-OMBK.AAEK08166R"  # 40 chars
        second = "2JHE5KOL7@MBK-PPIW0881822-986444010805-P"  # 40 chars
        assert len(first) == 40 and len(second) == 40
        ws += [_w("03/07/25", 20, top)]
        x = 100.0
        for token in first.split(" "):
            ws.append(_w(token, x, top))
            x += 4.0 * len(token) + 4
        ws += [_w("2,500.00", 380, top), _w("21,194.65", 450, top)]
        top += 17
        ws.append(Word(second, 100, 250, top, top + 8))
        top += 17
        ws += [_w("AYMENT", 100, top), _w("FROM", 130, top), _w("PHONE", 160, top)]
        top += 17
    return ws


def test_chunk_boundary_joins_without_a_space_and_wrap_at_a_space_keeps_it():
    rows = layout_page(_words(), page=1, state=LayoutState(), ocr=False)
    narr_i = rows[0].cells.index("Narration")
    expected = "UPI-AJIT SERVICE STATION-OMBK.AAEK08166R2JHE5KOL7@MBK-PPIW0881822-986444010805-PAYMENT FROM PHONE"
    for row in rows[1:]:
        assert row.cells[narr_i] == expected
