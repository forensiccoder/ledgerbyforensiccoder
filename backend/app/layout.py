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
from datetime import date

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
    line_start: bool = False  # first word on its printed line (set by group_lines)

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
    # Some banks (HDFC) chop each narration into fixed-width chunks (40 characters) and then let
    # the printed cell wrap on top of that, so a printed line break is sometimes *inside* a word
    # ("PAYMEN" / "T FROM PHONE") and sometimes at a space. 0 = not detected; else the chunk width.
    hard_wrap: int = 0
    last_date: date | None = None  # last valid row date seen (OCR date repair)
    chunk_len: dict = field(default_factory=dict)  # (id(row cells), column) -> length of open chunk
    warnings: list[str] = field(default_factory=list)
    # Which side of a row's date line its wrapped narration lines sit on is a property of the
    # statement's layout, not of any one row: text-only statements print continuation lines *below*
    # the date line, while bordered tables with bottom-aligned cells (common in scans) print them
    # *above* it. Each row where the spacing makes it unambiguous casts a vote, and the tally
    # settles the rows where it doesn't (see ``_rows_from_columns``).
    prefix_votes: int = 0
    continuation_votes: int = 0
    # The last row built on the previous page, so a narration that spills onto the next page can be
    # rejoined to it instead of being mistaken for the start of the next page's first row.
    last_row: list[str] | None = None


