"""PDF statement extraction.

Per page, in order of preference:
  1. pdfplumber table extraction (works when the statement has ruling lines);
  2. word-position layout (works for borderless statements);
  3. OCR + the same word-position layout (scanned pages with no text layer).
"""
from __future__ import annotations

import io
import os

import pdfplumber

from .errors import StatementError, password_required, wrong_password
from .headers import is_header_row
from .layout import _FOOTER, LayoutState, Word, layout_page, plan_ocr_columns
from .models import ExtractResult, RawRow
from .money import parse_date, parse_money
from .ocr import ocr_words

MAX_PAGES = int(os.environ.get("LEDGERLENS_MAX_PAGES", "300"))
MIN_TEXT_CHARS = 25  # fewer characters than this on a page => treat the page as an image


def _open(data: bytes, password: str | None):
    try:
        return pdfplumber.open(io.BytesIO(data), password=password or None)
    except Exception as exc:
        name = type(exc).__name__
        inner = exc.args[0] if exc.args else None
        inner_name = type(inner).__name__ if inner is not None else ""
        if "PasswordIncorrect" in name or "PasswordIncorrect" in inner_name or "password" in str(exc).lower():
            raise (wrong_password() if password else password_required()) from exc
        raise StatementError("BAD_PDF", "That file could not be read as a PDF.") from exc


def _clean_cell(value: object) -> str:
    return " ".join(str(value).split()) if value is not None else ""


def _bucket_row(
    texts: list[str], geoms: list[tuple[float, float, float, float] | None],
    col_ranges: list[tuple[float, float] | None],
) -> list[str]:
    """Realign one row's cells to the header's column x-ranges by nearest position, rather than
    trusting that this row's cells line up index-for-index with the header's (see the note on
    ``table_col_ranges`` in layout.py - pdfplumber's own column-boundary detection can split or
    merge a column on a per-row basis, not just per-page).
    """
    out = [""] * len(col_ranges)
    targets = [i for i, r in enumerate(col_ranges) if r is not None]
    if not targets:
        return [t.strip() for t in texts][: len(col_ranges)]
    for text, geom in zip(texts, geoms):
        text = (text or "").strip()
        if not text or geom is None:
            continue
        mid = (geom[0] + geom[2]) / 2
        idx = min(targets, key=lambda i: abs(mid - (col_ranges[i][0] + col_ranges[i][1]) / 2))  # type: ignore[index]
        out[idx] = f"{out[idx]} {text}".strip()
    return out


def _table_rows(page, page_no: int, state: LayoutState) -> list[RawRow] | None:
    """Rows from pdfplumber's line-based table finder, or None if it is not a usable statement table.

    Uses ``find_tables()`` (which gives each cell's bounding box, not just its text) rather than
    the simpler ``extract_tables()``, because that geometry is what makes position-based
    realignment in ``_bucket_row`` possible - the text-only API doesn't expose enough to detect,
    let alone correct, the column drift described there.
    """
    try:
        tables = page.find_tables()
    except Exception:
        return None
    rows: list[RawRow] = []
    header_here = False
    for table in tables or []:
        try:
            texts = table.extract()
        except Exception:
            continue
        for text_row, cell_row in zip(texts, table.rows):
            cells = [_clean_cell(c) for c in text_row]
            if not any(cells):
                continue
            joined = " ".join(cells)
            if _FOOTER.search(joined):
                continue  # page-number / registered-office / legend boilerplate, not a transaction
            if is_header_row(cells):
                header_here = True
                state.table_header_cells = cells
                state.table_col_ranges = [
                    (g[0], g[2]) if g is not None else None for g in cell_row.cells
                ]
                rows.append(RawRow(cells, page_no))
                continue
            if state.table_col_ranges:
                # Deliberately not gated on the row having the same cell count as the header -
                # that mismatch (an extra or missing column split) is exactly the case this
                # realignment exists to correct; see _bucket_row.
                cells = _bucket_row(cells, list(cell_row.cells), state.table_col_ranges)
            rows.append(RawRow(cells, page_no))
    if not rows:
        return None
    if header_here:
        state.table_header_seen = True
    elif not state.table_header_seen:
        return None  # a table with no recognisable header and none seen before: not a statement
    dated = sum(
        1 for r in rows
        if any(parse_date(c, strict=True) for c in r.cells[:3]) and any(parse_money(c) for c in r.cells[3:])
    )
    return rows if dated else None


