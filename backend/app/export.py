"""Export the analysed transactions to Excel or CSV.

Mirrors the old browser export (a Summary sheet plus one sheet per category), so the workbook a
person gets from the Python backend looks the same as before. Every text cell is passed through
``_sanitise`` first: a forensic review tool is a prime formula-injection target (a narration that
happens to start with '=', '+', '-' or '@' would otherwise execute as a formula the moment the
sheet is opened in Excel), so any such cell is neutralised by prefixing a tab character.
"""
from __future__ import annotations

import csv
import io

import pandas as pd
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from .narration import TARGET_CATEGORIES
from .service import display_counterparty

_FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")

COLUMNS = [
    ("date", "Transaction date"),
    ("category", "Category"),
    ("direction", "Direction"),
    ("beneficiary", "Beneficiary / payer"),
    ("amount", "Amount (INR)"),
    ("reference", "Reference / UTR"),
    ("narration", "Statement narration"),
    ("source", "Source file"),
]


def _sanitise(value: object) -> object:
    if isinstance(value, str) and value.startswith(_FORMULA_LEAD):
        return "'" + value  # a leading apostrophe forces Excel/Sheets to treat the cell as text
    return value


def _rows_for(transactions: list[dict], category: str | None) -> list[dict]:
    # The workbook/CSV cover the target categories only; miscellaneous ("Other") rows stay on screen.
    items = [t for t in transactions if (t["category"] == category if category else t["category"] in TARGET_CATEGORIES)]
    out = []
    for t in items:
        out.append({
            "Transaction date": t["date"],
            "Category": t["category"],
            "Direction": t["direction"],
            "Beneficiary / payer": _sanitise(t["beneficiary"]),
            "Amount (INR)": t["amount"],
            "Reference / UTR": _sanitise(t.get("reference") or ""),
            "Statement narration": _sanitise(t["narration"]),
            "Source file": _sanitise(t["source"]),
        })
    return out


def _autofit(ws) -> None:
    for col_cells in ws.columns:
        length = max((len(str(c.value)) for c in col_cells if c.value is not None), default=8)
        ws.column_dimensions[get_column_letter(col_cells[0].column)].width = min(max(length + 2, 10), 60)


def build_workbook(result: dict) -> bytes:
    """Summary sheet + one sheet per target category, matching the app's on-screen categories."""
    transactions = result["transactions"]
    summary_rows = [
        {
            "Category": category,
            "Transactions": info["count"],
            "Total amount (INR)": info["total"],
        }
        for category, info in result["summary"]["byCategory"].items()
    ]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        pd.DataFrame(summary_rows).to_excel(writer, sheet_name="Summary", index=False)
        for category in TARGET_CATEGORIES:
            sheet_name = category[:31]
            df = pd.DataFrame(_rows_for(transactions, category), columns=[c[1] for c in COLUMNS])
            df.to_excel(writer, sheet_name=sheet_name, index=False)
        for ws in writer.book.worksheets:
            for cell in ws[1]:
                cell.font = Font(bold=True)
            _autofit(ws)
    return buf.getvalue()


def build_csv(result: dict) -> bytes:
    """A single flat CSV of every target-category transaction (for a quick re-import / diff)."""
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([label for _, label in COLUMNS])
    for row in _rows_for(result["transactions"], None):
        writer.writerow([row[label] for _, label in COLUMNS])
    return out.getvalue().encode("utf-8-sig")  # BOM so Excel opens Indian-rupee/UTF-8 text correctly
