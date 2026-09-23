"""Deterministic narration parsing (regex only, no LLM) so results are auditable and repeatable.

``parse_narration`` classifies a bank narration into one of the four categories the app reports
(Cash deposit, Cash withdrawal, NEFT, UPI) or "Other", and pulls out the counterparty, VPA,
IFSC, bank and reference (UPI RRN / NEFT UTR) where the narration contains them.

Indian banks each format narrations differently, e.g.

    UPI-RAMESH KUMAR-RAMESH@OKSBI-SBIN0001234-412345678901-UPI                  (HDFC)
    TO TRANSFER-UPI/DR/412345678901/RAMESH K/SBIN/ramesh@oksbi/UPI               (SBI)
    UPI/P2A/412345678901/RAMESH KUMAR/HDFC BANK                                  (Axis)
    NEFT*HDFC0001234*HDFCN52024010112345678*ACME TRADERS PVT LTD*                (SBI)
    NEFT CR-SBIN0001234-ACME TRADERS PVT LTD-RAMESH KUMAR-SBINN52024010112345678 (HDFC)
    NEFT/SBIN524010112345678/ACME TRADERS PVT LTD/SBIN                           (Axis)

so the parser tokenises on whichever delimiter the narration uses and classifies each token
(name / VPA / IFSC / RRN / UTR / bank / boilerplate) instead of relying on fixed positions.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

CASH_DEPOSIT = "Cash deposit"
CASH_WITHDRAWAL = "Cash withdrawal"
NEFT = "NEFT"
UPI = "UPI"
OTHER = "Other"
TARGET_CATEGORIES = (CASH_DEPOSIT, CASH_WITHDRAWAL, NEFT, UPI)

BANK_CODES = frozenset(
    "SBIN HDFC ICIC UTIB KKBK PUNB BARB CNRB IDIB YESB INDB IBKL UBIN BKID MAHB IOBA UCBA CBIN "
    "SRCB FDRL SIBL KARB RATN DBSS CITI HSBC SCBL AIRP PYTM IPOS JSFB ESFB AUBL USFB DLXB TMBL "
    "KVBL CSBK JAKA DCBL NESF UTBI".split()
)


@dataclass
class NarrationInfo:
    category: str = OTHER
    channel: str = ""  # finer detail: ATM, CDM, Self cheque, IMPS, RTGS, POS, Charges ...
    counterparty: str = ""
    vpa: str = ""
    ifsc: str = ""
    bank: str = ""
    reference: str = ""
    direction_hint: str = ""  # "Credit" / "Debit" when the narration says so (UPI/DR/..., NEFT CR-...)
    confidence: str = "low"  # high / medium / low
    flags: list[str] = field(default_factory=list)


# ----------------------------------------------------------------------------------------------
# Token classification
# ----------------------------------------------------------------------------------------------
_IFSC = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")
# OCR often misreads the '0' in an IFSC as the letter 'O' ("SBIN0001234" -> "SBINO001234").
# Only trusted when the first 4 letters are a real bank code, so this can't misfire on an
# ordinary word.
_IFSC_OCR = re.compile(r"^[A-Z]{4}[0O][A-Z0-9]{6}$")
_RRN = re.compile(r"^\d{12}$")
_VPA_TOKEN = re.compile(r"^[A-Za-z0-9._+\-]{2,}@[A-Za-z][A-Za-z0-9.]{1,}$")
_UTR = re.compile(r"^(?=(?:.*\d){8,})[A-Z0-9]{12,}$")
_LONG_NUMBER = re.compile(r"^\d{9,}$")
_STRIP_ALWAYS = re.compile(
    r"^(?:(?:NEFT|RTGS|IMPS|UPIAR|UPI|UTR|RRN|INF|INB|TRANSFER|TRF|P2A|P2M|P2P|P2B)\b[\s:._#-]*)+",
    re.I,
)
_ALL_MARKERS = re.compile(
    r"^(?:(?:CR|DR|TO|BY|FROM|IN|OUT|REF|NO|CREDIT|DEBIT|REV|REVERSAL|REVERSED|REFUND)\b[\s:._#-]*)+$", re.I
)
_GENERIC_REMARK = re.compile(
    r"^(?:payment|paid|sent|pay|collect|request|towards|mandate|autopay|remarks?|na|nil|"
    r"via|thanks?|for|upi|oth(?:er)?s?)\b",
    re.I,
)
_CHANNEL_NOISE = re.compile(
    r"^(?:net\s*bank|netbanking|mobile|mob\b|internet|online|ib\b|mb\b|ecs|nach|neti|inb|bill\s*pay)",
    re.I,
)
_BANK_NAME = re.compile(r"\bbank\b|\bbk\b|\bpayments?\s+bank\b|\bsfb\b|\bco[\s-]?op", re.I)


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text or "")).strip()


_UPI_HANDLES = frozenset(
    "oksbi okhdfcbank okicici okaxis ybl ibl axl paytm apl sbi icici hdfcbank axisbank upi "
    "postbank pnb boi barodampay kotak yesbank airtel fbl idfcbank indus rbl unionbank "
    "okbizaxis freecharge jupiteraxis slice ikwik".split()
)


def _repair_wrapped_tokens(text: str) -> str:
    """Rejoin identifiers that PDF line-wrapping split in two.

    Banks such as HDFC wrap long narrations mid-token ('SBIN000' / '1234-4134...'). Joining the
    lines with a space would corrupt the IFSC, UPI RRN, UTR or VPA, so a split is only repaired
    when the joined result is a *valid* identifier. Ordinary word wraps are left alone.
    """
    # 12-digit UPI RRN split into two digit runs
    text = re.sub(
        r"(?<!\d)(\d{3,10})\s(\d{2,9})(?!\d)",
        lambda m: m.group(1) + m.group(2) if len(m.group(1) + m.group(2)) == 12 else m.group(0),
        text,
    )

    # IFSC (4 letters + 0 + 6 alphanumerics) split anywhere
    def ifsc(m: re.Match) -> str:
        joined = m.group(1) + m.group(2)
        return joined if _IFSC.match(joined) else m.group(0)

    text = re.sub(r"(?<![A-Za-z0-9])([A-Z]{4}0?[A-Z0-9]{0,5})\s([A-Z0-9]{1,7})(?![A-Za-z0-9])", ifsc, text)

    # NEFT UTR (bank code + digits, 16-23 chars) split in two
    def utr(m: re.Match) -> str:
        joined = m.group(1) + m.group(2)
        ok = (m.group(1)[:4] in BANK_CODES and 16 <= len(joined) <= 23 and re.fullmatch(r"[A-Z]{4}[A-Z]?\d{10,}", joined))
        return joined if ok else m.group(0)

    text = re.sub(r"(?<![A-Za-z0-9])([A-Z]{4}[A-Z]?\d{2,})\s(\d{1,})(?![A-Za-z0-9])", utr, text)

    # VPA whose handle was split ('name@ok' + 'sbi')
    def vpa(m: re.Match) -> str:
        handle = (m.group(2) + m.group(3)).lower()
        return m.group(1) + m.group(2) + m.group(3) if handle in _UPI_HANDLES and m.group(2).lower() not in _UPI_HANDLES else m.group(0)

    return re.sub(r"(?<![A-Za-z0-9])([A-Za-z0-9._+-]{2,}@)([A-Za-z]{1,12})\s([A-Za-z]{1,12})(?![A-Za-z0-9])", vpa, text)


def _delimiter(text: str) -> str:
    if "*" in text:
        return r"\*"
    if text.count("/") >= 2:
        return "/"
    if text.count("|") >= 2:
        return r"\|"
    return "-"


def _tokens(text: str) -> list[str]:
    parts = re.split(_delimiter(text), text)
    return [p.strip(" \t.,:;_") for p in parts if p and p.strip(" \t.,:;_")]


def _clean_name(token: str) -> str:
    name = re.sub(r"\s+", " ", token).strip(" -:;,._")
    return name[:80]


def _is_bank(token: str) -> bool:
    up = token.upper()
    return up in BANK_CODES or bool(_BANK_NAME.search(token))


def _kind(token: str) -> str:
    if _VPA_TOKEN.match(token):
        return "vpa"
    up = token.upper()
    if _IFSC.match(up):
        return "ifsc"
    if _IFSC_OCR.match(up) and up[:4] in BANK_CODES:
        return "ifsc"
    if _RRN.match(token):
        return "rrn"
    if _UTR.match(up.replace(" ", "")) and " " not in token:
        return "utr"
    if _LONG_NUMBER.match(token):
        return "number"
    if _is_bank(token):
        return "bank"
    if not re.search(r"[A-Za-z]{3}", token):
        return "junk"
    if _GENERIC_REMARK.match(token) or _CHANNEL_NOISE.match(token):
        return "remark"
    return "name"


@dataclass
class _Parts:
    name: str = ""
    vpa: str = ""
    ifsc: str = ""
    bank: str = ""
    rrn: str = ""
    utr: str = ""
    number: str = ""
    hint: str = ""


def _assign(parts: _Parts, kind: str, value: str) -> None:
    if kind == "vpa" and not parts.vpa:
        parts.vpa = value
    elif kind == "ifsc" and not parts.ifsc:
        parts.ifsc = value.upper().replace("O", "0", 1) if not _IFSC.match(value.upper()) else value.upper()
    elif kind == "rrn" and not parts.rrn:
        parts.rrn = value
    elif kind == "utr" and not parts.utr:
        parts.utr = value.upper()
    elif kind == "number" and not parts.number:
        parts.number = value
    elif kind == "bank" and not parts.bank:
        parts.bank = value.upper() if value.upper() in BANK_CODES else value
    elif kind == "name" and not parts.name:
        parts.name = _clean_name(value)


def _decompose(text: str) -> _Parts:
    parts = _Parts()
    text = _repair_wrapped_tokens(text)
    # SBI prints "TO TRANSFER-..." / "BY TRANSFER-..." which also tells us the direction.
    m = re.match(r"^\s*(TO|BY)\s+TRANSFER\b[\s:-]*", text, re.I)
    if m:
        parts.hint = "Debit" if m.group(1).upper() == "TO" else "Credit"
        text = text[m.end():]
    for raw in _tokens(text):
        stripped = _STRIP_ALWAYS.sub("", raw).strip(" \t.,:;_-")
        if not stripped or _ALL_MARKERS.match(stripped):
            if not parts.hint:
                marker_text = f"{raw} {stripped}"
                if re.search(r"\b(CR|CREDIT)\b", marker_text, re.I):
                    parts.hint = "Credit"
                elif re.search(r"\b(DR|DEBIT)\b", marker_text, re.I):
                    parts.hint = "Debit"
            continue
        words = stripped.split()
        if len(words) > 1:
            # No delimiter between fields ("NEFT UTR N241234567890 ACME TRADERS"): peel off any
            # reference-like words and keep the rest as the candidate name.
            keep = []
            for word in words:
                if _kind(word) in {"ifsc", "utr", "rrn", "number", "vpa"}:
                    _assign(parts, _kind(word), word)
                else:
                    keep.append(word)
            stripped = " ".join(keep)
            if not stripped:
                continue
        _assign(parts, _kind(stripped), stripped)
    if not parts.ifsc:
        m = re.search(r"\b[A-Z]{4}0[A-Z0-9]{6}\b", text.upper())
        if m:
            parts.ifsc = m.group(0)
    if not parts.vpa:
        m = re.search(r"\b[A-Za-z0-9._+]{2,}@[A-Za-z][A-Za-z0-9.]{1,}\b", text)
        if m:
            parts.vpa = m.group(0)
    if not parts.rrn:
        m = re.search(r"(?<![\dA-Za-z])\d{12}(?![\dA-Za-z])", text)
        if m:
            parts.rrn = m.group(0)
    return parts


# ----------------------------------------------------------------------------------------------
# Category patterns
# ----------------------------------------------------------------------------------------------
_UPI_WORD = re.compile(r"(?<![A-Z0-9])UPI(?![A-Z0-9])|(?<![A-Z0-9])UPIAR(?![A-Z0-9])", re.I)
_NEFT_WORD = re.compile(r"(?<![A-Z0-9])NEFT(?![A-Z0-9])", re.I)
_RTGS_WORD = re.compile(r"(?<![A-Z0-9])RTGS(?![A-Z0-9])", re.I)
_FEE = re.compile(
    r"\b(?:chg|chgs|chrg|chrgs|charge|charges|fee|fees|commission|comm|gst|cgst|sgst|igst|amc|"
    r"penalty|penal|service\s+tax|min(?:imum)?\s+bal(?:ance)?|non[\s-]?maint\w*|maintenance)\b",
    re.I,
)
_CASH_DEP = re.compile(
    r"\b(?:cash\s*[-/.]?\s*(?:dep(?:osit(?:ed)?)?|cr(?:edit)?|received|rcvd|in)|csh\s*[-/.]?\s*dep\w*|"
    r"by\s+cash|cdm|bna)\b",
    re.I,
)
_CASH_WDL = re.compile(
    r"\b(?:cash\s*[-/.]?\s*(?:wdl|wd|wdrl|withdrawal|withdraw|out)|csh\s*[-/.]?\s*wd\w*|"
    r"atm\s*[-/.]?\s*(?:wdl|wd|wdrl|cash|csh|withdrawal)|nfs|atw|nwd|eaw|awb|cwdr|wthdrl)\b",
    re.I,
)
_ATM_WORD = re.compile(r"\bATM\b", re.I)
_CASH_WORD = re.compile(r"\bCASH\b", re.I)
_CHEQUE_WORD = re.compile(r"\b(?:CHQ|CHEQUE|CHK)\b", re.I)
# Broader than _CHEQUE_WORD: also catches "CLG"/"CLEARING", used to keep a bare "WTHDRL" that's
# actually a cheque paid to a third party via clearing (e.g. "WTHDRL,CLG/000006/JOHN DOE") out of
# Cash withdrawal - that money didn't go to the account holder as cash.
_CHEQUE_OR_CLEARING_WORD = re.compile(r"\b(?:CHQ|CHEQUE|CHK|CLG|CLEARING)\b", re.I)
_SELF_WORD = re.compile(r"\bSELF\b", re.I)
_REVERSAL = re.compile(r"\b(?:rev|reversal|reversed|refund|return(?:ed)?|failed|reject(?:ed)?|unpaid)\b", re.I)
_REF_LABELLED = re.compile(
    r"(?:UTR|REF(?:ERENCE)?|RRN|TXN|TRN|TRAN(?:SACTION)?\s*ID)[\s:/#._-]*(?:NO\.?)?[\s:/#._-]*([A-Z0-9]{6,})",
    re.I,
)
_OTHER_CHANNELS = (
    ("IMPS", re.compile(r"(?<![A-Z0-9])IMPS(?![A-Z0-9])", re.I)),
    ("RTGS", _RTGS_WORD),
    ("ECS/NACH", re.compile(r"\b(?:ECS|NACH|ACH)\b", re.I)),
    ("Cheque", _CHEQUE_OR_CLEARING_WORD),
    ("Card (POS/online)", re.compile(r"\b(?:POS|PCD|ECOM|E-COM)\b", re.I)),
    ("Interest", re.compile(r"\b(?:INT\.?\s*PD|INTEREST|INT\s+CREDIT)\b", re.I)),
)


def _cash_counterparty(text: str) -> str:
    m = re.search(r"\bBY\s+([A-Z][A-Z .&'-]{2,40})", text, re.I)
    if m:
        name = _clean_name(m.group(1))
        name = re.sub(r"\b(?:cash|deposit|dep|cdm)\b.*$", "", name, flags=re.I).strip()
        if name:
            return name
    if _SELF_WORD.search(text):
        return "SELF"
    return ""


def _generic_reference(text: str) -> str:
    m = _REF_LABELLED.search(text)
    if m:
        return m.group(1).upper()
    m = re.search(r"(?<![\dA-Za-z])\d{6,16}(?![\dA-Za-z])", text)
    return m.group(0) if m else ""


def _apply_parts(info: NarrationInfo, parts: _Parts, reference: str) -> None:
    info.vpa = parts.vpa
    info.ifsc = parts.ifsc
    info.bank = parts.bank or (parts.ifsc[:4] if parts.ifsc else "")
    info.reference = reference
    info.direction_hint = parts.hint


def parse_narration(narration: str, direction: str = "Unknown") -> NarrationInfo:
    """Classify a narration. ``direction`` is "Credit"/"Debit"/"Unknown" (from the amount columns)."""
    text = _norm_text(narration)
    info = NarrationInfo()
    if not text:
        return info
    parts = _decompose(text)
    strong_ref = bool(parts.rrn or parts.vpa or parts.utr or parts.ifsc)
    if _REVERSAL.search(text):
        info.flags.append("reversal_or_return")

    # 1. Bank fees named after the product ("NEFT CHARGES", "ATM ANNUAL FEE") are not transfers or
    #    cash movements. A real UPI/NEFT transfer always carries an RRN/VPA/UTR/IFSC, so require
    #    that to be absent before calling it a fee (a UPI remark like "GST payment" stays UPI).
    if _FEE.search(text) and not strong_ref:
        info.channel = "Charges"
        info.reference = _generic_reference(text)
        return info

    # 2. UPI
    if _UPI_WORD.search(text) or (parts.vpa and parts.rrn):
        info.category = UPI
        info.channel = "UPI"
        _apply_parts(info, parts, parts.rrn or parts.number or _generic_reference(text))
        info.counterparty = parts.name or parts.vpa
        info.confidence = "high" if parts.name and (parts.vpa or parts.rrn) else "medium"
        if not parts.name:
            info.flags.append("counterparty_from_vpa" if parts.vpa else "counterparty_not_found")
        return info

    # 3. NEFT
    if _NEFT_WORD.search(text):
        info.category = NEFT
        info.channel = "NEFT"
        _apply_parts(info, parts, parts.utr or parts.number or parts.rrn or _generic_reference(text))
        info.counterparty = parts.name
        info.confidence = "high" if parts.name and info.reference else "medium"
        if not parts.name:
            info.flags.append("counterparty_not_found")
        return info

    # 3.5. RTGS - a same-day external bank transfer, not cash. Checked before the cash heuristics
    # below because banks tag an RTGS row with an urgency/priority code like "URGENT/CASH HIGH"
    # that would otherwise trip the bare "CASH" fallback in step 5 and misclassify it as a Cash
    # deposit, the same way NEFT is checked above to keep it out of the cash buckets.
    if _RTGS_WORD.search(text):
        info.channel = "RTGS"
        info.reference = parts.utr or parts.number or parts.rrn or _generic_reference(text)
        return info

    # 4. Cash deposit / withdrawal
    is_dep = bool(_CASH_DEP.search(text))
    # A bare "WTHDRL" is unambiguous, but banks also write it as "WTHDRL,CLG/.../PAYEE NAME" for a
    # cheque cleared to a third party - that's not cash into the account holder's hand, so a
    # cheque/clearing reference alongside it should keep it out of Cash withdrawal (it still gets
    # picked up by the "Cheque" entry in _OTHER_CHANNELS below).
    is_wdl = bool(_CASH_WDL.search(text)) and not _CHEQUE_OR_CLEARING_WORD.search(text)
    if is_dep and not is_wdl:
        info.category = CASH_DEPOSIT
        info.channel = "CDM" if re.search(r"\b(?:cdm|bna)\b", text, re.I) else "Cash"
        info.counterparty = _cash_counterparty(text)
        info.reference = _generic_reference(text)
        info.confidence = "high"
        if direction == "Debit":
            info.flags.append("cash_deposit_text_on_debit")
        return info
    if is_wdl and not is_dep:
        info.category = CASH_WITHDRAWAL
        info.channel = "ATM" if re.search(r"\b(?:atm|nfs|atw|nwd|eaw|awb)\b", text, re.I) else "Cash"
        info.reference = _generic_reference(text)
        info.confidence = "high"
        if direction == "Credit":
            info.flags.append("cash_withdrawal_text_on_credit")
        return info

    # 5. Weaker cash signals that depend on direction.
    if direction == "Debit":
        if _CHEQUE_WORD.search(text) and _SELF_WORD.search(text):
            info.category = CASH_WITHDRAWAL
            info.channel = "Self cheque"
            info.confidence = "medium"
            info.flags.append("verify_self_cheque_is_cash")
            info.reference = _generic_reference(text)
            return info
        if _ATM_WORD.search(text) or _CASH_WORD.search(text):
            info.category = CASH_WITHDRAWAL
            info.channel = "ATM" if _ATM_WORD.search(text) else "Cash"
            info.confidence = "medium"
            info.reference = _generic_reference(text)
            return info
    elif direction == "Credit" and _CASH_WORD.search(text):
        info.category = CASH_DEPOSIT
        info.channel = "Cash"
        info.counterparty = _cash_counterparty(text)
        info.confidence = "medium"
        info.reference = _generic_reference(text)
        return info
    elif direction == "Unknown":
        # The row's Debit/Credit column was blank, ambiguous, or couldn't be resolved by balance
        # reconciliation (no usable running-balance column). Previously this branch only left a
        # flag and fell through to "Other" - a real cash withdrawal with an unreadable direction
        # column was silently dropped instead of surfaced. "ATM" narration is not used for
        # deposits by Indian banks (deposits say CDM/BNA, handled in step 4 above), so it is safe
        # to classify it as a withdrawal outright, just at low confidence with a flag for review.
        # A bare "CASH" word alone is genuinely ambiguous (could be either direction) and is left
        # unclassified, flagged, rather than guessed.
        if _ATM_WORD.search(text) or (_CHEQUE_WORD.search(text) and _SELF_WORD.search(text)):
            info.category = CASH_WITHDRAWAL
            info.channel = "ATM" if _ATM_WORD.search(text) else "Self cheque"
            info.confidence = "low"
            info.reference = _generic_reference(text)
            info.flags.append("direction_unknown_classified_from_narration")
            return info
        if _CASH_WORD.search(text):
            info.flags.append("cash_direction_unknown")

    # 6. Everything else. Record what it is so the UI can say what was left out.
    for label, pattern in _OTHER_CHANNELS:
        if pattern.search(text):
            info.channel = label
            break
    info.reference = parts.rrn or parts.utr or _generic_reference(text)
    info.direction_hint = parts.hint
    return info
