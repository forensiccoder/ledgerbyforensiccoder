"""Plain dataclasses shared across the pipeline."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any


@dataclass
class RawRow:
    """One row of cells straight from a PDF table / OCR layout / CSV / XLSX."""

    cells: list[Any]
    page: int | None = None


@dataclass
class ExtractResult:
    rows: list[RawRow]
    method: str  # pdf-table | pdf-text | ocr | csv | xlsx (joined with "+" if mixed)
    pages: int = 0
    ocr_pages: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class Transaction:
    id: str
    date: date
    amount: Decimal
    narration: str
    source: str
    direction: str = "Unknown"  # Credit / Debit / Unknown
    direction_source: str = "unknown"  # column / indicator / sign / balance / narration / unknown
    balance: Decimal | None = None
    page: int | None = None
    row: int = 0  # 1-based position among parsed transactions (the audit trail back to the statement)
    explicit_reference: str = ""
    explicit_counterparty: str = ""
    # filled in by apply_narration():
    category: str = "Other"
    channel: str = ""
    counterparty: str = ""
    vpa: str = ""
    ifsc: str = ""
    bank: str = ""
    reference: str = ""
    direction_hint: str = ""
    confidence: str = "low"
    flags: list[str] = field(default_factory=list)

    def signed_amount(self) -> Decimal | None:
        if self.direction == "Credit":
            return self.amount
        if self.direction == "Debit":
            return -self.amount
        return None

    def add_flag(self, flag: str) -> None:
        if flag not in self.flags:
            self.flags.append(flag)


@dataclass
class NormalizeResult:
    transactions: list[Transaction]
    opening_balance: Decimal | None
    closing_balance: Decimal | None
    total_rows: int
    warnings: list[str] = field(default_factory=list)
