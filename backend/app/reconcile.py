"""Balance-chain reconciliation.

Every statement row carries a running balance, so for consecutive rows
``balance[i] == balance[i-1] +/- amount[i]`` must hold exactly. That gives us three things:

* a check that nothing was missed or misread (a break points at the row where it happened);
* the true direction of a row whose Debit/Credit column was blank, ambiguous or misaligned;
* the statement's ascending/descending order (some banks print newest first).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from .models import Transaction

MAX_ISSUES = 25


@dataclass
class Reconciliation:
    status: str = "unavailable"  # ok | mismatch | unavailable
    order: str = "ascending"
    opening: Decimal | None = None
    closing: Decimal | None = None
    total_debits: Decimal = Decimal("0")
    total_credits: Decimal = Decimal("0")
    expected_closing: Decimal | None = None
    difference: Decimal | None = None
    checked: int = 0
    broken: int = 0
    unknown_direction: int = 0
    issues: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _pairs_matching(chron: list[Transaction]) -> int:
    n = 0
    for prev, cur in zip(chron, chron[1:]):
        if prev.balance is not None and cur.balance is not None and abs(cur.balance - prev.balance) == cur.amount:
            n += 1
    return n


def detect_order(txns: list[Transaction]) -> str:
    return "descending" if _pairs_matching(txns[::-1]) > _pairs_matching(txns) else "ascending"


def reconcile(txns: list[Transaction], opening: Decimal | None = None, closing: Decimal | None = None) -> Reconciliation:
    rec = Reconciliation()
    if not txns:
        return rec
    rec.order = detect_order(txns)
    chron = txns if rec.order == "ascending" else txns[::-1]

    # Balance evidence overrides a misread column; narration hints only fill remaining unknowns.
    prev_balance = opening
    for t in chron:
        implied = None
        if prev_balance is not None and t.balance is not None:
            delta = t.balance - prev_balance
            if delta == t.amount:
                implied = "Credit"
            elif delta == -t.amount:
                implied = "Debit"
            else:
                rec.broken += 1
                t.add_flag("balance_break")
                if len(rec.issues) < MAX_ISSUES:
                    rec.issues.append({
                        "id": t.id, "date": t.date.isoformat(), "page": t.page, "row": t.row,
                        "narration": t.narration[:80], "amount": float(t.amount),
                        "expectedBalance": float(prev_balance + (t.signed_amount() or Decimal(0))),
                        "statementBalance": float(t.balance),
                    })
            rec.checked += 1
        if implied and implied != t.direction:
            if t.direction != "Unknown":
                t.add_flag("direction_corrected_by_balance")
            t.direction, t.direction_source = implied, "balance"
        elif t.direction == "Unknown" and t.direction_hint:
            t.direction, t.direction_source = t.direction_hint, "narration"
            t.add_flag("direction_from_narration")
        prev_balance = t.balance if t.balance is not None else prev_balance

    rec.total_debits = sum((t.amount for t in txns if t.direction == "Debit"), Decimal(0))
    rec.total_credits = sum((t.amount for t in txns if t.direction == "Credit"), Decimal(0))
    rec.unknown_direction = sum(1 for t in txns if t.direction == "Unknown")

    first, last = chron[0], chron[-1]
    if opening is None and first.balance is not None and first.signed_amount() is not None:
        opening = first.balance - first.signed_amount()  # type: ignore[operator]
        rec.notes.append("Opening balance was not printed; it was derived from the first row.")
    if closing is None and last.balance is not None:
        closing = last.balance
    rec.opening, rec.closing = opening, closing

    if opening is not None and closing is not None:
        rec.expected_closing = opening + rec.total_credits - rec.total_debits
        rec.difference = closing - rec.expected_closing

    if rec.checked == 0 or (rec.opening is None and rec.closing is None):
        rec.status = "unavailable"
        rec.notes.append("No running balance column was found, so extraction could not be cross-checked.")
    elif rec.broken == 0 and (rec.difference in (None, Decimal(0))) and rec.unknown_direction == 0:
        rec.status = "ok"
    else:
        rec.status = "mismatch"
    if rec.unknown_direction:
        rec.notes.append(f"{rec.unknown_direction} row(s) have no determinable Debit/Credit direction.")
    return rec
