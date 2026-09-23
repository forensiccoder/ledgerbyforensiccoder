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
# Headerless column inference
#
# Some scans genuinely never show a header row - e.g. the page carrying it wasn't included in the
# scan. Dates and amounts still have a recognisable shape and a stable position even without a
# label, and a Balance column is self-verifying: it is the one column where
# ``balance[i] == balance[i-1] +/- amount[i]`` actually holds across rows. This tries every
# plausible role assignment and keeps the one that reconciles - and only that one, above a
# confidence bar - rather than silently mislabelling a Debit as a Credit.
# ----------------------------------------------------------------------------------------------
_DECIMAL_AMOUNT = re.compile(r"\.\d{1,2}\s*(?:cr|dr)?\.?\s*\|?\s*$", re.I)
_MIN_SAMPLES = 5
_MIN_RECONCILE_SCORE = 0.3


def _cluster_by_anchor(items: list[Word], anchor, gap: float) -> list[list[Word]]:
    ordered = sorted(items, key=anchor)
    clusters: list[list[Word]] = []
    for w in ordered:
        a = anchor(w)
        if clusters and a - anchor(clusters[-1][-1]) <= gap:
            clusters[-1].append(w)
        else:
            clusters.append([w])
    return clusters


def infer_columns_from_data(lines: list[Line], page: int, ocr: bool) -> list[Column] | None:
    all_words = [w for line in lines for w in line.words]
    date_words = [w for w in all_words if parse_date(w.text, strict=True)]
    if len(date_words) < _MIN_SAMPLES:
        return None
    date_gap = max(20.0, 4 * statistics.median(w.height for w in date_words))
    date_clusters = sorted(_cluster_by_anchor(date_words, lambda w: w.x0, date_gap), key=len, reverse=True)
    date_col_words = date_clusters[0]
    if len(date_col_words) < _MIN_SAMPLES:
        return None
    date_x0 = min(w.x0 for w in date_col_words)
    date_x1 = max(w.x1 for w in date_col_words)

    # Amounts with paise (a decimal point + 1-2 digits) are real transaction values; a bare
    # integer at a similar position is usually a transaction-type code printed in its own trailing
    # column, not an amount - filtering to the decimal form keeps that code column from being
    # mistaken for a fourth money column.
    amount_words = [w for w in all_words if is_money_like(w.text) and _DECIMAL_AMOUNT.search(w.text)]
    if len(amount_words) < _MIN_SAMPLES:
        return None
    amt_gap = max(20.0, 4 * statistics.median(w.height for w in amount_words))
    num_clusters = [c for c in _cluster_by_anchor(amount_words, lambda w: w.x1, amt_gap) if len(c) >= _MIN_SAMPLES]
    if len(num_clusters) not in (2, 3):
        return None  # only the (debit, credit, balance) and (amount, balance) shapes are handled
    num_clusters.sort(key=lambda c: min(w.x0 for w in c))
    num_ranges = [(min(w.x0 for w in c), max(w.x1 for w in c)) for c in num_clusters]

    narration_x0, narration_x1 = date_x1 + 4, min(r[0] for r in num_ranges) - 4
    if narration_x1 <= narration_x0:
        return None
    date_col = Column("date", LABELS["date"], date_x0, date_x1)
    narration_col = Column("narration", LABELS["narration"], narration_x0, narration_x1)

    # A trailing code column (a bank's internal transaction-type number, printed right of Balance)
    # was deliberately excluded from num_ranges above by requiring paise - but without a column of
    # its own it would nearest-match Balance and get appended onto real balance values, corrupting
    # them. Giving it an explicit role-less column (like a real header's trailing branch-code
    # column) makes the row builder drop it instead, the same as it already does for those.
    trailing_x0 = max(r[1] for r in num_ranges) + 4
    trailing_words = [w for w in all_words if is_money_like(w.text) and w.x0 > trailing_x0]
    extra_cols: list[Column] = []
    if trailing_words:
        extra_cols.append(Column(None, "", trailing_x0, max(w.x1 for w in trailing_words) + 4))

    def trial_columns(role_by_range: dict[int, str]) -> list[Column]:
        cols = [date_col, narration_col]
        cols.extend(Column(role, LABELS[role], *num_ranges[i]) for i, role in role_by_range.items())
        cols.extend(extra_cols)
        return cols

    def score(role_by_range: dict[int, str]) -> tuple[float, int]:
        columns = trial_columns(role_by_range)
        rows = _rows_from_columns(lines, columns, page, ocr, start=0, warnings=[])
        # Must match _rows_from_columns' own out_cols filter - a role-less column (the trailing
        # code column here) doesn't get an output cell, so isn't in row.cells at all.
        out_cols = [c for c in columns if c.role and c.role != "serial"]
        role_index = {c.role: i for i, c in enumerate(out_cols)}
        bal_i, deb_i = role_index.get("balance"), role_index.get("debit")
        cred_i, amt_i = role_index.get("credit"), role_index.get("amount")
        matches = total = 0
        prev_bal = None
        for r in rows[1:]:  # skip the synthetic header row
            bal = parse_money(r.cells[bal_i]) if bal_i is not None else None
            bal = bal.signed if bal else None
            if bal is None:
                continue
            if prev_bal is not None:
                debit = parse_money(r.cells[deb_i]) if deb_i is not None else None
                credit = parse_money(r.cells[cred_i]) if cred_i is not None else None
                amount = parse_money(r.cells[amt_i]) if amt_i is not None else None
                if debit or credit:
                    total += 1
                    expected = prev_bal - (debit.value if debit else 0) + (credit.value if credit else 0)
                    matches += abs(expected - bal) < 1
                elif amount:
                    total += 1
                    matches += abs(abs(bal - prev_bal) - amount.value) < 1
            prev_bal = bal
        return (matches / total, total) if total else (0.0, 0)

    best: tuple[float, dict[int, str]] | None = None
    n = len(num_ranges)
    for bal_i in range(n):
        others = [i for i in range(n) if i != bal_i]
        combos = (
            [{bal_i: "balance", others[0]: "debit", others[1]: "credit"},
             {bal_i: "balance", others[1]: "debit", others[0]: "credit"}]
            if n == 3 else [{bal_i: "balance", others[0]: "amount"}]
        )
        for combo in combos:
            s, total = score(combo)
            if total >= _MIN_SAMPLES and (best is None or s > best[0]):
                best = (s, combo)

    if best is None or best[0] < _MIN_RECONCILE_SCORE:
        return None
    return trial_columns(best[1])


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


