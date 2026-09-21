"""OCR for scanned statement pages (pytesseract + the Tesseract binary).

Pages are rasterised with pdfplumber (which uses pypdfium2), so no extra PDF library is needed.
Word boxes come back in the same shape as the digital-text path so the same layout engine parses
both.
"""
from __future__ import annotations

import os

from PIL import Image, ImageOps

from .errors import StatementError
from .layout import Word

OCR_DPI = int(os.environ.get("LEDGERLENS_OCR_DPI", "300"))
OCR_LANG = os.environ.get("LEDGERLENS_OCR_LANG", "eng")


def ocr_available() -> bool:
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _require_tesseract():
    try:
        import pytesseract

        pytesseract.get_tesseract_version()
        return pytesseract
    except Exception as exc:  # binary missing or pytesseract not installed
        raise StatementError(
            "OCR_UNAVAILABLE",
            "This PDF looks like a scan (no text layer) but OCR is not available on the server. "
            "Install the Tesseract OCR engine (e.g. `apt install tesseract-ocr` or "
            "`brew install tesseract`), or upload the CSV/XLSX statement instead.",
            status=503,
        ) from exc


def _auto_rotate(img: Image.Image, pytesseract) -> Image.Image:
    """Fix 90/180/270-degree scans using Tesseract's orientation detection (best effort)."""
    try:
        osd = pytesseract.image_to_osd(img, output_type=pytesseract.Output.DICT)
        angle = int(osd.get("rotate", 0))
        if angle in (90, 180, 270) and float(osd.get("orientation_conf", 0)) >= 2:
            return img.rotate(-angle, expand=True, fillcolor="white")
    except Exception:
        pass
    return img


def ocr_words(page, dpi: int = OCR_DPI) -> tuple[list[Word], float]:
    """OCR one pdfplumber page. Returns (words in PDF points, mean word confidence 0-100)."""
    pytesseract = _require_tesseract()
    img = page.to_image(resolution=dpi).original.convert("RGB")
    img = _auto_rotate(img, pytesseract)
    gray = ImageOps.autocontrast(ImageOps.grayscale(img), cutoff=1)
    data = pytesseract.image_to_data(
        gray,
        lang=OCR_LANG,
        config="--psm 6 -c preserve_interword_spaces=1",
        output_type=pytesseract.Output.DICT,
    )
    scale = 72.0 / dpi
    words: list[Word] = []
    confs: list[float] = []
    for i, text in enumerate(data["text"]):
        text = (text or "").strip()
        try:
            conf = float(data["conf"][i])
        except (TypeError, ValueError):
            conf = -1
        if not text or conf < 0:
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(text, x * scale, (x + w) * scale, y * scale, (y + h) * scale))
        confs.append(conf)
    return words, (sum(confs) / len(confs) if confs else 0.0)
