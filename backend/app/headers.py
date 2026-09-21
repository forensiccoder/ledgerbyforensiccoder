"""Mapping of statement column headers to roles.

Banks label the same column many ways ("Withdrawal Amt.", "DR", "Debit", ...). Each header is
classified into exactly one role. The ORDER of the checks matters: e.g. "Transaction Date" must
be a date column, not a narration column just because it contains the word "transaction"
(the old browser parser got this wrong).
"""
from __future__ import annotations

import difflib
import re

NUMERIC_ROLES = {"debit", "credit", "amount", "balance"}
TEXT_ROLES = {"narration", "ref", "counterparty"}

LABELS = {
    "date": "Date",
    "value_date": "Value Date",
    "narration": "Narration",
    "ref": "Reference",
    "debit": "Debit",
    "credit": "Credit",
    "amount": "Amount",
    "balance": "Balance",
    "indicator": "Dr/Cr",
    "counterparty": "Counterparty",
    "serial": "Sr No",
}

_FUZZY_KEYWORDS = {
    "narration": "narration", "description": "narration", "particulars": "narration",
    "remarks": "narration", "withdrawal": "debit", "debit": "debit", "deposit": "credit",
    "credit": "credit", "balance": "balance", "date": "date", "amount": "amount",
}


def norm(text: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def classify_header(text: object, fuzzy: bool = False) -> str | None:
    n = norm(text)
    if not n:
        return None
    if n in {"drcr", "crdr", "drcrind", "crdrind", "drcrindicator", "crdrindicator", "type",
             "txntype", "trantype", "transactiontype"}:
        return "indicator"
    if n in {"sno", "srno", "srlno", "slno", "serialno", "serial", "sr", "no", "sl"}:
        return "serial"
    if "value" in n and ("date" in n or n.endswith("dt")):
        return "value_date"
    if "date" in n or n in {"dt", "txndt", "trandt", "postdt"}:
        return "date"
    if "balance" in n or n in {"bal", "closingbal", "runningbal"}:
        return "balance"
    if any(k in n for k in ("beneficiary", "payee", "counterparty", "partyname")):
        return "counterparty"
    has_debit = any(k in n for k in ("withdrawal", "debit", "paidout", "dramount", "wdl")) or n == "dr"
    has_credit = any(k in n for k in ("deposit", "credit", "paidin", "cramount")) or n == "cr"
    if has_debit and has_credit:
        return "amount"  # e.g. Kotak's single "Withdrawal (Dr)/Deposit (Cr)" column
    if has_debit:
        return "debit"
    if has_credit:
        return "credit"
    if "amount" in n or n in {"amt", "value"}:
        return "amount"
    if any(k in n for k in ("narration", "description", "particular", "remark", "detail")):
        return "narration"
    if any(k in n for k in ("chq", "cheque", "instrument", "utr", "rrn", "txnid",
                             "transactionid", "refno", "reference")) or n in {"ref", "chqno"}:
        return "ref"
    if fuzzy and len(n) >= 4:
        hit = difflib.get_close_matches(n, list(_FUZZY_KEYWORDS), n=1, cutoff=0.8)
        if hit:
            return _FUZZY_KEYWORDS[hit[0]]
    return None


def header_ok(roles: set[str | None]) -> bool:
    """A header row must locate the date, the narration and at least one money column."""
    return "date" in roles and "narration" in roles and bool(roles & NUMERIC_ROLES)


def is_header_row(cells: list[object], fuzzy: bool = False) -> bool:
    return header_ok({classify_header(c, fuzzy) for c in cells})
