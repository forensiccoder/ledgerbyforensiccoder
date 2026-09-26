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


_CONFUSABLE = {"0": "986", "1": "47", "2": "7", "3": "8", "4": "1", "5": "6", "6": "50", "7": "12", "8": "03", "9": "0"}


def _is_ocr_near_miss(read: Decimal, true: Decimal) -> bool:
    """Could ``read`` be ``true`` with one digit swapped for a look-alike (1/4, 0/8/9, 5/6, 3/8...)?
    Anything else - a different amount altogether - is more likely a missing row than a misread."""
    a, b = f"{read:.2f}", f"{true:.2f}"
    if len(a) != len(b):
        return False
    diff = [(x, y) for x, y in zip(a, b) if x != y]
    return len(diff) == 1 and diff[0][1] in _CONFUSABLE.get(diff[0][0], "")


def repair_from_balances(txns: list[Transaction], opening: Decimal | None = None) -> int:
    """Use the running balance to repair OCR misreads, returns how many values were changed.

    For a photographed statement a digit or a whole figure is often misread, but the balance chain
    (previous balance +/- amount = this balance) is redundant enough to say which figure is wrong:
    if this row's balance agrees with the *next* row, the amount is the odd one out and is replaced
    by the balance difference; if it does not, the balance is the odd one out and is rebuilt from
    the previous balance and the amount. A blank balance is filled the same way. Nothing is changed
    unless the next row corroborates it, and every change is flagged for review.
    """
    if len(txns) < 3:
        return 0
    chron = txns if detect_order(txns) == "ascending" else txns[::-1]
    changed = 0
    prev = opening if opening is not None else next((t.balance for t in chron if t.balance is not None), None)
    start = 0 if opening is not None else next((i for i, t in enumerate(chron) if t.balance is not None), 0) + 1
    if opening is None and prev is not None:
        prev = chron[start - 1].balance

    def consistent(row: Transaction | None, balance: Decimal | None) -> bool:
        if row is None or row.balance is None or balance is None:
            return True  # nothing to contradict it
        return abs(row.balance - balance) == row.amount

    for i in range(start, len(chron)):
        t = chron[i]
        nxt = chron[i + 1] if i + 1 < len(chron) else None
        if prev is None:
            prev = t.balance
            continue
        if t.balance is not None:
            delta = t.balance - prev
            if abs(delta) != t.amount:
                # Only a *near miss* is an OCR slip (one wrong or dropped digit); a big difference
                # usually means a whole row between the two balances was not read, and inventing an
                # amount for it would hide that.
                if (delta != 0 and _is_ocr_near_miss(t.amount, abs(delta))
                        and nxt is not None and nxt.balance is not None and abs(nxt.balance - t.balance) == nxt.amount):
                    t.amount = abs(delta)
                    t.direction, t.direction_source = ("Credit" if delta > 0 else "Debit"), "balance"
                    t.add_flag("amount_corrected_by_balance")
                    changed += 1
                else:
                    order = ("Credit", "Debit") if t.direction == "Credit" else ("Debit", "Credit")
                    for direction in order:
                        candidate = prev + t.amount if direction == "Credit" else prev - t.amount
                        if nxt is not None and nxt.balance is not None and abs(nxt.balance - candidate) == nxt.amount:
                            t.balance, t.direction, t.direction_source = candidate, direction, "balance"
                            t.add_flag("balance_corrected_by_chain")
                            changed += 1
                            break
        elif (t.direction == "Debit" and t.amount > max(prev, Decimal(0)) and nxt is not None
                and nxt.balance is not None and nxt.direction in ("Debit", "Credit")):
            # A debit larger than the whole balance it came out of cannot be right (an OCR slip such
            # as 1,000.00 read as 1900007): with this row's own balance unreadable, the next row's
            # balance still pins down what the amount must have been.
            after = nxt.balance + nxt.amount if nxt.direction == "Debit" else nxt.balance - nxt.amount
            implied = prev - after
            if 0 < implied <= prev:
                t.amount, t.balance = implied, after
                t.add_flag("amount_corrected_by_balance")
                changed += 1
        elif t.direction in ("Debit", "Credit"):
            candidate = prev + t.amount if t.direction == "Credit" else prev - t.amount
            if consistent(nxt, candidate):
                t.balance = candidate
                t.add_flag("balance_inferred_from_chain")
                changed += 1
        prev = t.balance if t.balance is not None else prev
    return changed


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
