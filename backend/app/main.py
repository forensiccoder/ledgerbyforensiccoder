"""FastAPI wrapper around the analysis pipeline.

Endpoints:
  POST /api/analyze  - upload a statement, get back categorised transactions + reconciliation
  POST /api/export    - re-run the same analysis and download it as .xlsx or .csv
  GET  /api/health    - liveness/readiness, including whether OCR is available on this server

The pipeline itself (app.service) has no FastAPI/pydantic dependency, so it can be imported and
tested on its own; this file only adapts it to HTTP.
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .errors import StatementError
from .export import build_csv, build_workbook
from .ocr import ocr_available
from .service import analyze

app = FastAPI(title="LedgerLens API", version="1.0.0")

# The Next.js frontend calls this API from the browser. Restrict this list to the frontend's
# real origin(s) in production via the LEDGERLENS_ALLOWED_ORIGINS env var (comma-separated);
# it defaults to localhost dev ports so `npm run dev` works out of the box.
import os

_origins = [o.strip() for o in os.environ.get(
    "LEDGERLENS_ALLOWED_ORIGINS", "http://localhost:3000,http://127.0.0.1:3000"
).split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=False,
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

MAX_UPLOAD_BYTES = int(os.environ.get("LEDGERLENS_MAX_UPLOAD_BYTES", str(25 * 1024 * 1024)))
OcrMode = Literal["auto", "force", "off"]


class ErrorResponse(BaseModel):
    code: str
    message: str


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD_BYTES:
        raise StatementError(
            "FILE_TOO_LARGE",
            f"That file is larger than the {MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit.",
            status=413,
        )
    return data


@app.exception_handler(StatementError)
async def _statement_error_handler(_request, exc: StatementError) -> JSONResponse:
    return JSONResponse(status_code=exc.status, content={"code": exc.code, "message": exc.message})


@app.get("/api/health")
async def health() -> dict:
    return {"status": "ok", "ocrAvailable": ocr_available()}


@app.post("/api/analyze")
async def analyze_endpoint(
    file: UploadFile = File(...),
    password: str | None = Form(None),
    ocrMode: OcrMode = Form("auto"),
) -> dict:
    data = await _read_upload(file)
    return analyze(data, file.filename or "statement", password or None, ocrMode)


@app.post("/api/export")
async def export_endpoint(
    file: UploadFile = File(...),
    password: str | None = Form(None),
    ocrMode: OcrMode = Form("auto"),
    format: Literal["xlsx", "csv"] = Form("xlsx"),
) -> Response:
    data = await _read_upload(file)
    result = analyze(data, file.filename or "statement", password or None, ocrMode)
    stem = (file.filename or "statement").rsplit(".", 1)[0]
    if format == "csv":
        body = build_csv(result)
        media_type, ext = "text/csv", "csv"
    else:
        body = build_workbook(result)
        media_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ext = "xlsx"
    filename = f"ledgerlens-category-review-{stem}.{ext}"
    return Response(
        content=body,
        media_type=media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
