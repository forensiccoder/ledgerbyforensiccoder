"""Clean-up of narration text that came from OCR, applied before narrations are classified.

OCR leaves a few recognisable kinds of damage in payee names - a stray quote in front of a word
("MUMTAZ 'ANSARI"), a symbol where a letter belongs ("VISHAL CHANDRA PAL $"), a letter read as
a look-alike pair ("VUAY" for "VIJAY") - and lets a row's leading punctuation ride along (".UPI-
RAKESH CHAUHAN"). The same payee almost always appears elsewhere in the same statement, read
correctly, so the document itself is the best dictionary.
"""
from __future__ import annotations

import re
from collections import Counter

from .models import Transaction

# Common given names, so a once-only misreading can be corrected even when the clean spelling is not
# elsewhere in the statement. Deliberately short: this is a fallback, not a name database.
_COMMON_NAMES = frozenset("""
AMIT AJAY AKASH ANIL ANKIT ANUJ ARUN ASHISH ASHOK DEEPAK DINESH GAURAV HARISH HARSH MAHESH MANISH
MANOJ MOHIT MUKESH NARESH NEERAJ NITIN PANKAJ PRAVEEN RAHUL RAJESH RAKESH RAMESH ROHIT SANDEEP SANJAY
SANJEEV SATISH SUNIL SURESH SUMIT VIJAY VIKAS VIKRAM VINOD VIPIN VISHAL VIVEK YOGESH PRIYA POOJA
KAVITA SUNITA ANITA SEEMA REKHA MEENA SAVITA RENU NEHA KIRAN SHYAM SHAHID IMRAN IRFAN SALIM SAEED
JITENDER JITENDRA HARISH RAVI ROHAN SACHIN KULDEEP LALIT MOHAN
""".split())

# What an OCR engine tends to turn a letter (or a pair of letters) into.
_CONFUSIONS = (("U", "IJ"), ("IJ", "U"), ("RN", "M"), ("M", "RN"), ("VV", "W"), ("W", "VV"),
               ("CL", "D"), ("0", "O"), ("1", "I"))
# A symbol standing alone at the end of a name is a letter the OCR could not read.
_SYMBOL_LETTERS = {"$": "S", "§": "S", "&": "S", "8": "SB", "5": "S", "|": "I", "!": "I"}

_STRAY_QUOTE = re.compile(r"(?<![A-Za-z0-9])[’‘'`\"]+(?=[A-Za-z])")
_LEADING_JUNK = re.compile(r"^[\s.\-_|'`:;,]+")
_NAME_FIELD = re.compile(r"^(UPI-)([^-]+?)(-)", re.I)
_TRAILING_SYMBOL = re.compile(r"^(?P<base>.*?[A-Za-z]) (?P<sym>[$§&85|!])$")


def _name_field(narration: str) -> re.Match[str] | None:
    return _NAME_FIELD.match(narration)


_TRANSFER_LEAD = re.compile(r"^[\W\d]{0,20}?\s*((?:TO|BY)\s*TRANSFER)\s*[-:.]*\s*(.*)$", re.I | re.S)
_UPI_BODY = re.compile(r"[^\w/]*\bUPI/(?P<kind>[A-Za-z0-9]{1,3})/?\s*(?P<rrn>[0-9A-Za-z][0-9A-Za-z ]{9,15}?)(?=/|[A-Za-z]{3,}|-|$)")
_RRN_LOOKALIKES = str.maketrans({"S": "5", "s": "5", "O": "0", "o": "0", "I": "1", "l": "1", "B": "8", "Z": "2"})


