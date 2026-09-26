"""Raw rows -> Transaction objects (exact Decimal amounts, ISO dates, direction, wrapped narration)."""
from __future__ import annotations

import re
from decimal import Decimal

from .headers import classify_header, header_ok
from .models import NormalizeResult, RawRow, Transaction
from .money import Money, parse_date, parse_money

_MARKER_OPEN = re.compile(r"^\s*(?:opening\s+balance|balance\s+(?:b/?f|brought)|brought\s+forward|b/f\b)", re.I)
_MARKER_CLOSE = re.compile(r"^\s*(?:closing\s+balance|balance\s+(?:c/?f|carried)|carried\s+forward|c/f\b)", re.I)
_MARKER_TOTAL = re.compile(r"^\s*(?:grand\s+)?total\b|page\s+total|statement\s+summary", re.I)
# At least one bank (HDFC) prints a standard legal disclaimer footer on every page ("Closing
# balance includes funds earmarked for hold and uncleared funds. Contents of this statement will
# be considered correct if no error is reported within 30 days...") right after the last row's own
# text, with no row/line boundary of its own to separate it - and PDF text extraction sometimes
# drops the spaces between its words entirely ("Closingbalanceincludesfunds..."), which is also why
# app/layout.py's _FOOTER pattern (which requires literal spaces) never catches it. The result is
# this disclaimer landing inside a real transaction's narration - and since it's the same wording
# every time, cut everything from its first recognisable phrase onward, spaces or not.
_KNOWN_FOOTER_SUFFIX = re.compile(
    r"\s*(?:HDFC\s*BANK\s*LIMITED\s*)?\*?\s*closing\s*balance\s*includes\s*funds\s*earmarked.*$",
    re.I | re.S,
)


# A chunk boundary sometimes swallows the single space that started the next chunk.
def _loose(phrase: str) -> re.Pattern[str]:
    """The phrase with an optional stray space allowed between any two of its letters."""
    letters = [re.escape(ch) for ch in phrase.replace(" ", "")]
    return re.compile(r"\s?".join(letters))


_CHUNK_BOUNDARY_REPAIRS = [
    (_loose("PAYMENT FROM PHONE"), "PAYMENT FROM PHONE"),
    (re.compile(r"-PAYTO (?=\S)"), "-PAY TO "),
]


def _repair_chunk_boundaries(text: str) -> str:
    for pattern, repl in _CHUNK_BOUNDARY_REPAIRS:
        text = pattern.sub(repl, text)
    return text


def _nonzero(m: Money | None) -> Money | None:
    return None if m is None or m.value == 0 else m


def _build_mapping(cells: list[object]) -> dict[str, list[int]]:
    mapping: dict[str, list[int]] = {}
    for idx, cell in enumerate(cells):
        role = classify_header(cell)
        if role is None:
            continue
        # narration can span several columns (Narration + Remarks); other roles keep the first
        if role == "narration" or role not in mapping:
            mapping.setdefault(role, []).append(idx)
    return mapping


def _cell(cells: list[object], mapping: dict[str, list[int]], role: str) -> str:
    parts = [str(cells[i]).strip() for i in mapping.get(role, []) if i < len(cells) and cells[i] not in (None, "")]
    return " ".join(p for p in parts if p)


def _cell_raw(cells: list[object], mapping: dict[str, list[int]], role: str) -> object:
    idxs = mapping.get(role, [])
    return cells[idxs[0]] if idxs and idxs[0] < len(cells) else ""


_PADDED_REFERENCE_PREFIX = re.compile(r"^0{2,}\d{6,}(?:\s+(\S.*))?$")


def _strip_padded_reference_prefix(value: object) -> object:
    """Some banks print a zero-padded UTR/reference number directly inside the Debit/Credit cell
    itself, ahead of the real amount when there is one ("0000105526161947 2,520.00"), and with
    nothing following it at all on a row where that column has no real amount - e.g. a credit-only
    row still shows the bare reference in the Debit cell ("0000105526161947"). Left alone, that
    bare reference gets parsed as if it were the amount itself, turning a small transaction into a
    six-trillion-rupee one. A genuine amount is never zero-padded, so this is a safe, narrow signal
    - unlike a general "reject long numbers" rule, it won't touch a real (if unusually large) rupee
    figure that simply lacks decimal paise.
    """
    if not isinstance(value, str):
        return value
    m = _PADDED_REFERENCE_PREFIX.match(value.strip())
    if not m:
        return value
    return m.group(1) or ""


