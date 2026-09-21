"""Turn positioned words (from pdfplumber or OCR) into table rows.

Many bank PDFs have no ruling lines, so pdfplumber's table finder returns nothing. This module
rebuilds the table from word coordinates instead:

1. group words into visual lines;
2. find the header line(s) and derive one column per header cell;
3. assign every word to a column - money-like words by their RIGHT edge (amounts are right-aligned),
   everything else by left edge; dates are recognised by pattern rather than position;
4. a line that starts with a date starts a new transaction; lines without a date are wrapped
   narration and are appended to the transaction above (this is where counterparty names often
   sit, and the old browser parser dropped them).

Because columns come from positions, a blank Debit cell and a "0.00" Debit cell are both handled
correctly - the amount is whatever sits under the Debit header.
"""
from __future__ import annotations

import re
import statistics
from dataclasses import dataclass, field

from .headers import LABELS, NUMERIC_ROLES, classify_header, header_ok
from .models import RawRow
from .money import is_money_like, parse_date, parse_money


@dataclass
class Word:
    text: str
    x0: float
    x1: float
    top: float
    bottom: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def height(self) -> float:
        return max(self.bottom - self.top, 1.0)


@dataclass
class Line:
    words: list[Word]
    mid: float

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)


@dataclass
class Column:
    role: str | None
    label: str
    x0: float
    x1: float


@dataclass
class LayoutState:
    """Column layout carried from page to page (some banks print the header only once)."""

    columns: list[Column] | None = None
    table_header_seen: bool = False  # set by the pdfplumber-table path
    # Column x-ranges learned from the ruled-table header, carried page to page the same way
    # `columns` is for the word-layout path (see extract_pdf._table_rows): pdfplumber's own
    # column-boundary detection can drift row to row within a single table - not just page to
    # page - splitting or merging a column, so later rows are realigned by position against these
    # ranges rather than trusted at face value by raw cell index.
    table_header_cells: list[str] | None = None
    table_col_ranges: list[tuple[float, float] | None] | None = None
    warnings: list[str] = field(default_factory=list)


_FOOTER = re.compile(
    r"page\s*\d+\s*(?:of|/)\s*\d+|^page\s*(?:no\.?)?\s*\d+\s*$|end of statement|computer[\s-]generated|"
    # "Registered & Corporate Office", "Corporate Office", "Regd. Office" etc - banks phrase this
    # differently enough (word order, "&"/"and", abbreviations) that a literal "registered office"
    # match misses most of them, so this matches "registered"/"regd"/"corporate" within a few
    # words of "office" rather than requiring them adjacent.
    r"(?:registered|regd\.?|corporate)\s*(?:[&,]|and)?\s*(?:regd\.?|corporate\s+)?\s*office|"
    r"statement (?:summary|of account)|generated (?:on|by)|legends?\b|"
    r"abbreviations|customer care|toll[\s-]?free|nomination|this is a system|\bgstin\b|"
    r"important (?:notice|information)|unless the constituent|contents of this statement",
    re.I,
)
_BALANCE_MARKER = re.compile(
    r"^\s*(?:opening\s+balance|closing\s+balance|brought\s+forward|carried\s+forward|"
    r"balance\s+(?:b/?f|c/?f|brought|carried)|b/f\b|c/f\b)",
    re.I,
)
_SUFFIX = re.compile(r"^(?:cr|dr)\.?$", re.I)


# ----------------------------------------------------------------------------------------------
# Lines
# ----------------------------------------------------------------------------------------------
def group_lines(words: list[Word]) -> list[Line]:
    if not words:
        return []
    tol = max(2.0, 0.55 * statistics.median(w.height for w in words))
    lines: list[Line] = []
    for w in sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0)):
        mid = (w.top + w.bottom) / 2
        if lines and abs(mid - lines[-1].mid) <= tol:
            line = lines[-1]
            line.words.append(w)
            line.mid = (line.mid * (len(line.words) - 1) + mid) / len(line.words)
        else:
            lines.append(Line([w], mid))
    for line in lines:
        line.words.sort(key=lambda w: w.x0)
        _attach_suffixes(line)
    return lines


def _attach_suffixes(line: Line) -> None:
    """'12,345.00' followed by a separate 'Cr'/'Dr' word becomes one word '12,345.00 Cr'."""
    merged: list[Word] = []
    for w in line.words:
        if merged and _SUFFIX.match(w.text) and is_money_like(merged[-1].text) and w.x0 - merged[-1].x1 < 12:
            prev = merged[-1]
            merged[-1] = Word(f"{prev.text} {w.text}", prev.x0, w.x1, min(prev.top, w.top), max(prev.bottom, w.bottom))
        else:
            merged.append(w)
    line.words = merged