def _tidy_upi_body(text: str) -> str:
    """"UPI/DR/<12-digit RRN>/<name>/<bank>/<handle>/<remark>" read through OCR: a ruling-line mark
    or quote in front of "UPI", a mangled DR/CR tag, a space or look-alike letter inside the
    12-digit reference ("04802551707 7", "4241989990S7"), the reference glued to the name
    ("...747VIJAYP AL"), and stray underscores inside a name."""
    m = _UPI_BODY.search(text)
    if not m:
        return text
    kind = m.group("kind").upper()
    kind = {"IDR": "DR", "OR": "DR", "0R": "DR", "D": "DR"}.get(kind, kind)
    rrn = m.group("rrn").replace(" ", "")
    fixed = rrn.translate(_RRN_LOOKALIKES) if len(rrn) == 12 else rrn
    if len(fixed) == 12 and fixed.isdigit():
        rrn = fixed
    elif len(rrn) > 12 and rrn[:12].translate(_RRN_LOOKALIKES).isdigit():
        rrn = rrn[:12].translate(_RRN_LOOKALIKES) + "/" + rrn[12:]
    head = text[:m.start()]
    tail = re.sub(r"\s?_(?=[A-Za-z])", " ", text[m.end():])
    if tail[:1].isalpha():  # the reference ran straight into the name
        tail = "/" + tail
    return f"{head}UPI/{kind}/{rrn}{tail}".strip()


def _tidy_transfer_narration(text: str) -> str:
    """"TO TRANSFER-" / "BY TRANSFER-" narrations (SBI): fragments of the date columns and of the
    reference column ("27", "2024)", "TRANSFER") that rode along on the same lines are not part of
    the narration."""
    m = _TRANSFER_LEAD.match(text)
    if not m:
        return text
    rest = m.group(2)
    rest = re.sub(r"^(?:(?:19|20)\d{2}\W*\s*)+", "", rest)
    rest = re.sub(r"^(?:TRANSFER\W*\s*)+(?=\W*UPI|INB|NEFT|IMPS)", "", rest, flags=re.I)
    rest = re.sub(r"^\W*\d{1,4}\s+(?=\W*UPI)", "", rest)  # a date fragment left in front of UPI/...
    return f"{m.group(1).upper()}- {_tidy_upi_body(rest)}".strip()


def clean_ocr_narrations(txns: list[Transaction]) -> None:
    """Tidy ``narration`` in place for every transaction (call only for OCR-read statements)."""
    for t in txns:
        text = _STRAY_QUOTE.sub("", t.narration)
        t.narration = _tidy_transfer_narration(_LEADING_JUNK.sub("", text))

    vocabulary: Counter[str] = Counter()
    clean_names: set[str] = set()
    for t in txns:
        vocabulary.update(re.findall(r"[A-Z]{4,}", t.narration.upper()))
        m = _name_field(t.narration)
        if m and not _TRAILING_SYMBOL.match(m.group(2)):
            clean_names.add(m.group(2).strip().upper())

    for t in txns:
        m = _name_field(t.narration)
        if not m:
            continue
        name = m.group(2).strip()
        name = _fix_trailing_symbol(name, clean_names)
        name = _fix_lookalike_words(name, vocabulary)
        t.narration = f"{m.group(1)}{name}{m.group(3)}{t.narration[m.end():]}"


def _fix_trailing_symbol(name: str, clean_names: set[str]) -> str:
    m = _TRAILING_SYMBOL.match(name)
    if not m:
        return name
    base = m.group("base")
    for letter in _SYMBOL_LETTERS.get(m.group("sym"), ""):
        if f"{base} {letter}".upper() in clean_names:
            return f"{base} {letter}"
    return base  # no clean spelling elsewhere: a stray symbol is better dropped than shown


def _fix_lookalike_words(name: str, vocabulary: Counter[str]) -> str:
    words = name.split(" ")
    for i, word in enumerate(words):
        token = word.upper()
        if len(token) < 4 or not token.isalpha() or vocabulary[token] != 1 or token in _COMMON_NAMES:
            continue
        for wrong, right in _CONFUSIONS:
            pos = token.find(wrong)
            while pos != -1:
                candidate = token[:pos] + right + token[pos + len(wrong):]
                if candidate in _COMMON_NAMES or (candidate != token and vocabulary[candidate] >= 1):
                    words[i] = candidate if word.isupper() else candidate.capitalize()
                    break
                pos = token.find(wrong, pos + 1)
            else:
                continue
            break
    return " ".join(words)