def _indicator_direction(text: str) -> str | None:
    if re.search(r"\b(?:cr|credit)\b", text, re.I):
        return "Credit"
    if re.search(r"\b(?:dr|debit)\b", text, re.I):
        return "Debit"
    return None


def build_transactions(rows: list[RawRow], source: str) -> NormalizeResult:
    mapping: dict[str, list[int]] | None = None
    txns: list[Transaction] = []
    opening: Decimal | None = None
    closing: Decimal | None = None
    warnings: list[str] = []
    skipped_no_date_with_amount = 0
    saw_negative_amount = False
    signed_unknown: list[Transaction] = []
    candidate_rows = 0

    for raw in rows:
        cells = raw.cells
        roles = {classify_header(c) for c in cells}
        if header_ok(roles):
            mapping = _build_mapping(cells)  # refreshed on every repeated header (multi-page PDFs)
            continue
        if mapping is None:
            continue

        narration = _repair_chunk_boundaries(_KNOWN_FOOTER_SUFFIX.sub("", _cell(cells, mapping, "narration")).strip())
        d_raw = _cell_raw(cells, mapping, "date")
        date = parse_date(d_raw)
        debit = _nonzero(parse_money(_strip_padded_reference_prefix(_cell_raw(cells, mapping, "debit"))))
        credit = _nonzero(parse_money(_strip_padded_reference_prefix(_cell_raw(cells, mapping, "credit"))))
        amount_m = _nonzero(parse_money(_strip_padded_reference_prefix(_cell_raw(cells, mapping, "amount"))))
        balance_m = parse_money(_cell_raw(cells, mapping, "balance"))
        indicator = _cell(cells, mapping, "indicator")
        has_amount = bool(debit or credit or amount_m)

        # Opening / closing balance lines carry no transaction.
        if _MARKER_OPEN.match(narration):
            if opening is None and not txns and balance_m is not None:
                opening = balance_m.signed
            continue
        if _MARKER_CLOSE.match(narration):
            if balance_m is not None:
                closing = balance_m.signed
            continue
        if _MARKER_TOTAL.match(narration) and date is None:
            continue

        if date is None:
            if narration and not has_amount and balance_m is None and txns:
                txns[-1].narration = f"{txns[-1].narration} {narration}".strip()  # wrapped narration row
            elif has_amount:
                skipped_no_date_with_amount += 1
            continue
        candidate_rows += 1
        if not narration and not has_amount:
            continue

        direction, dsource, amount = "Unknown", "unknown", None
        flags: list[str] = []
        if credit and debit:
            amount, flags = debit.value, ["both_debit_and_credit_filled"]
        elif credit:
            direction, dsource, amount = "Credit", "column", credit.value
        elif debit:
            direction, dsource, amount = "Debit", "column", debit.value
        elif amount_m:
            amount = amount_m.value
            ind = _indicator_direction(indicator) if indicator else None
            if ind:
                direction, dsource = ind, "indicator"
            elif amount_m.suffix:
                direction, dsource = ("Credit" if amount_m.suffix == "Cr" else "Debit"), "indicator"
            elif amount_m.negative:
                direction, dsource, saw_negative_amount = "Debit", "sign", True
        if amount is None:
            continue

        txn = Transaction(
            id=f"t{len(txns) + 1:05d}",
            date=date,
            amount=amount,
            narration=narration,
            source=source,
            direction=direction,
            direction_source=dsource,
            balance=balance_m.signed if balance_m is not None else None,
            page=raw.page,
            row=len(txns) + 1,
            explicit_reference=_cell(cells, mapping, "ref"),
            explicit_counterparty=_cell(cells, mapping, "counterparty"),
            flags=flags,
        )
        if direction == "Unknown" and amount_m and not (credit or debit):
            signed_unknown.append(txn)
        txns.append(txn)

    # In a file whose single Amount column uses minus signs for debits, unsigned means credit.
    if saw_negative_amount:
        for t in signed_unknown:
            t.direction, t.direction_source = "Credit", "sign"
    if skipped_no_date_with_amount:
        warnings.append(
            f"{skipped_no_date_with_amount} row(s) with an amount but no date were skipped. "
            "Compare the totals against the statement summary."
        )
    return NormalizeResult(txns, opening, closing, candidate_rows, warnings)
