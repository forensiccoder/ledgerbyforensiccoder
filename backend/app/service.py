"""The analysis pipeline: extract -> normalise -> classify -> reconcile -> summarise.

Framework-free on purpose (main.py is only the HTTP wrapper) so it is easy to test and reuse.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pandas as pd

from .errors import StatementError
from .extract import extract_statement
from .models import Transaction
from .narration import CASH_DEPOSIT, CASH_WITHDRAWAL, OTHER, TARGET_CATEGORIES, parse_narration
from .normalize import build_transactions
from .reconcile import Reconciliation, reconcile

_ZERO_PAD_REF = re.compile(r"^0+(\d{12})$")


def _normalise_reference(text: str) -> str:
    """HDFC prints UPI refs zero-padded to 16 digits; the real 12-digit RRN is the tail."""
    text = text.strip()
    m = _ZERO_PAD_REF.match(text)
    return m.group(1) if m else text


def apply_narration(t: Transaction) -> None:
    """Classify a transaction from its narration. Idempotent: safe to re-run after direction changes."""
    info = parse_narration(t.narration, t.direction)
    t.category, t.channel = info.category, info.channel
    t.vpa, t.ifsc, t.bank = info.vpa, info.ifsc, info.bank
    t.direction_hint = info.direction_hint
    t.confidence = info.confidence
    # A dedicated counterparty column in the statement beats anything inferred from narration.
    t.counterparty = t.explicit_counterparty or info.counterparty
    # The narration's own RRN/UTR is preferred; fall back to the statement's Ref/Cheque column.
    t.reference = info.reference or _normalise_reference(t.explicit_reference)
    kept = [f for f in t.flags if f in {"balance_break", "direction_corrected_by_balance",
                                        "direction_from_narration", "both_debit_and_credit_filled"}]
    t.flags = kept + [f for f in info.flags if f not in kept]
    if t.category in TARGET_CATEGORIES and info.confidence in {"medium", "low"} and "review_classification" not in t.flags:
        t.flags.append("review_classification")


def display_counterparty(t: Transaction) -> str:
    if t.counterparty:
        return t.counterparty
    if t.category == CASH_DEPOSIT:
        return "Cash deposit"
    if t.category == CASH_WITHDRAWAL:
        return "Cash withdrawal"
    return "Review narration"


def to_dataframe(txns: list[Transaction]) -> pd.DataFrame:
    return pd.DataFrame([{
        "id": t.id, "date": t.date, "category": t.category, "channel": t.channel,
        "direction": t.direction, "counterparty": display_counterparty(t), "vpa": t.vpa,
        "ifsc": t.ifsc, "bank": t.bank, "reference": t.reference, "amount": t.amount,
        "balance": t.balance, "narration": t.narration, "page": t.page, "row": t.row,
        "confidence": t.confidence, "flags": ", ".join(t.flags),
    } for t in txns], dtype=object)


def summarise(df: pd.DataFrame) -> dict:
    """Per-category counts and exact (Decimal) totals, plus what fell outside the four categories."""
    by_category = {}
    for category in (*TARGET_CATEGORIES, OTHER):
        sub = df[df["category"] == category] if len(df) else df
        by_category[category] = {
            "count": int(len(sub)),
            "total": float(sum(sub["amount"], Decimal(0))) if len(sub) else 0.0,
        }
    other = df[df["category"] == OTHER] if len(df) else df
    other_channels = other["channel"].replace("", "Unclassified").value_counts().to_dict() if len(other) else {}
    return {
        "byCategory": by_category,
        "otherCount": int(len(other)),
        "otherChannels": {str(k): int(v) for k, v in other_channels.items()},
    }


def _f(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def txn_to_api(t: Transaction) -> dict:
    return {
        "id": t.id,
        "date": t.date.strftime("%d %b %Y"),
        "dateIso": t.date.isoformat(),
        "category": t.category,
        "channel": t.channel,
        "direction": t.direction,
        "beneficiary": display_counterparty(t),
        "vpa": t.vpa,
        "ifsc": t.ifsc,
        "bank": t.bank,
        "reference": t.reference,
        "narration": t.narration,
        "amount": float(t.amount),
        "balance": _f(t.balance),
        "source": t.source,
        "page": t.page,
        "row": t.row,
        "confidence": t.confidence,
        "flags": t.flags,
    }


def reconciliation_to_api(r: Reconciliation) -> dict:
    return {
        "status": r.status, "order": r.order,
        "opening": _f(r.opening), "closing": _f(r.closing),
        "totalDebits": _f(r.total_debits), "totalCredits": _f(r.total_credits),
        "expectedClosing": _f(r.expected_closing), "difference": _f(r.difference),
        "checkedRows": r.checked, "brokenRows": r.broken, "unknownDirection": r.unknown_direction,
        "issues": r.issues, "notes": r.notes,
    }


def analyze(data: bytes, filename: str, password: str | None = None, ocr_mode: str = "auto") -> dict:
    if ocr_mode not in {"auto", "force", "off"}:
        ocr_mode = "auto"
    extracted = extract_statement(data, filename, password, ocr_mode)
    normalised = build_transactions(extracted.rows, filename)
    txns = normalised.transactions
    if not txns:
        hint = ""
        if extracted.method == "ocr" or extracted.ocr_pages:
            hint = " The pages were read with OCR; try a higher-quality scan or the bank's CSV/XLSX export."
        raise StatementError(
            "NO_TRANSACTIONS",
            "No transaction rows were found. Check that this is a bank statement with a header row "
            "(Date, Narration/Description, Debit/Credit or Amount, Balance)." + hint,
        )

    for t in txns:
        apply_narration(t)
    rec = reconcile(txns, normalised.opening_balance, normalised.closing_balance)
    for t in txns:  # direction may have changed (balance / narration) -> classify again
        apply_narration(t)

    df = to_dataframe(txns)
    summary = summarise(df)
    dates = [t.date for t in txns]
    warnings = extracted.warnings + normalised.warnings
    if rec.status == "mismatch":
        warnings.append(
            "Balances do not fully reconcile - some rows may be missing or misread. "
            "Review the flagged rows against the original statement."
        )
    return {
        "fileName": filename,
        "extraction": {"method": extracted.method, "pages": extracted.pages, "ocrPages": extracted.ocr_pages},
        "statementPeriod": {"from": min(dates).isoformat(), "to": max(dates).isoformat()},
        "totalRows": len(txns),
        "transactions": [txn_to_api(t) for t in txns],
        "summary": summary,
        "reconciliation": reconciliation_to_api(rec),
        "warnings": warnings,
    }