# ----------------------------------------------------------------------------------------------
# Header detection
# ----------------------------------------------------------------------------------------------
def _segment_header(block: list[Line], fuzzy: bool) -> list[Column]:
    tagged = [(li, w) for li, line in enumerate(block) for w in line.words]
    if not tagged:
        return []
    height = statistics.median(w.height for _, w in tagged)
    gap = max(4.0, 0.55 * height)
    tagged.sort(key=lambda t: t[1].x0)
    cells: list[dict] = []
    for li, w in tagged:
        if cells and w.x0 <= cells[-1]["x1"] + gap:
            cells[-1]["words"].append((li, w))
            cells[-1]["x1"] = max(cells[-1]["x1"], w.x1)
        else:
            cells.append({"words": [(li, w)], "x0": w.x0, "x1": w.x1})
    columns: list[Column] = []
    for cell in cells:
        ordered = sorted(cell["words"], key=lambda t: (t[0], t[1].x0))
        text = " ".join(w.text for _, w in ordered)
        columns.append(Column(classify_header(text, fuzzy), text, cell["x0"], cell["x1"]))
    # A role-less fragment right next to a real header ("Amt." after "Withdrawal") belongs to it.
    merged: list[Column] = []
    for col in columns:
        if merged and col.role is None and col.x0 - merged[-1].x1 <= 3 * gap and merged[-1].role is not None:
            merged[-1].x1 = max(merged[-1].x1, col.x1)
            merged[-1].label += " " + col.label
        else:
            merged.append(col)
    seen: set[str] = set()
    for col in merged:  # keep the first column per role
        if col.role in seen:
            col.role = None
        elif col.role:
            seen.add(col.role)
    return merged


def find_header(lines: list[Line], fuzzy: bool = False) -> tuple[int, int, list[Column]] | None:
    for i in range(len(lines)):
        for span in (1, 2, 3):
            block = lines[i:i + span]
            if len(block) < span or (span > 1 and block[-1].mid - block[0].mid > 30):
                break
            columns = _segment_header(block, fuzzy)
            if header_ok({c.role for c in columns}):
                return i, i + span - 1, columns
    return None


# ----------------------------------------------------------------------------------------------
# Row extraction
# ----------------------------------------------------------------------------------------------
def _date_spans(words: list[Word]) -> list[tuple[int, int]]:
    """Index ranges of words that form a date: one word ('01/04/24') or three ('01 Apr 2024')."""
    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(words):
        if parse_date(words[i].text, strict=True):
            spans.append((i, i))
            i += 1
        elif i + 2 < len(words) and parse_date(" ".join(w.text for w in words[i:i + 3]), strict=True):
            spans.append((i, i + 2))
            i += 3
        else:
            i += 1
    return spans


def _fix_ocr_number(text: str) -> str:
    m = re.match(r"^(.*?)(\s*(?:cr|dr)\.?)?$", text, re.I)
    body, suffix = m.group(1), m.group(2) or ""
    fixed = body.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"}))
    return fixed + suffix if parse_money(fixed) is not None else text