_COMPLETE_PAISE = re.compile(r"\.\d{2}$")


def _fix_ocr_number(text: str) -> str:
    m = re.match(r"^(.*?)(\s*(?:cr|dr)\.?)?$", text, re.I)
    body, suffix = m.group(1), m.group(2) or ""
    # A table's vertical ruling line next to the column is often misread as a stray "|"/"l"/"I"
    # glued onto the very end of the number - if the amount is already complete (ends in two paise
    # digits) without it, it's noise to drop, not a digit: the confusable-translation below would
    # otherwise turn it into a spurious extra "1", e.g. "801601.67|" -> "801601.671".
    if body and body[-1] in "|lI" and _COMPLETE_PAISE.search(body[:-1]):
        body = body[:-1]
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
        inferred = infer_columns_from_data(lines, page, ocr)
        if inferred is None:
            return []
        columns = state.columns = inferred
        state.warnings.append(
            f"page {page}: no header row was found, so columns were inferred from word position "
            "and running-balance math instead - double-check amounts and Debit/Credit against the "
            "original statement."
        )
    return _rows_from_columns(lines, columns, page, ocr, start, state.warnings)


def _rows_from_columns(
    lines: list[Line], columns: list[Column], page: int, ocr: bool, start: int, warnings: list[str],
) -> list[RawRow]:
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
    # Whether the row's own date line already carried narration text of its own (as opposed to
    # relying entirely on a wrapped prefix/continuation line) - see the note on ``run_end`` below.
    current_self_narrated = False
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
            current_self_narrated = False
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
            # A lookahead tells the two apart: if a run of one or more such narration-only lines is
            # immediately followed by a line with a date that would otherwise contribute no
            # narration, the whole run belongs to it, not to the row already in progress - some
            # banks wrap that opening narration across two or more lines, not just one. This has to
            # be checked even when there is no ``current`` row (e.g. right after a stray
            # amount-without-date line reset it below) - the prefix run belongs to the row that
            # follows it, not to whatever row happened to come before.
            extra_numeric = [w for w in ws if is_money_like(w.text) and w.x1 >= first_num_x0 - 3 and text_cols]
            if extra_numeric:
                if current is not None:
                    warnings.append(f"page {page}: a line with amounts but no date was skipped: {line.text[:60]!r}")
                    current = None
                    current_self_narrated = False
                i += 1
                continue
            run_end = i
            while run_end < len(seg):
                rline = seg[run_end]
                if _FOOTER.search(rline.text) or find_head_date(rline.words) is not None or _BALANCE_MARKER.match(rline.text):
                    break
                if [w for w in rline.words if is_money_like(w.text) and w.x1 >= first_num_x0 - 3 and text_cols]:
                    break
                run_end += 1
            nxt = seg[run_end] if run_end < len(seg) else None
            if nxt is not None and not _FOOTER.search(nxt.text):
                nxt_head_date = find_head_date(nxt.words)
                # Whether the *open* row already has its own narration (not the upcoming one) is
                # the signal that actually distinguishes "this run is a continuation of the row in
                # progress" from "this run is the opening of the row that follows": a row whose own
                # date line already carried narration doesn't need more appended to it, freeing the
                # run to belong to what comes next - even if that next row's date line *also*
                # carries some trailing narration of its own (some banks wrap narration around the
                # date line, split across a prefix run and a same-line tail, not purely before or
                # after it - checking the upcoming line instead of the current one, as this used
                # to, gets exactly that case backwards). A row with no narration of its own (relying
                # entirely on a wrapped prefix or continuation) is still assumed to want this run.
                if nxt_head_date is not None and (current is None or current_self_narrated):
                    for rline in seg[i:run_end]:
                        pending_prefix.extend(rline.words)
                    i = run_end
                    continue
            if current is None:
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
            current_self_narrated = False
            i += 1
            continue
        rows.append(RawRow(cells, page))
        current = cells
        current_self_narrated = head_date is not None and has_own_narration(ws)
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
