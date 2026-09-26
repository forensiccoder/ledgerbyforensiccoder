"""Synthetic statement generators (reportlab) used by the tests.

No real customer data: the layouts imitate how common Indian banks print statements
(bordered / borderless tables, zero-filled or blank debit-credit cells, single Dr/Cr amount
column, newest-first ordering, wrapped narrations, repeated headers, page footers).
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

OPENING = Decimal("50000.00")

# (narration template, direction, amount, expected category, expected counterparty, expected ref-or-empty)
TEMPLATES = [
    ("UPI-RAMESH KUMAR-RAMESH@OKSBI-SBIN0001234-{rrn}-UPI", "Debit", "1500.00", "UPI", "RAMESH KUMAR", "{rrn}"),
    ("NEFT CR-SBIN0001234-ACME TRADERS PVT LTD-SHARMA AND SONS-SBINN52024040212345678", "Credit", "250000.00", "NEFT", "ACME TRADERS PVT LTD", "SBINN52024040212345678"),
    ("CASH DEPOSIT BY SELF", "Credit", "90000.00", "Cash deposit", "SELF", ""),
    ("ATM WDL/512345XXXXXX1234/S1ANDL01/ANDHERI MUMBAI", "Debit", "20000.00", "Cash withdrawal", "", ""),
    ("UPI-SWIGGY-SWIGGY.PAYU@ICICI-ICIC0DC0099-{rrn}-PAYMENT FOR ORDER 998877 THROUGH APP", "Debit", "549.50", "UPI", "SWIGGY", "{rrn}"),
    ("NEFT DR-UTIB0000001-JOHN DOE-NETBANK, MUM-UTIBN52024040612345678-RENT APRIL", "Debit", "30000.00", "NEFT", "JOHN DOE", "UTIBN52024040612345678"),
    ("NEFT CHARGES INCL GST", "Debit", "5.90", "Other", "", ""),
    ("IMPS-{rrn}-PRIYA-HDFC-XXXX", "Debit", "1000.00", "IMPS", "PRIYA", "{rrn}"),
    ("CDM DEP-ANDHERI EAST", "Credit", "49000.00", "Cash deposit", "", ""),
    ("UPI/CR/{rrn}/ANITA SHARMA/HDFC/anita@ybl/UPI", "Credit", "2500.00", "UPI", "ANITA SHARMA", "{rrn}"),
    ("ATM ANNUAL FEE", "Debit", "295.00", "Other", "", ""),
    ("NEFT*HDFC0001234*HDFCN52024041012345678*GUPTA ENTERPRISES*", "Credit", "125000.50", "NEFT", "GUPTA ENTERPRISES", "HDFCN52024041012345678"),
    ("NFS/CASH WDL/{rrn}/DELHI", "Debit", "10000.00", "Cash withdrawal", "", "{rrn}"),
]


@dataclass
class Row:
    date: date
    narration: str
    direction: str
    amount: Decimal
    balance: Decimal
    category: str
    counterparty: str
    reference: str
    ref_col: str


def make_rows(n: int = 44, opening: Decimal = OPENING) -> list[Row]:
    rows: list[Row] = []
    bal = opening
    d = date(2024, 4, 1)
    for i in range(n):
        narr_t, direction, amt, cat, cp, ref = TEMPLATES[i % len(TEMPLATES)]
        rrn = f"41{(23456789012 + i * 7919) % 10**10:010d}"
        amount = Decimal(amt) + (Decimal(i) if cat == "UPI" else Decimal(0))
        if direction == "Debit" and bal - amount < 1000:
            direction, amount = "Credit", Decimal("75000.00")  # keep balances positive
            cat, cp, ref = "Other", "", ""
            narr = f"BY CLG CHQ DEP-{rrn}"
        else:
            narr = narr_t.format(rrn=rrn)
        bal = bal + amount if direction == "Credit" else bal - amount
        rows.append(Row(d, narr, direction, amount, bal, cat, cp, ref.format(rrn=rrn), f"000{rrn}" if cat == "UPI" else ""))
        if i % 3 == 2:
            d += timedelta(days=1)
    return rows


def money(v: Decimal) -> str:
    """Indian digit grouping, 2 decimals."""
    s = f"{v:.2f}"
    whole, frac = s.split(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join(parts + [tail])
    return f"{whole}.{frac}"


def _doc(buf, title: str, encrypt: str | None = None):
    kwargs = {}
    if encrypt:
        from reportlab.lib.pdfencrypt import StandardEncryption

        kwargs["encrypt"] = StandardEncryption(encrypt, canPrint=1)
    return SimpleDocTemplate(buf, pagesize=A4, leftMargin=24, rightMargin=24, topMargin=30, bottomMargin=36, title=title, **kwargs)


def _footer(canvas, doc):
    canvas.setFont("Helvetica", 7)
    canvas.drawString(24, 18, f"Page {doc.page} of 3   This is a computer generated statement and does not require signature.")


def _build(buf, style_rows, col_widths, header, body_rows, align_right_cols, grid, encrypt=None, intro=True, opening_line=None):
    cell = ParagraphStyle("c", fontName="Helvetica", fontSize=7, leading=8.5)
    data = [header]
    if opening_line:
        data.append(opening_line)
    for r in body_rows:
        data.append([Paragraph(c, cell) if i == style_rows else c for i, c in enumerate(r)])
    table = Table(data, colWidths=col_widths, repeatRows=1)
    st = [("FONT", (0, 0), (-1, -1), "Helvetica", 7), ("FONT", (0, 0), (-1, 0), "Helvetica-Bold", 7),
          ("VALIGN", (0, 0), (-1, -1), "TOP"), ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2)]
    for c in align_right_cols:
        st.append(("ALIGN", (c, 0), (c, -1), "RIGHT"))
    if grid:
        st.append(("GRID", (0, 0), (-1, -1), 0.5, colors.black))
    else:
        st.append(("LINEBELOW", (0, 0), (-1, 0), 0.5, colors.grey))
    table.setStyle(TableStyle(st))
    story = []
    if intro:
        story += [Paragraph("<b>EXAMPLE BANK LTD</b> - Statement of account", ParagraphStyle("h", fontName="Helvetica", fontSize=10)),
                  Paragraph("Account Name: TEST CUSTOMER   Account No: XXXXXXXX1234   From: 01/04/2024 To: 30/04/2024", ParagraphStyle("s", fontName="Helvetica", fontSize=8)),
                  Spacer(1, 12)]
    story.append(table)
    _doc(buf, "statement", encrypt).build(story, onFirstPage=_footer, onLaterPages=_footer)


def hdfc_bordered(rows: list[Row], zero_fill: bool = False, encrypt: str | None = None) -> bytes:
    """Date | Narration | Chq./Ref.No. | Value Dt | Withdrawal Amt. | Deposit Amt. | Closing Balance (with grid)."""
    z = "0.00" if zero_fill else ""
    body = [[r.date.strftime("%d/%m/%y"), r.narration, r.ref_col or f"00000{i:05d}", r.date.strftime("%d/%m/%y"),
             money(r.amount) if r.direction == "Debit" else z, money(r.amount) if r.direction == "Credit" else z,
             money(r.balance)] for i, r in enumerate(rows)]
    buf = io.BytesIO()
    _build(buf, 1, [42, 175, 70, 42, 62, 62, 68], ["Date", "Narration", "Chq./Ref.No.", "Value Dt", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"],
           body, [4, 5, 6], grid=True, encrypt=encrypt)
    return buf.getvalue()


def sbi_borderless(rows: list[Row], zero_fill: bool = False, descending: bool = False) -> bytes:
    """Txn Date | Value Date | Description | Ref No./Cheque No. | Debit | Credit | Balance (no grid)."""
    z = "0.00" if zero_fill else ""
    ordered = rows[::-1] if descending else rows
    body = [[r.date.strftime("%d %b %Y"), r.date.strftime("%d %b %Y"), r.narration, r.ref_col or "-",
             money(r.amount) if r.direction == "Debit" else z, money(r.amount) if r.direction == "Credit" else z,
             money(r.balance)] for r in ordered]
    buf = io.BytesIO()
    _build(buf, 2, [52, 52, 170, 62, 58, 58, 66], ["Txn Date", "Value Date", "Description", "Ref No./Cheque No.", "Debit", "Credit", "Balance"],
           body, [4, 5, 6], grid=False)
    return buf.getvalue()


def kotak_single_amount(rows: list[Row]) -> bytes:
    """Date | Narration | Chq/Ref No | Withdrawal (Dr)/Deposit (Cr) | Balance, amounts like 1,500.00(Dr)."""
    body = [[r.date.strftime("%d-%m-%Y"), r.narration, r.ref_col or "-",
             f"{money(r.amount)}({'Dr' if r.direction == 'Debit' else 'Cr'})", f"{money(r.balance)}(Cr)"] for r in rows]
    buf = io.BytesIO()
    _build(buf, 1, [55, 210, 70, 100, 100], ["Date", "Narration", "Chq/Ref No", "Withdrawal (Dr)/ Deposit (Cr)", "Balance"],
           body, [3, 4], grid=False)
    return buf.getvalue()


def rasterise_to_scan(pdf_bytes: bytes, dpi: int = 200) -> bytes:
    """Turn a digital PDF into an image-only (scanned-looking) PDF."""
    import pdfplumber
    from PIL import Image

    images = []
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            images.append(page.to_image(resolution=dpi).original.convert("L").convert("RGB"))
    out = io.BytesIO()
    images[0].save(out, "PDF", resolution=dpi, save_all=True, append_images=images[1:])
    return out.getvalue()


def to_csv(rows: list[Row], header_first: str = "Transaction Date") -> bytes:
    """A CSV export with the 'Transaction Date | Narration' header order and quoted money fields
    (real bank CSV exports commonly quote any field that itself contains a comma, such as
    Indian-grouped amounts like "1,500.00" - otherwise the comma would split the column)."""
    lines = ["Account Statement for XXXX1234", "", f"{header_first},Narration,Withdrawal,Deposit,Balance"]
    for r in rows:
        narr = '"' + r.narration.replace('"', '""') + '"'
        debit = money(r.amount) if r.direction == "Debit" else "0.00"
        credit = money(r.amount) if r.direction == "Credit" else "0.00"
        lines.append(f'{r.date.strftime("%d/%m/%Y")},{narr},"{debit}","{credit}","{money(r.balance)}"')
    return ("\n".join(lines) + "\n").encode("utf-8")
