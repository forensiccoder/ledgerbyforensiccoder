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
SPARSE_PASS_BELOW = float(os.environ.get("LEDGERLENS_OCR_SPARSE_BELOW", "80"))  # mean confidence


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
        if angle in (90, 180, 270) and float(osd.get("orientation_conf", 0)) >= 8:
            return img.rotate(-angle, expand=True, fillcolor="white")
    except Exception:
        pass
    return img


def _flatten_lighting(gray: Image.Image) -> Image.Image:
    """Clean up a photographed page: flatten uneven lighting and remove table ruling lines.

    A phone photo of paper has a smooth, slowly varying background much brighter than the ink;
    estimating that background and dividing it away leaves flat white paper with dark text, which
    Tesseract reads far more accurately than the raw grey photo. Best effort: without OpenCV the
    original image is returned untouched.
    """
    try:
        import cv2
        import numpy as np
    except Exception:
        return gray
    a = np.array(gray)
    background = cv2.medianBlur(cv2.dilate(a, np.ones((25, 25), np.uint8)), 51)
    flat = cv2.divide(a, background, scale=255)
    # Table ruling lines become '|' and '_' noise (and glue onto digits next to them): find long
    # straight strokes and paint them out. Only long ones - text strokes are far shorter.
    ink = cv2.adaptiveThreshold(flat, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY_INV, 41, 15)
    span = max(40, flat.shape[1] // 20)
    horizontal = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (span, 1)))
    vertical = cv2.morphologyEx(ink, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_RECT, (1, span)))
    rules = cv2.dilate(cv2.bitwise_or(horizontal, vertical), np.ones((3, 3), np.uint8))
    flat[rules > 0] = 255
    angle = _skew_angle(horizontal)
    if 0.15 <= abs(angle) <= 8:
        h, w = flat.shape
        matrix = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
        flat = cv2.warpAffine(flat, matrix, (w, h), flags=cv2.INTER_CUBIC, borderValue=255)
    return Image.fromarray(flat)


def _skew_angle(horizontal) -> float:
    """Median tilt (degrees) of the long near-horizontal ruling lines, 0 if there are none."""
    import cv2
    import numpy as np

    lines = cv2.HoughLinesP(horizontal, 1, np.pi / 720, threshold=200,
                            minLineLength=horizontal.shape[1] // 6, maxLineGap=20)
    if lines is None:
        return 0.0
    angles = [
        float(np.degrees(np.arctan2(y2 - y1, x2 - x1)))
        for x1, y1, x2, y2 in lines.reshape(-1, 4)
        if abs(np.degrees(np.arctan2(y2 - y1, x2 - x1))) < 10
    ]
    return float(np.median(angles)) if angles else 0.0


def ocr_words(page, dpi: int = OCR_DPI) -> tuple[list[Word], float]:
    """OCR one pdfplumber page. Returns (words in PDF points, mean word confidence 0-100)."""
    pytesseract = _require_tesseract()
    img = page.to_image(resolution=dpi).original.convert("RGB")
    img = _auto_rotate(img, pytesseract)
    gray = ImageOps.autocontrast(_flatten_lighting(ImageOps.grayscale(img)), cutoff=1)
    scale = 72.0 / dpi
    words, confs = _read_words(pytesseract, gray, 6, scale)
    mean = sum(confs) / len(confs) if confs else 0.0
    if mean < SPARSE_PASS_BELOW:
        # Whole-page "assume one block of text" (psm 6) silently skips cells it takes for
        # background on a photographed page - whole amounts vanish. Sparse-text mode (psm 11) finds
        # scattered words and misses different ones, so add whatever it found that psm 6 did not.
        extra, extra_confs = _read_words(pytesseract, gray, 11, scale)
        for word, conf in zip(extra, extra_confs):
            if not any(_overlaps(word, seen) for seen in words):
                words.append(word)
                confs.append(conf)
        mean = sum(confs) / len(confs) if confs else 0.0
    return words, mean


def _overlaps(a: Word, b: Word) -> bool:
    """Do two word boxes share a meaningful part of the smaller one's area?"""
    w = min(a.x1, b.x1) - max(a.x0, b.x0)
    h = min(a.bottom, b.bottom) - max(a.top, b.top)
    if w <= 0 or h <= 0:
        return False
    smaller = min((a.x1 - a.x0) * (a.bottom - a.top), (b.x1 - b.x0) * (b.bottom - b.top))
    return w * h >= 0.3 * smaller


def _read_words(pytesseract, gray, psm: int, scale: float) -> tuple[list[Word], list[float]]:
    data = pytesseract.image_to_data(
        gray,
        lang=OCR_LANG,
        config=f"--psm {psm} -c preserve_interword_spaces=1",
        output_type=pytesseract.Output.DICT,
    )
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
        # Ruling-line remnants, shadows and paper texture come back as stray punctuation and
        # low-confidence scraps; they only add phantom text lines that push real rows apart.
        if not any(ch.isalnum() for ch in text) or (conf < 25 and len(text) <= 3):
            continue
        x, y, w, h = data["left"][i], data["top"][i], data["width"][i], data["height"][i]
        words.append(Word(text, x * scale, (x + w) * scale, y * scale, (y + h) * scale))
        confs.append(conf)
    return words, confs
