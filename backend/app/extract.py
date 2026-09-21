"""Dispatch an uploaded file to the right extractor (by content first, extension second)."""
from __future__ import annotations

from .errors import StatementError
from .extract_pdf import extract_pdf
from .extract_tabular import read_csv, read_excel
from .models import ExtractResult


def detect_kind(data: bytes, filename: str) -> str:
    head = data[:8]
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else ""
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "xlsx"
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if ext in {"csv", "txt", "tsv"}:
        return "csv"
    return "unknown"


def extract_statement(data: bytes, filename: str, password: str | None = None, ocr_mode: str = "auto") -> ExtractResult:
    if not data:
        raise StatementError("EMPTY_FILE", "The uploaded file is empty.")
    kind = detect_kind(data, filename)
    if kind == "pdf":
        return extract_pdf(data, password, ocr_mode)
    if kind in {"xlsx", "xls"}:
        return read_excel(data, filename)
    if kind == "csv":
        return read_csv(data)
    raise StatementError("UNSUPPORTED_FILE", "Please upload a PDF, CSV, XLSX or XLS bank statement.")
