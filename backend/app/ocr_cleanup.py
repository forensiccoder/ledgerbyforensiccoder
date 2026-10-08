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


def clean_ocr_narrations(txns: list[Transaction]) -> None:
    """Tidy ``narration`` in place for every transaction (call only for OCR-read statements)."""
    for t in txns:
        text = _STRAY_QUOTE.sub("", t.narration)
        t.narration = _LEADING_JUNK.sub("", text)

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