def layout_page(words: list[Word], page: int, state: LayoutState, ocr: bool = False) -> list[RawRow]:
    lines = group_lines(words)
    found = find_header(lines, fuzzy=ocr)
    start = 0
    if found:
        hi, hj, columns = found
        state.columns = columns
        start = hj + 1
    elif state.columns:
        columns = state.columns
    else:
        return []

    numeric_cols = [c for c in columns if c.role in NUMERIC_ROLES]
    if not numeric_cols:
        return []
    first_num_x0 = min(c.x0 for c in numeric_cols)
    right_cols = [c for c in columns if c.x0 >= first_num_x0 - 3]  # numeric + trailing role-less cols
    text_cols = [c for c in columns if c.role in {"narration", "ref", "counterparty"}]
    value_date_col = next((c for c in columns if c.role == "value_date"), None)
    date_col = next((c for c in columns if c.role == "date"), None)
    # Columns a "does this token start a new transaction row" check should choose between: is a
    # candidate date-shaped token actually under the Date column, or under Narration/Ref (a wrapped
    # continuation line whose first word happens to parse as a date, e.g. a reference fragment like
    # "05/06/1234")? Nearest-left-edge, not a fixed tolerance band, is what tells them apart -
    # Narration columns vary too much in width for any fixed tolerance to be both safe and correct.
    _left_cols = [c for c in columns if c.role in {"date", "value_date", "narration", "ref", "counterparty"}]
    has_serial = any(c.role == "serial" for c in columns)
    narr_col = next((c for c in text_cols if c.role == "narration"), None)
    out_cols = [c for c in columns if c.role and c.role != "serial"]
    index_of = {id(c): i for i, c in enumerate(out_cols)}

    rows: list[RawRow] = [RawRow([LABELS.get(c.role, c.label) for c in out_cols], page)]
    current: list[str] | None = None
    pending_prefix: list[Word] = []

    def blank() -> list[str]:
        return [""] * len(out_cols)

    def put(cells: list[str], col: Column | None, text: str) -> None:
        if col is None or id(col) not in index_of:
            return
        k = index_of[id(col)]
        cells[k] = f"{cells[k]} {text}".strip()

    def find_head_date(ws: list[Word]) -> tuple[int, int] | None:
        spans = _date_spans(ws)
        return next(
            (
                s for s in spans
                if s[0] <= 1
                and ws[s[0]].x0 < first_num_x0
                and (
                    date_col is None
                    or not _left_cols
                    or min(_left_cols, key=lambda c: abs(ws[s[0]].x0 - c.x0)) is date_col
                )
            ),
            None,
        )

    def has_own_narration(ws: list[Word]) -> bool:
        """Would this line, taken on its own, contribute any narration text? A line that is only a
        date plus money values (no other words) is one whose narration was printed on a separate
        line instead - see the note on ``pending_prefix`` below."""
        used: set[int] = set()
        for s in _date_spans(ws):
            used.update(range(s[0], s[1] + 1))
        return any(idx not in used and not is_money_like(w.text) for idx, w in enumerate(ws))

    seg = lines[start:]
    i = 0
    while i < len(seg):
        line = seg[i]
        ws = line.words
        if _FOOTER.search(line.text):
            current = None
            i += 1
            continue
        spans = _date_spans(ws)
        head_date = find_head_date(ws)
        marker = bool(_BALANCE_MARKER.match(line.text))
        if head_date is None and not marker:
            # Continuation of the previous transaction's narration / reference - or, on at least one
            # real bank's layout (Fincare), the *opening* narration of the transaction whose own
            # date+amount line is printed next, with no narration text of its own on that line (the
            # amount line is sandwiched between the narration's start and its wrapped continuation).
            # A one-line lookahead tells the two apart: if the very next line has a date and would
            # otherwise contribute no narration, this line belongs to it, not to the row already in
            # progress.
            if current is None:
                i += 1
                continue
            extra_numeric = [w for w in ws if is_money_like(w.text) and w.x1 >= first_num_x0 - 3 and text_cols]
            if extra_numeric:
                state.warnings.append(f"page {page}: a line with amounts but no date was skipped: {line.text[:60]!r}")
                current = None
                i += 1
                continue
            nxt = seg[i + 1] if i + 1 < len(seg) else None
            if nxt is not None and not _FOOTER.search(nxt.text):
                nxt_head_date = find_head_date(nxt.words)
                if nxt_head_date is not None and not has_own_narration(nxt.words):
                    pending_prefix.extend(ws)
                    i += 1
                    continue
            for w in ws:
                col = _text_column(w, text_cols, narr_col, has_serial)
                put(current, col, w.text)
            i += 1
            continue

        cells = blank()
        if pending_prefix:
            for w in pending_prefix:
                col = _text_column(w, text_cols, narr_col, has_serial)
                put(cells, col, w.text)
            pending_prefix = []
        used: set[int] = set()
        if head_date:
            date_text = " ".join(w.text for w in ws[head_date[0]:head_date[1] + 1])
            used.update(range(head_date[0], head_date[1] + 1))
            put(cells, next((c for c in out_cols if c.role == "date"), None), date_text)
        if value_date_col:
            for s in spans:
                if head_date and s == head_date:
                    continue
                if ws[s[0]].x0 >= value_date_col.x0 - 6 and ws[s[0]].x0 < first_num_x0:
                    used.update(range(s[0], s[1] + 1))
                    put(cells, value_date_col, " ".join(w.text for w in ws[s[0]:s[1] + 1]))
                    break
        for wi, w in enumerate(ws):
            if wi in used:
                continue
            text = w.text
            if is_money_like(text) and w.x1 >= first_num_x0 - 3 and right_cols:
                col = min(right_cols, key=lambda c: abs(w.x1 - c.x1))
                if col.role in NUMERIC_ROLES:
                    put(cells, col, _fix_ocr_number(text) if ocr else text)
                continue  # role-less trailing columns (branch code etc.) are dropped
            put(cells, _text_column(w, text_cols, narr_col, has_serial), text)
        if marker and not head_date:
            # Opening/closing balance line: keep the last number found as the balance.
            nums = [w.text for w in ws if is_money_like(w.text)]
            bal_col = next((c for c in out_cols if c.role == "balance"), None)
            if nums and bal_col is not None:
                cells[index_of[id(bal_col)]] = nums[-1]
            if narr_col is not None:
                own_text = " ".join(w.text for w in ws if not is_money_like(w.text))
                cells[index_of[id(narr_col)]] = f"{cells[index_of[id(narr_col)]]} {own_text}".strip()
            rows.append(RawRow(cells, page))
            current = None
            i += 1
            continue
        rows.append(RawRow(cells, page))
        current = cells
        i += 1
    return rows


def _text_column(w: Word, text_cols: list[Column], narr_col: Column | None, has_serial: bool) -> Column | None:
    """Pick the text column for a word by left edge; drop stray words left of every text column."""
    if not text_cols:
        return None
    candidates = [c for c in text_cols if c.x0 - 3 <= w.x0]
    if candidates:
        return max(candidates, key=lambda c: c.x0)
    if has_serial:
        return None
    return narr_col or text_cols[0]
