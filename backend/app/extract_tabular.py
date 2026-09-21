"""CSV / XLSX / XLS statement readers. Returns raw cell rows; normalize.py finds the header."""
from __future__ import annotations

import csv
import io

import pandas as pd

from .errors import StatementError
from .models import ExtractResult, RawRow


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_csv(data: bytes) -> ExtractResult:
    text = _decode(data)
    sample = "\n".join(text.splitlines()[:50])
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    rows = [RawRow([c.strip() for c in row]) for row in csv.reader(io.StringIO(text), dialect)]
    return ExtractResult(rows, "csv")


def read_excel(data: bytes, filename: str) -> ExtractResult:
    try:
        sheets = pd.read_excel(io.BytesIO(data), sheet_name=None, header=None, dtype=object)
    except ImportError as exc:  # legacy .xls needs xlrd
        raise StatementError(
            "UNSUPPORTED_FILE",
            "Reading legacy .xls files needs the 'xlrd' package on the server. "
            "Save the statement as .xlsx or .csv and upload that instead.",
        ) from exc
    except Exception as exc:
        raise StatementError("BAD_SPREADSHEET", "That spreadsheet could not be read.") from exc
    rows: list[RawRow] = []
    for frame in sheets.values():
        for record in frame.itertuples(index=False, name=None):
            cells = [("" if pd.isna(v) else v) for v in record]
            if any(str(c).strip() for c in cells):
                rows.append(RawRow(cells))
    return ExtractResult(rows, "xlsx")
