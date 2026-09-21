# LedgerLens backend

A Python backend for forensic analysis of Indian bank statements (PDF / CSV / XLSX), replacing
the browser-only TypeScript parser in `app/page.tsx` with a proper extraction + classification
pipeline.

## Stack

| Concern                          | Library                                   |
| --------------------------------- | ------------------------------------------ |
| PDF table/text extraction         | `pdfplumber` (+ a custom word-position layout engine for borderless statements) |
| Cleaning, filtering, Excel export  | `pandas`, `openpyxl`                       |
| Narration -> type/counterparty/ref | `re` (deterministic, auditable regex — no LLM) |
| Scanned statements                | `pytesseract` (Tesseract OCR)              |
| API                                | `FastAPI`                                  |

**On PyMuPDF / Camelot / OCRmyPDF:** `pdfplumber` (built on `pdfminer.six` + `pypdfium2`) already
covers both text extraction and ruled-table extraction, and the custom `app/layout.py` engine
handles borderless tables from raw word positions — the case Camelot's stream/lattice modes exist
for. Adding a second table library on top would mean reconciling two different extraction results
for no clear gain, so it wasn't used; `camelot-py` is a drop-in alternative if you hit a layout the
current engine doesn't handle well. Likewise `pytesseract` was used directly rather than
`OCRmyPDF` — OCRmyPDF's job is producing a searchable PDF file as an artifact, whereas here OCR
output only needs to become table rows in memory, so calling Tesseract directly avoids an
unnecessary PDF-rewrite round trip.

## Layout

```
backend/
  app/
    money.py          Decimal-safe amount + date parsing (₹/Rs/INR, Indian digit grouping, Cr/Dr, parentheses)
    headers.py         Maps arbitrary column headers ("Withdrawal Amt.", "DR", "Chq./Ref.No." ...) to roles
    narration.py        Regex classifier: category, channel, counterparty, VPA, IFSC, reference
    layout.py            Word-position table reconstruction for borderless / no-grid PDFs
    ocr.py                pytesseract wrapper (auto-rotation, confidence scoring)
    extract_pdf.py         Per-page: pdfplumber table -> word layout -> OCR fallback chain
    extract_tabular.py      CSV / XLSX / XLS readers
    extract.py                File-type dispatch (magic bytes, not just extension)
    normalize.py               Raw rows -> Transaction objects (direction, wrapped narration, balances)
    reconcile.py                 Balance-chain cross-check; corrects direction; detects statement order
    service.py                    Orchestrates the pipeline; pandas summaries; JSON shaping
    export.py                      Excel (Summary + per-category sheets) / CSV export, formula-injection safe
    main.py                         FastAPI app: /api/analyze, /api/export, /api/health
  tests/
    fixtures.py          Synthetic statement generator (reportlab) - HDFC bordered, SBI borderless,
                          Kotak single-Dr/Cr-column, zero-filled, descending order, scanned, encrypted
```

## Running it

```bash
cd backend
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

### System dependencies (OS packages)

`pip install` alone is not enough — several packages in `requirements.txt` are thin wrappers
around native binaries that must be installed separately at the OS level:

| Package (pip)          | Needs (system binary) | macOS (Homebrew)                    | Debian/Ubuntu (apt)                          |
| ----------------------- | ---------------------- | ------------------------------------ | ---------------------------------------------- |
| `pytesseract`           | `tesseract`             | `brew install tesseract`              | `apt install tesseract-ocr`                     |
| `camelot-py`             | Ghostscript              | `brew install ghostscript`             | `apt install ghostscript`                        |
| `ocrmypdf`                | Ghostscript + Poppler     | `brew install ghostscript poppler`      | `apt install ghostscript poppler-utils`           |

Without these, the pip packages import fine but fail at runtime the first time they actually try
to OCR a page or extract a table (e.g. `pytesseract.TesseractNotFoundError`, or Camelot/OCRmyPDF
erroring out looking for `gs`/`pdftoppm`). Install them before deploying anywhere OCR or
Camelot/OCRmyPDF extraction is expected to run, including CI and Docker images.

Then `POST /api/analyze` with `multipart/form-data`: `file` (required), `password` (optional, for
encrypted PDFs), `ocrMode` (`auto` | `force` | `off`, default `auto`). `POST /api/export` takes
the same fields plus `format` (`xlsx` | `csv`) and streams back a download.

Set `LEDGERLENS_ALLOWED_ORIGINS` (comma-separated) to the real frontend origin(s) in production;
it defaults to `http://localhost:3000`.

## Testing

`pytest` and `PyMuPDF` could not be installed in the sandbox used to build this (no package-index
access), so the pipeline was instead verified directly against `tests/fixtures.py`-generated
statements covering every layout below, and the HTTP layer was verified with a Starlette test
client (FastAPI's own underlying framework) exercising the exact same multipart/response code
paths `main.py` uses. Once you `pip install -r requirements-dev.txt` in a networked environment,
wrap these same checks in proper `pytest` test functions — the manual checks in
`tests/fixtures.py` and the scenarios below translate directly.

Verified end-to-end (extraction -> normalisation -> narration classification -> reconciliation):

- HDFC-style bordered table (with and without zero-filled Debit/Credit cells)
- SBI-style borderless table, ascending and descending statement order
- Kotak-style single "Withdrawal (Dr)/Deposit (Cr)" column with `1,234.00(Dr)` formatting
- CSV export with quoted, comma-grouped amounts
- A scanned (image-only) PDF via the OCR path, including a low-confidence-page warning
- Password-protected PDFs: missing password, wrong password, and correct password
- Excel/CSV export, including formula-injection sanitisation of narration/beneficiary cells

## Known limitations

- OCR language pack: only `eng` is installed in the sandbox this was built in. Statements with
  Hindi/regional-script text need the matching Tesseract language pack (`apt install
  tesseract-ocr-hin`, etc.) plus `LEDGERLENS_OCR_LANG=eng+hin`.
- Legacy `.xls` needs `xlrd`, which isn't in `requirements.txt` by default (most banks now export
  `.xlsx`); uncomment it if you need it.
- The narration parser is tuned against the major public/private banks (SBI, HDFC, ICICI, Axis,
  Kotak, and the common NBFC/PSU formats via `BANK_CODES` in `narration.py`) - an unusual bank's
  phrasing may land in "Other" and should be reviewed/extended there.