def _pdf_words(page, fitz_page=None) -> list[Word]:
    # pdfplumber runs words together on statements that draw spaces without advancing the cursor
    # ("UPI-AMARCHAND"); PyMuPDF keeps them, so prefer it for the word list when it is available.
    if fitz_page is not None:
        try:
            fwords = [
                Word(w[4], w[0], w[2], w[1], w[3])
                for w in fitz_page.get_text("words")
                if w[4].strip()
            ]
            if fwords:
                return fwords
        except Exception:
            pass
    return [
        Word(w["text"], w["x0"], w["x1"], w["top"], w["bottom"])
        for w in page.extract_words(x_tolerance=2, y_tolerance=2, keep_blank_chars=False)
    ]


def _fitz_open(data: bytes, password: str | None):
    try:
        import fitz

        doc = fitz.open(stream=data, filetype="pdf")
        if doc.needs_pass and not doc.authenticate(password or ""):
            return None
        return doc
    except Exception:
        return None


def _fitz_page(doc, page_no: int):
    try:
        return doc[page_no - 1] if doc is not None else None
    except Exception:
        return None


def extract_pdf(data: bytes, password: str | None = None, ocr_mode: str = "auto") -> ExtractResult:
    """``ocr_mode``: "auto" (OCR only pages with no text layer), "force", or "off"."""
    state = LayoutState()
    rows: list[RawRow] = []
    deferred: list[tuple[int, int, list[Word]]] = []  # (insert position, page, OCR words)
    methods: set[str] = set()
    ocr_pages: list[int] = []
    warnings: list[str] = []
    fdoc = _fitz_open(data, password)
    with _open(data, password) as pdf:
        total = len(pdf.pages)
        for page_no, page in enumerate(pdf.pages, start=1):
            if page_no > MAX_PAGES:
                warnings.append(f"Only the first {MAX_PAGES} pages were processed.")
                break
            try:
                has_text = len(page.chars) >= MIN_TEXT_CHARS
                if ocr_mode == "force" or (ocr_mode == "auto" and not has_text):
                    words, conf = ocr_words(page)
                    # Laid out after the whole document has been read (see plan_ocr_columns).
                    deferred.append((len(rows), page_no, words))
                    methods.add("ocr")
                    ocr_pages.append(page_no)
                    if conf < 70:
                        warnings.append(
                            f"Page {page_no}: OCR confidence is low ({conf:.0f}%). "
                            "Check these rows against the original statement."
                        )
                elif not has_text:
                    warnings.append(f"Page {page_no} has no text layer and OCR is switched off.")
                else:
                    table = _table_rows(page, page_no, state)
                    if table is not None:
                        rows.extend(table)
                        methods.add("pdf-table")
                    else:
                        rows.extend(layout_page(_pdf_words(page, _fitz_page(fdoc, page_no)), page_no, state))
                        methods.add("pdf-text")
            finally:
                page.flush_cache()
    if deferred:
        plans = plan_ocr_columns([words for _, _, words in deferred])
        results: list[list[RawRow]] = []
        for (position, page_no, words), columns in zip(deferred, plans):
            results.append(layout_page(words, page_no, state, ocr=True, columns_override=columns))
        # Spliced back-to-front so earlier insert positions stay valid.
        for (position, _, _), page_rows in reversed(list(zip(deferred, results))):
            rows[position:position] = page_rows
    warnings.extend(state.warnings[:10])
    return ExtractResult(rows, "+".join(sorted(methods)) or "pdf-text", total, ocr_pages, warnings)