_FOOTER = re.compile(
    r"page\s*\d+\s*(?:of|/)\s*\d+|^page\s*(?:no\.?)?\s*\d+\s*$|end of statement|computer[\s-]generated|"
    # "Registered & Corporate Office", "Corporate Office", "Regd. Office" etc - banks phrase this
    # differently enough (word order, "&"/"and", abbreviations) that a literal "registered office"
    # match misses most of them, so this matches "registered"/"regd"/"corporate" within a few
    # words of "office" rather than requiring them adjacent.
    r"(?:registered|regd\.?|corporate)\s*(?:[&,]|and)?\s*(?:regd\.?|corporate\s+)?\s*office|"
    r"statement (?:summary|of account)|generated (?:on|by)|legends?\b|"
    r"closing\s*balance\s*includes\s*funds|contents\s*of\s*this\s*statement|^hdfc\s*bank\s*limited\s*$|"
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
        for w in line.words:
            w.line_start = False
        line.words[0].line_start = True
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
        role = classify_header(text, fuzzy)
        x0, x1 = cell["x0"], cell["x1"]
        if role is None and fuzzy and len(ordered) > 1:
            # OCR often drops a junk fragment ("suai") right beside a real header word, which
            # spoils the whole cell; look for a header inside it, one or two words at a time.
            for size in (2, 1):
                found = next(
                    ((classify_header(" ".join(w.text for _, w in ordered[k:k + size]), True), ordered[k:k + size])
                     for k in range(len(ordered) - size + 1)
                     if classify_header(" ".join(w.text for _, w in ordered[k:k + size]), True)),
                    None,
                )
                if found:
                    role, sub = found
                    text = " ".join(w.text for _, w in sub)
                    x0, x1 = min(w.x0 for _, w in sub), max(w.x1 for _, w in sub)
                    break
        columns.append(Column(role, text, x0, x1))
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
_MIN_SAMPLES_OCR = 3  # a photographed page often carries only a handful of rows
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
    min_samples = _MIN_SAMPLES_OCR if ocr else _MIN_SAMPLES
    date_words = [w for w in all_words if parse_date(w.text, strict=True)]
    if len(date_words) < min_samples:
        return None
    date_gap = max(20.0, 4 * statistics.median(w.height for w in date_words))
    date_clusters = sorted(_cluster_by_anchor(date_words, lambda w: w.x0, date_gap), key=len, reverse=True)
    date_col_words = date_clusters[0]
    if len(date_col_words) < min_samples:
        return None
    date_x0 = min(w.x0 for w in date_col_words)
    date_x1 = max(w.x1 for w in date_col_words)

    # Amounts with paise (a decimal point + 1-2 digits) are real transaction values; a bare
    # integer at a similar position is usually a transaction-type code printed in its own trailing
    # column, not an amount - filtering to the decimal form keeps that code column from being
    # mistaken for a fourth money column.
    amount_words = [w for w in all_words if is_money_like(w.text) and _DECIMAL_AMOUNT.search(w.text)]
    if len(amount_words) < min_samples:
        return None
    amt_gap = max(20.0, 4 * statistics.median(w.height for w in amount_words))
    num_clusters = [c for c in _cluster_by_anchor(amount_words, lambda w: w.x1, amt_gap) if len(c) >= min_samples]
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
        rows = _rows_from_columns(lines, columns, page, ocr, start=0, state=LayoutState())
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
    # A column whose figures carry a "CR"/"DR" suffix ("7,641.45CR") is the running balance - no
    # transaction amount is written that way - so do not let a few noisy samples argue otherwise.
    suffixed = [
        i for i, c in enumerate(num_clusters)
        if sum(bool(re.search(r"(?:cr|dr)\.?\W*$", w.text, re.I)) for w in c) >= 0.6 * len(c)
    ]
    for bal_i in (suffixed[:1] if suffixed else range(n)):
        others = [i for i in range(n) if i != bal_i]
        combos = (
            [{bal_i: "balance", others[0]: "debit", others[1]: "credit"},
             {bal_i: "balance", others[1]: "debit", others[0]: "credit"}]
            if n == 3 else [{bal_i: "balance", others[0]: "amount"}]
        )
        for combo in combos:
            s, total = score(combo)
            if total >= min_samples and (best is None or s > best[0]):
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


_COMPLETE_PAISE = re.compile(r"[.,]\d{2}$")


def _fix_stray_decimal_separator(body: str) -> str:
    """OCR sometimes misreads the decimal point as a comma, or duplicates it so an actual
    thousands-grouping mark also comes out as one. Both are unambiguous to spot and fix without
    touching a normally-formatted number: a genuine Indian-formatted integer's right-most group is
    always three digits (e.g. "1,50,000"), so a comma followed by exactly two digits at the very
    end is never valid grouping, only ever a misread "." (e.g. "324928,39" -> "324928.39"); and a
    real amount never has more than one period, so a second one is always a misread "," (e.g.
    "1021.42.93" -> "102142.93", not two decimal points).
    """
    if body.count(".") >= 2:
        head, _, tail = body.rpartition(".")
        return head.replace(".", "").replace(",", "") + "." + tail
    if "." not in body:
        m = re.search(r",(\d{2})$", body)
        if m:
            return body[: m.start()].replace(",", "") + "." + m.group(1)
    return body


def _fix_ocr_number(text: str) -> str:
    m = re.match(r"^(.*?)(\s*(?:cr|dr)\.?)?$", text, re.I)
    body, suffix = m.group(1), m.group(2) or ""
    # A table's vertical ruling line next to the column is often misread as a stray "|"/"l"/"I"
    # glued onto the very end of the number - if the amount is already complete (ends in two paise
    # digits, with either a "." or a misread "," as the decimal marker - see below) without it,
    # it's noise to drop, not a digit: the confusable-translation below would otherwise turn it
    # into a spurious extra "1", e.g. "801601.67|" -> "801601.671".
    if body and body[-1] in "|lI" and _COMPLETE_PAISE.search(body[:-1]):
        body = body[:-1]
    body = _fix_stray_decimal_separator(body)
    fixed = body.translate(str.maketrans({"O": "0", "o": "0", "l": "1", "I": "1", "|": "1", "S": "5", "B": "8"}))
    return fixed + suffix if parse_money(fixed) is not None else text


_DATE_SHAPE = re.compile(r"^[|\[(]?(\d{2})[-/.]?(\d{2})[-/.](\d{4})[|\])]?$|^[|\[(]?(\d{2})(\d{2})[-/.](\d{4})[|\])]?$")
# Digits an OCR engine mistakes for one another in small print.
_DIGIT_CONFUSION = {"0": "986", "1": "47", "2": "7", "3": "8", "4": "1", "5": "6", "6": "50", "7": "12", "8": "03", "9": "0"}


def _repair_ocr_dates(words: list[Word], previous: date | None) -> tuple[list[Word], date | None]:
    """Fix date-shaped tokens the OCR misread ("46-11-2024" for 16-11-2024, "98-07-2024",
    "1206-2024", or a year read as 2024 for 2023), so their rows are not thrown away as undated or
    filed in the wrong month.

    A statement covers a short span, so a date far from its neighbours is a misreading. Of the
    readings reachable by swapping one or two commonly confused digits, the valid one closest to the
    median of the surrounding dates (within 45 days of it) is taken. Order-agnostic, so it works for
    ascending and descending statements alike.
    """
    import itertools
    import statistics as st

    ordered = sorted(range(len(words)), key=lambda k: (words[k].top + words[k].bottom) / 2)
    tokens: list[tuple[int, str, date | None]] = []
    for k in ordered:
        w = words[k]
        if w.x0 > 400 or not _DATE_SHAPE.match(w.text.strip()):
            continue
        core = re.sub(r"[|\[\]()]", "", w.text.strip())
        m = re.match(r"^(\d{2})(\d{2})[-/.](\d{4})$", core)
        if m:
            core = f"{m[1]}-{m[2]}-{m[3]}"
        tokens.append((k, core, parse_date(core, strict=True)))
    if not tokens:
        return words, previous
    valid = [t[2].toordinal() for t in tokens if t[2]]
    if previous is not None:
        valid.append(previous.toordinal())
    if not valid:
        return words, previous
    typical = st.median(valid)
    out = list(words)
    last = previous
    for idx, (k, core, parsed) in enumerate(tokens):
        w = words[k]
        near = [t[2].toordinal() for t in tokens[max(0, idx - 4): idx + 5] if t[2] and t[0] != k]
        ref = st.median(near) if near else typical
        if parsed and abs(parsed.toordinal() - ref) <= 45:
            last = parsed
            if core != w.text.strip():
                out[k] = Word(core, w.x0, w.x1, w.top, w.bottom, w.line_start)
            continue
        digits = [i for i, ch in enumerate(core) if ch.isdigit()]
        best = None
        for count in (1, 2):
            for spots in itertools.combinations(digits, count):
                for repl in itertools.product(*(_DIGIT_CONFUSION.get(core[i], "") for i in spots)):
                    chars = list(core)
                    for i, r in zip(spots, repl):
                        chars[i] = r
                    candidate = "".join(chars)
                    cand = parse_date(candidate, strict=True)
                    if not cand or abs(cand.toordinal() - ref) > 45:
                        continue
                    if best is None or abs(cand.toordinal() - ref) < abs(best[0].toordinal() - ref):
                        best = (cand, candidate)
            if best:
                break
        if best:
            out[k] = Word(best[1], w.x0, w.x1, w.top, w.bottom, w.line_start)
            last = best[0]
        elif parsed:
            last = parsed
    return out, last


def _is_ocr_junk(token: str) -> bool:
    """Scraps OCR invents out of shadows and paper grain: a short run of letters that is neither
    upper-case (bank codes: WDL, TFR, AT) nor mixed with digits/punctuation (references, names of
    payees): "ei", "rn.", "vik", "La"."""
    core = token.strip(".,;:'\"`|()[]{}_-~=<>")
    if not core:
        return True
    if any(ch.isdigit() or ch in "/@&#%" for ch in core):
        return False
    return len(core) <= 4 and not core.isupper()


def _merge_split_money(words: list[Word]) -> list[Word]:
    """Rejoin a figure whose decimal point the OCR lost: "5000 00" -> "5000.00", "803 27Cr" ->
    "803.27Cr". Only in the money columns (right of the page's midline), where a whole number
    directly followed by a two-digit tail on the same line can only be rupees and paise."""
    if not words:
        return words
    width = max(w.x1 for w in words)
    ordered = sorted(words, key=lambda w: ((w.top + w.bottom) / 2, w.x0))
    out: list[Word] = []
    skip: set[int] = set()
    for k, w in enumerate(ordered):
        if id(w) in skip:
            continue
        if w.x0 > 0.5 * width and re.fullmatch(r"\d{1,9}", w.text.replace(",", "")):
            for nxt in ordered[k + 1:k + 4]:
                if id(nxt) in skip or abs((nxt.top + nxt.bottom) / 2 - (w.top + w.bottom) / 2) > 3:
                    continue
                if 0 <= nxt.x0 - w.x1 <= 14 and re.fullmatch(r"\d{2}(?:cr|dr)?\.?", nxt.text, re.I):
                    skip.add(id(nxt))
                    w = Word(f"{w.text}.{nxt.text.rstrip('.')}", w.x0, nxt.x1, min(w.top, nxt.top), max(w.bottom, nxt.bottom), w.line_start)
                    break
        out.append(w)
    return out


def _header_block_end(lines: list[Line]) -> int:
    """Index just past a run of header-like lines (OCR often reads a column header only partly, so
    no full header is found, but its words - Debit, Credit, Balance, Description... - still show)."""
    last = -1
    for k, line in enumerate(lines[:40]):
        roles = {classify_header(w.text, True) for w in line.words} - {None}
        if len(roles) >= 2 and not _date_spans(line.words):
            last = k
    return last + 1


def page_anchors(words: list[Word]) -> tuple[float, float] | None:
    """(left, right) x positions that locate a page's table: where the Date column's text starts and
    where the Balance figures end. Comparing them between pages measures how far a photo's
    perspective moved the table."""
    import numpy as np

    if len(words) < 30:
        return None
    width = max(w.x1 for w in words)
    dates = [w for w in words if w.x0 < 0.35 * width and parse_date(w.text, strict=True)]
    if len(dates) < 3:
        return None
    lefts = sorted(w.x0 for w in dates)
    cluster = [x for x in lefts if x <= lefts[0] + 25]
    balances = [w.x1 for w in words if w.x0 > 0.45 * width and re.search(r"\d\.\d{2}\s*(?:cr|dr)\.?$", w.text, re.I)]
    if len(balances) < 3:
        return None
    return float(np.median(cluster)), float(np.median(balances))


def plan_ocr_columns(pages: list[list[Word]]) -> list[list[Column] | None]:
    """One column layout for a whole scanned statement, fitted to each page.

    Per-page header reading and per-page inference are both unreliable on photographed pages (a
    header printed on a grey band comes back as fragments; a page with three rows cannot tell a
    Debit column from a Balance column). The columns are the same on every page, though, so take
    the best evidence anywhere - a header read in full, else the most convincing inference - and
    move it onto each page by the left/right anchors, which follow the photo's perspective.
    """
    prepared: list[list[Word]] = []
    state_date: date | None = None
    for words in pages:
        fixed, state_date = _repair_ocr_dates(_merge_split_money(words), state_date)
        prepared.append(_dewarp(fixed))
    best: tuple[int, list[Column], tuple[float, float]] | None = None
    for words in prepared:
        anchors = page_anchors(words)
        if anchors is None:
            continue
        lines = group_lines(words)
        found = find_header(lines, fuzzy=True)
        cols = found[2] if found else None
        score = len({c.role for c in cols if c.role}) if cols else 0
        if not cols or not {"date", "narration", "balance"} <= {c.role for c in cols} or not {"debit", "credit"} & {c.role for c in cols}:
            inferred = infer_columns_from_data(lines, 0, True)
            if inferred and {"debit", "credit", "balance"} <= {c.role for c in inferred}:
                cols, score = inferred, max(score, 5)
            elif not cols:
                continue
        if best is None or score > best[0]:
            best = (score, cols, anchors)
    if best is None:
        return [None] * len(pages)
    _, template, (tl, tr) = best
    out: list[list[Column] | None] = []
    for words in prepared:
        anchors = page_anchors(words)
        if anchors is None or tr - tl < 100:
            out.append(None)
            continue
        pl, pr = anchors
        scale = (pr - pl) / (tr - tl)
        out.append([Column(c.role, c.label, pl + (c.x0 - tl) * scale, pl + (c.x1 - tl) * scale) for c in template])
    return out


def _dewarp(words: list[Word]) -> list[Word]:
    """Straighten the vertical drift of a photographed table.

    A phone photo of a page is rarely flat: the right-hand columns sit higher or lower than the
    Date column beside them, and by an amount that itself changes down the page (+5pt near the top,
    -8pt near the bottom on one real scan) - more than the tolerance for calling two words the same
    line, so amounts land between rows and get dropped. The dates and the money figures of a row
    are the same row, so their measured vertical offsets say how far each column has drifted; fit
    that as ``offset = fraction_of_the_way_across * (c0 + c1 * y)`` and take it back out.
    Returns the words unchanged unless there is a clear, consistent drift to correct.
    """
    import numpy as np

    if len(words) < 30:
        return words
    width = max(w.x1 for w in words)
    date_words = [w for w in words if w.x0 < 0.35 * width and parse_date(w.text, strict=True)]
    if len(date_words) < 4:
        return words
    # One anchor per row: the Post Date and the Value Date of a row share a line, and either may be
    # the only one the OCR managed to read.
    raw_ys = sorted((w.top + w.bottom) / 2 for w in date_words)
    date_ys = []
    group = [raw_ys[0]]
    for y in raw_ys[1:]:
        if y - group[-1] <= 6:
            group.append(y)
        else:
            date_ys.append(sum(group) / len(group))
            group = [y]
    date_ys.append(sum(group) / len(group))
    if len(date_ys) < 4:
        return words
    spacing = float(np.median(np.diff(date_ys)))
    if spacing < 14:
        return words  # rows too tight to tell drift from a neighbouring row
    date_x = float(np.mean([(w.x0 + w.x1) / 2 for w in date_words]))
    amounts = [w for w in words if w.x0 > 0.45 * width and is_money_like(w.text) and "." in w.text]
    if len(amounts) < 4:
        return words
    ref_x = float(np.median([(w.x0 + w.x1) / 2 for w in amounts if re.search(r"(?:cr|dr)$", w.text, re.I)] or [max((w.x0 + w.x1) / 2 for w in amounts)]))
    if ref_x <= date_x + 50:
        return words

    def fraction(w: Word) -> float:
        return min(1.0, max(0.0, ((w.x0 + w.x1) / 2 - date_x) / (ref_x - date_x)))

    samples: list[tuple[float, float, float]] = []  # (y_date, dy / fraction, weight)
    for w in amounts:
        y = (w.top + w.bottom) / 2
        nearest = min(date_ys, key=lambda dy: abs(dy - y))
        if abs(nearest - y) <= 0.45 * spacing and fraction(w) > 0.5:
            samples.append((nearest, (y - nearest) / fraction(w), 1.0))
    if len(samples) < 4:
        return words
    ys = np.array([s[0] for s in samples])
    off = np.array([s[1] for s in samples])
    keep = np.ones(len(ys), bool)
    coeffs = np.array([float(np.median(off)), 0.0])
    for _ in range(4):
        if keep.sum() < 4:
            return words
        coeffs = np.polyfit(ys[keep], off[keep], 1)[::-1]  # c0, c1
        residual = np.abs(off - (coeffs[0] + coeffs[1] * ys))
        keep = residual <= max(2.0, 2.5 * float(np.median(residual[keep])))
    fitted = coeffs[0] + coeffs[1] * ys
    if float(np.max(np.abs(fitted))) < 2.5:
        return words  # no meaningful drift
    if float(np.median(np.abs(off - fitted))) > 0.25 * spacing:
        return words  # the model does not describe this page well; leave it alone
    # The straight-line fit gets close, but a curled page drifts a little differently from row to
    # row; refine it with each row's own measured offset (the inliers), interpolated in between.
    inlier_ys, inlier_off = ys[keep], off[keep]
    anchors: dict[float, list[float]] = {}
    for y, o in zip(inlier_ys, inlier_off):
        anchors.setdefault(float(y), []).append(float(o))
    anchor_y = np.array(sorted(anchors))
    anchor_off = np.array([float(np.median(anchors[y])) for y in anchor_y])

    def drift(y: float) -> float:
        if len(anchor_y) >= 5:
            return float(np.interp(y, anchor_y, anchor_off))
        return float(coeffs[0] + coeffs[1] * y)

    out = []
    for w in words:
        y = (w.top + w.bottom) / 2
        shift = fraction(w) * drift(y)
        if is_money_like(w.text) and "." in w.text and w.x0 > 0.45 * width:
            # A money figure is on its row's line by definition: snap what is left of the drift.
            nearest = min(date_ys, key=lambda dy: abs(dy - (y - shift)))
            if abs(nearest - (y - shift)) <= 0.4 * spacing:
                shift = y - nearest
        out.append(Word(w.text, w.x0, w.x1, w.top - shift, w.bottom - shift))
    return out


def layout_page(
    words: list[Word], page: int, state: LayoutState, ocr: bool = False,
    columns_override: list[Column] | None = None,
) -> list[RawRow]:
    if ocr:
        words = _merge_split_money(words)
        words, state.last_date = _repair_ocr_dates(words, state.last_date)
        words = _dewarp(words)
    lines = group_lines(words)
    if not state.hard_wrap and not ocr:
        state.hard_wrap = _detect_hard_wrap(lines)
    found = find_header(lines, fuzzy=ocr)
    start = 0
    if columns_override:
        columns = state.columns = columns_override
        if found:
            start = found[1] + 1
        else:
            start = _header_block_end(lines)
    elif found:
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
    return _rows_from_columns(lines, columns, page, ocr, start, state)


_HARD_WRAP_WIDTH = 40


def _detect_hard_wrap(lines: list[Line]) -> int:
    """Recognise statements whose narration is chopped into exactly-40-character chunks: a text-only
    line (no date, no amount) is very often exactly that long, which ordinary wrapping never does."""
    text_only = [
        l for l in lines
        if find_date_free(l) and len(l.text) > 20
    ]
    full = sum(1 for l in text_only if len(l.text) == _HARD_WRAP_WIDTH)
    return _HARD_WRAP_WIDTH if full >= 5 and full >= 0.25 * len(text_only) else 0


def find_date_free(line: Line) -> bool:
    return not _date_spans(line.words) and not any(is_money_like(w.text) for w in line.words)


def _rows_from_columns(
    lines: list[Line], columns: list[Column], page: int, ocr: bool, start: int, state: LayoutState,
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
    # Whether the row's own date line already carried a Debit/Credit/Balance value of its own -
    # see the note on ``extra_numeric`` below.
    current_has_numbers = False
    # Vertical position of the last line attributed to the open row, for the spacing test in
    # ``run_belongs_to_next``.
    current_mid = 0.0
    pending_prefix: list[Word] = []

    def is_amount_word(w: Word) -> bool:
        """A money-shaped word in the numeric area. A bare integer that only *reaches* into it (a
        right-aligned reference number wider than its header, e.g. "308067" under "Chq./Ref.No.")
        is not an amount: real amounts carry decimals/separators or start inside the numeric area."""
        if not is_money_like(w.text) or w.x1 < first_num_x0 - 3 or re.fullmatch(r"\d{10,}", w.text):
            return False  # (a bare 10+ digit run is a reference/account number, never an amount)
        return "." in w.text or "," in w.text or w.x0 >= first_num_x0 - 3

    def blank() -> list[str]:
        return [""] * len(out_cols)

    def put(cells: list[str], col: Column | None, text: str, brk: bool = False) -> None:
        if col is None or id(col) not in index_of:
            return
        k = index_of[id(col)]
        if ocr and col is narr_col and _is_ocr_junk(text):
            return
        if state.hard_wrap and col is narr_col and cells[k]:
            key = (id(cells), k)
            used_len = state.chunk_len.get(key, len(cells[k]))
            if brk and used_len >= state.hard_wrap - 3:
                # the previous printed line ended a full chunk: the break may be mid-word
                cells[k] += text
                state.chunk_len[key] = len(text)
            else:
                cells[k] += " " + text
                state.chunk_len[key] = used_len + 1 + len(text)
            return
        cells[k] = f"{cells[k]} {text}".strip()
        if state.hard_wrap and col is narr_col:
            state.chunk_len[(id(cells), k)] = len(text)

    def find_head_date(ws: list[Word]) -> tuple[int, int] | None:
        spans = _date_spans(ws)
        head = next_head(ws, spans)
        if head is None and ocr and value_date_col is not None and date_col is not None:
            # The Post Date was unreadable but the Value Date beside it was not: still a row.
            head = next(
                (
                    s for s in spans
                    if s[0] <= 1 and ws[s[0]].x0 < first_num_x0
                    and min(_left_cols, key=lambda c: abs(ws[s[0]].x0 - c.x0)) is value_date_col
                ),
                None,
            )
        return head

    def next_head(ws: list[Word], spans: list[tuple[int, int]]) -> tuple[int, int] | None:
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

    def prefix_mode() -> bool:
        return state.prefix_votes > state.continuation_votes

    def run_belongs_to_next(run: list[Line], nxt: Line) -> bool:
        """Do these narration-only lines, sitting between the open row and the next dated line,
        belong to the row that follows them rather than the one above?

        Statements differ in which side of the date line a wrapped narration sits on - continuation
        lines *below* it (text statements) or lines *above* it (bottom-aligned cells, common in
        scans) - and a wrong guess silently attaches text to the wrong transaction. Cheapest tell:
        spacing. Lines belonging together are set closer than lines belonging to different rows, so
        whichever dated line the run is clearly nearer to is its owner. When the spacing is uniform
        there is no such tell (a plain text statement), and the run follows the document's own
        convention as voted so far - defaulting to the common one, continuation below.
        """
        if not has_own_narration(nxt.words):
            return True  # its own line carries none, so what it has must come from the run
        if current is None:
            return prefix_mode()
        if not current_self_narrated:
            return False  # the open row is still short of narration; the run is its continuation
        gap_before = run[0].mid - current_mid
        gap_after = nxt.mid - run[-1].mid
        if gap_after < 0.75 * gap_before:
            state.prefix_votes += 1
            return True
        if gap_before < 0.75 * gap_after:
            state.continuation_votes += 1
            return False
        return prefix_mode()

    def split_point(run: list[Line], nxt: Line) -> int | None:
        """A run between two dated lines can hold the tail of the open row *and* the head of the
        next one (a cell whose text is centred on the date line: a label above it, the rest below).
        The row boundary then shows as one clearly larger vertical gap strictly inside the run, while
        the lines of one row are set at an even pitch. Returns how many leading lines belong to the
        open row, or None when there is no such clear boundary."""
        if current is None or len(run) < 2 or not has_own_narration(nxt.words):
            return None
        gaps = [run[0].mid - current_mid] + [b.mid - a.mid for a, b in zip(run, run[1:])] + [nxt.mid - run[-1].mid]
        widest = max(range(len(gaps)), key=lambda g: gaps[g])
        if widest in (0, len(gaps) - 1):
            return None
        rest = sorted(g for k, g in enumerate(gaps) if k != widest)
        typical = rest[len(rest) // 2]
        return widest if typical > 0 and gaps[widest] >= 1.4 * typical else None

    seg = lines[start:]
    i = 0
    while i < len(seg):
        line = seg[i]
        ws = line.words
        if _FOOTER.search(line.text):
            current = None
            current_self_narrated = False
            current_has_numbers = False
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
            extra_numeric = [w for w in ws if is_amount_word(w) and text_cols]
            if extra_numeric:
                if current is not None and not current_has_numbers:
                    # The open row's own date line had no Debit/Credit/Balance of its own - the
                    # same OCR line-split as the narration case above, just for the amount instead
                    # of the narration: this line supplies the value that row was missing, not a
                    # genuinely stray one to discard.
                    for w in extra_numeric:
                        col = min(right_cols, key=lambda c: abs(w.x1 - c.x1))
                        if col.role in NUMERIC_ROLES:
                            put(current, col, _fix_ocr_number(w.text) if ocr else w.text)
                    current_has_numbers = True
                    i += 1
                    continue
                if current is not None:
                    state.warnings.append(f"page {page}: a line with amounts but no date was skipped: {line.text[:60]!r}")
                    current = None
                    current_self_narrated = False
                    current_has_numbers = False
                i += 1
                continue
            run_end = i
            while run_end < len(seg):
                rline = seg[run_end]
                if _FOOTER.search(rline.text) or find_head_date(rline.words) is not None or _BALANCE_MARKER.match(rline.text):
                    break
                if [w for w in rline.words if is_amount_word(w) and text_cols]:
                    break
                run_end += 1
            nxt = seg[run_end] if run_end < len(seg) else None
            run = seg[i:run_end]
            if nxt is not None and not _FOOTER.search(nxt.text) and find_head_date(nxt.words) is not None:
                cut = split_point(run, nxt)
                if cut:
                    for rline in run[:cut]:
                        for w in rline.words:
                            put(current, _text_column(w, text_cols, narr_col, has_serial), w.text, w.line_start)
                        current_mid = rline.mid
                    for rline in run[cut:]:
                        pending_prefix.extend(rline.words)
                    i = run_end
                    continue
                if run_belongs_to_next(run, nxt):
                    for rline in run:
                        pending_prefix.extend(rline.words)
                    i = run_end
                    continue
                # Otherwise it continues the row above it - or, at the very top of a page with no
                # row open yet, the last row of the previous page whose narration spilled over.
                target = current
                spill = run
                if target is None and state.last_row is not None and len(state.last_row) == len(out_cols):
                    # Only the trailing lines that sit inside the narration column count: a page
                    # without its own header row also has page-header text before the first row.
                    spill = []
                    for rline in reversed(run):
                        # (narration text usually starts left of its header, so measure from the
                        # date column instead)
                        left_edge = (date_col.x0 + 25) if date_col is not None else (narr_col.x0 - 8 if narr_col else 0)
                        inside = narr_col is not None and all(
                            w.x0 >= left_edge and w.x1 < first_num_x0 for w in rline.words
                        )
                        if not inside or len(spill) >= 4:
                            break
                        spill.insert(0, rline)
                    if spill:
                        target = state.last_row
                if target is not None:
                    for rline in spill:
                        for w in rline.words:
                            put(target, _text_column(w, text_cols, narr_col, has_serial), w.text, w.line_start)
                        if target is current:
                            current_mid = rline.mid
                i = run_end
                continue
            if current is None:
                i += 1
                continue
            for w in ws:
                col = _text_column(w, text_cols, narr_col, has_serial)
                put(current, col, w.text, w.line_start)
            current_mid = line.mid
            i += 1
            continue

        cells = blank()
        if pending_prefix:
            for w in pending_prefix:
                col = _text_column(w, text_cols, narr_col, has_serial)
                put(cells, col, w.text, w.line_start)
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
        first_narration_word = True
        for wi, w in enumerate(ws):
            if wi in used:
                continue
            text = w.text
            if is_amount_word(w) and right_cols:
                col = min(right_cols, key=lambda c: abs(w.x1 - c.x1))
                if col.role in NUMERIC_ROLES:
                    put(cells, col, _fix_ocr_number(text) if ocr else text)
                continue  # role-less trailing columns (branch code etc.) are dropped
            tcol = _text_column(w, text_cols, narr_col, has_serial)
            # on a date line the first narration word starts a new printed line for the wrap logic
            put(cells, tcol, text, w.line_start or (tcol is narr_col and first_narration_word))
            if tcol is narr_col:
                first_narration_word = False
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
            current = state.last_row = None
            current_self_narrated = False
            current_has_numbers = False
            i += 1
            continue
        rows.append(RawRow(cells, page))
        current = state.last_row = cells
        current_mid = line.mid
        current_self_narrated = head_date is not None and has_own_narration(ws)
        current_has_numbers = any(cells[index_of[id(c)]] for c in numeric_cols if id(c) in index_of)
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
