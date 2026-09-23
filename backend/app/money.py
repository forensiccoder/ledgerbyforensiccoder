"""Amount and date parsing for Indian bank statements.

Amounts are parsed into ``Decimal`` (never float) so totals and balance checks are exact.
Dates are day-first (dd/mm/yyyy), which is the Indian convention.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_MONEY_RE = re.compile(
    r"""^\s*
    (?P<open>\()?\s*
    (?P<sign>[-+])?\s*
    (?:₹|rs\.?|inr)?\s*
    (?P<sign2>[-+])?\s*
    (?P<num>\d[\d,]*(?:\.\d+)?|\.\d+)\s*
    (?:\(?\s*(?P<suffix>cr|dr)\.?\s*\)?)?\s*
    (?P<close>\))?
    \s*\|?\s*$""",
    # The trailing "\|?" tolerates a stray "|" glued directly onto the number with no space - a
    # table's vertical ruling line next to the Balance column is a common OCR misread on scanned
    # statements, and without this it silently drops the value instead of just the noise character.
    re.I | re.X,
)
_EMPTY_MARKERS = {"", "-", "--", "—", "–", "na", "n/a", "nil", "null", "none"}


@dataclass(frozen=True)
class Money:
    value: Decimal  # absolute value
    negative: bool  # explicit minus sign or (parentheses)
    suffix: str | None  # "Cr" / "Dr" if the cell carried one

    @property
    def signed(self) -> Decimal:
        """Signed value as a *balance*: negative for '-' / '(...)' / 'Dr' suffix."""
        return -self.value if (self.negative or self.suffix == "Dr") else self.value


def parse_money(value: object) -> Money | None:
    """Parse a cell into a Money, or None if it is empty / not a number."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return None
        d = Decimal(str(value))
        return Money(abs(d), d < 0, None)
    text = str(value).replace(" ", " ").strip()
    if text.lower() in _EMPTY_MARKERS:
        return None
    m = _MONEY_RE.match(text)
    if not m:
        return None
    try:
        num = Decimal(m["num"].replace(",", ""))
    except InvalidOperation:
        return None
    negative = m["sign"] == "-" or m["sign2"] == "-" or bool(m["open"] and m["close"])
    suffix = m["suffix"].capitalize() if m["suffix"] else None
    return Money(num, negative, suffix)


def is_money_like(text: str) -> bool:
    return parse_money(text) is not None


_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_TIME_TAIL = r"(?:[\sT,]+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)?"
_ISO = r"(?P<iy>\d{4})-(?P<im>\d{2})-(?P<id>\d{2})"
_NUM = r"(?P<d>\d{1,2})[/.\-](?P<m>\d{1,2})[/.\-](?P<y>\d{4}|\d{2})"
_NAMED = r"(?P<nd>\d{1,2})[\s/.\-]*(?P<mon>[A-Za-z]{3,9})\.?[\s,/.\-]*(?P<ny>\d{4}|\d{2})"
_DATE_ANY = re.compile(rf"(?<!\d)(?:{_ISO}|{_NUM}|{_NAMED})(?!\d)")
_DATE_STRICT = re.compile(rf"^\s*(?:{_ISO}|{_NUM}|{_NAMED}){_TIME_TAIL}\s*$")

EXCEL_EPOCH = datetime(1899, 12, 30)


def _year(y: int) -> int:
    return y if y >= 100 else (1900 + y if y >= 70 else 2000 + y)


def _build(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_date(value: object, strict: bool = False) -> date | None:
    """Parse a date cell. With ``strict`` the whole cell must be a date (plus optional time)."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and math.isnan(value):
            return None
        if 20000 < float(value) < 80000:  # Excel serial date
            return (EXCEL_EPOCH.fromordinal(EXCEL_EPOCH.toordinal() + int(value))).date()
        return None
    text = str(value).replace(" ", " ").strip()
    if not text:
        return None
    m = (_DATE_STRICT if strict else _DATE_ANY).search(text) if not strict else _DATE_STRICT.match(text)
    if not m:
        return None
    if m["iy"]:
        return _build(int(m["iy"]), int(m["im"]), int(m["id"]))
    if m["d"]:
        return _build(_year(int(m["y"])), int(m["m"]), int(m["d"]))
    month = _MONTHS.get(m["mon"][:3].lower())
    if not month:
        return None
    return _build(_year(int(m["ny"])), month, int(m["nd"]))
