"use client";

import { ChangeEvent, DragEvent, useMemo, useRef, useState } from "react";

type Category = "Cash deposit" | "Cash withdrawal" | "NEFT" | "UPI" | "Other";
type Direction = "Credit" | "Debit" | "Unknown";

type Transaction = {
  id: string;
  date: string;
  dateIso: string;
  category: Category;
  direction: Direction;
  beneficiary: string;
  narration: string;
  reference: string;
  amount: number;
  source: string;
};

type ParsedFile = {
  transactions: Transaction[];
  totalRows: number;
  unclassified: number;
};

const TARGET_CATEGORIES: Category[] = ["Cash deposit", "Cash withdrawal", "NEFT", "UPI"];
const categoryClass: Record<Category, string> = {
  "Cash deposit": "deposit",
  "Cash withdrawal": "withdrawal",
  NEFT: "neft",
  UPI: "upi",
  Other: "other",
};

const numberFormatter = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  maximumFractionDigits: 2,
});

const clean = (value: unknown) => String(value ?? "").replace(/\s+/g, " ").trim();

function parseAmount(value: unknown): number | null {
  const text = clean(value).replace(/[₹\s]/g, "");
  if (!text || /^(?:-|n\/?a)$/i.test(text)) return null;
  const negative = /^\(.*\)$/.test(text) || /\bDR\b/i.test(text);
  const numeric = Number(text.replace(/[(),]/g, "").replace(/(?:CR|DR)$/i, ""));
  if (!Number.isFinite(numeric)) return null;
  return negative ? -Math.abs(numeric) : Math.abs(numeric);
}

function parseIndianDate(value: unknown): { display: string; iso: string } | null {
  if (typeof value === "number" && value > 20000 && value < 80000) {
    const utc = new Date(Date.UTC(1899, 11, 30) + value * 86400000);
    return toDateParts(utc.getUTCFullYear(), utc.getUTCMonth() + 1, utc.getUTCDate());
  }
  const text = clean(value);
  if (!text) return null;
  const numeric = text.match(/(\d{1,2})[\/.\-](\d{1,2})[\/.\-](\d{2,4})/);
  if (numeric) {
    let year = Number(numeric[3]);
    if (year < 100) year += year > 70 ? 1900 : 2000;
    return toDateParts(year, Number(numeric[2]), Number(numeric[1]));
  }
  const named = text.match(/(\d{1,2})[\s\-]([A-Za-z]{3,9})[\s,\-](\d{2,4})/);
  if (named) {
    const month = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"].indexOf(
      named[2].slice(0, 3).toLowerCase(),
    );
    let year = Number(named[3]);
    if (year < 100) year += 2000;
    return month >= 0 ? toDateParts(year, month + 1, Number(named[1])) : null;
  }
  return null;
}

function toDateParts(year: number, month: number, day: number) {
  const test = new Date(Date.UTC(year, month - 1, day));
  if (test.getUTCFullYear() !== year || test.getUTCMonth() !== month - 1 || test.getUTCDate() !== day) return null;
  const display = new Intl.DateTimeFormat("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" }).format(test);
  return { display, iso: `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}` };
}

function normaliseKey(key: unknown) {
  return clean(key).toLowerCase().replace(/[^a-z0-9]/g, "");
}

function findValue(row: Record<string, unknown>, patterns: string[]) {
  const entry = Object.entries(row).find(([key]) => patterns.some((pattern) => normaliseKey(key).includes(pattern)));
  return entry ? entry[1] : "";
}

function getDirection(row: Record<string, unknown>, narration: string, debit: unknown, credit: unknown): Direction {
  if (parseAmount(credit) !== null) return "Credit";
  if (parseAmount(debit) !== null) return "Debit";
  const indicator = clean(findValue(row, ["drcr", "crdr", "type", "transactiontype"]));
  if (/\b(cr|credit)\b/i.test(indicator)) return "Credit";
  if (/\b(dr|debit)\b/i.test(indicator)) return "Debit";
  if (/\b(cr|credit)\b/i.test(narration)) return "Credit";
  if (/\b(dr|debit|withdrawal)\b/i.test(narration)) return "Debit";
  return "Unknown";
}

function categorise(narration: string, direction: Direction): Category {
  const text = narration.toLowerCase();
  if (/\bupi\b|upi[\/-]|\b(phonepe|gpay|google pay|paytm|bhim)\b/.test(text)) return "UPI";
  if (/\bneft\b/.test(text)) return "NEFT";
  if (/\bcash\s*(deposit|dep|received|credit)|\bby\s+cash\b/.test(text) || (direction === "Credit" && /\bcash\b/.test(text))) return "Cash deposit";
  if (/\b(cash\s*(withdrawal|withdraw|wd|w\/d)|atm\s*(cash|withdrawal)?|cash withdrawal)\b/.test(text) || (direction === "Debit" && /\bcash\b/.test(text))) return "Cash withdrawal";
  return "Other";
}

function findReference(narration: string, explicit: unknown) {
  const existing = clean(explicit);
  if (existing) return existing;
  const match = narration.match(/(?:UTR|REF|TXN|RRN)[\s:/\-#]*([A-Z0-9]{6,})/i) || narration.match(/\b[A-Z]{4,}\d{8,}\b/);
  return match ? clean(match[1] || match[0]) : "—";
}

function inferBeneficiary(narration: string, explicit: unknown, category: Category) {
  const supplied = clean(explicit);
  if (supplied && !/^(?:na|n\/a|-|nil)$/i.test(supplied)) return supplied;
  if (category === "Cash deposit") return "Cash deposit";
  if (category === "Cash withdrawal") return "Cash withdrawal";
  const trimmed = narration
    .replace(/\b(?:NEFT|UPI|IMPS|RTGS|INB|P2A|P2M|DR|CR|DEBIT|CREDIT)\b/gi, " ")
    .replace(/(?:UTR|REF|TXN|RRN)[\s:/\-#]*[A-Z0-9]{6,}/gi, " ")
    .replace(/\b[A-Z]{4}[A-Z0-9]{7,}\b/g, " ");
  const vpa = trimmed.match(/\b[A-Z0-9._-]+@[A-Z0-9._-]+\b/i);
  if (vpa) return vpa[0];
  const candidates = trimmed
    .split(/[|/]/)
    .map((part) => clean(part.replace(/^[\-:;,\s]+|[\-:;,\s]+$/g, "")))
    .filter((part) => part.length > 2 && !/^\d+(?:\.\d+)?$/.test(part) && !/^(?:to|from|transfer|payment|bank)$/i.test(part));
  const candidate = candidates.find((part) => /[a-z]/i.test(part));
  return candidate ? candidate.slice(0, 80) : "Review narration";
}

function rowsToTransactions(rows: unknown[][], source: string): ParsedFile {
  const usableRows = rows.filter((row) => row.some((cell) => clean(cell)));
  const headerIndex = usableRows.findIndex((row) => {
    const cells = row.map(normaliseKey);
    return cells.some((cell) => cell.includes("date")) && cells.some((cell) => /narration|description|particular|remark|detail/.test(cell));
  });
  const headers = headerIndex >= 0 ? usableRows[headerIndex].map((cell, index) => clean(cell) || `Column ${index + 1}`) : ["Date", "Narration", "Amount"];
  const dataRows = headerIndex >= 0 ? usableRows.slice(headerIndex + 1) : usableRows;
  const transactions: Transaction[] = [];
  let unclassified = 0;

  dataRows.forEach((cells, index) => {
    const row: Record<string, unknown> = {};
    headers.forEach((header, cellIndex) => {
      row[header] = cells[cellIndex] ?? "";
    });
    const dateValue = findValue(row, ["transactiondate", "txn date", "date", "valuedate"]);
    const date = parseIndianDate(dateValue || cells[0]);
    const narration = clean(findValue(row, ["narration", "description", "particular", "remark", "detail", "transaction"]));
    if (!date || !narration) return;
    const debit = findValue(row, ["withdrawal", "debit", "dramount", "debitamount"]);
    const credit = findValue(row, ["deposit", "credit", "cramount", "creditamount"]);
    const amountValue = findValue(row, ["amount", "transactionamount"]);
    const direction = getDirection(row, narration, debit, credit);
    const amount = Math.abs(parseAmount(credit) ?? parseAmount(debit) ?? parseAmount(amountValue) ?? 0);
    if (!amount) return;
    const category = categorise(narration, direction);
    if (category === "Other") unclassified += 1;
    transactions.push({
      id: `${source}-${index}-${date.iso}-${amount}`,
      date: date.display,
      dateIso: date.iso,
      category,
      direction,
      beneficiary: inferBeneficiary(narration, findValue(row, ["beneficiary", "payee", "counterparty", "partyname"]), category),
      narration,
      reference: findReference(narration, findValue(row, ["reference", "utr", "rrn", "transactionid", "txn id"])),
      amount,
      source,
    });
  });
  return { transactions, totalRows: dataRows.length, unclassified };
}

function parseCsv(text: string) {
  const rows: string[][] = [];
  let row: string[] = [];
  let cell = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (char === '"' && text[index + 1] === '"') { cell += '"'; index += 1; }
    else if (char === '"') quoted = !quoted;
    else if (char === "," && !quoted) { row.push(cell); cell = ""; }
    else if ((char === "\n" || char === "\r") && !quoted) {
      if (char === "\r" && text[index + 1] === "\n") index += 1;
      row.push(cell); rows.push(row); row = []; cell = "";
    } else cell += char;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  return rows;
}

function pdfTextToRows(text: string) {
  return text
    .split(/\n+/)
    .map((line) => {
      const dateMatch = line.match(/(\d{1,2}[\/.\-]\d{1,2}[\/.\-]\d{2,4}|\d{1,2}\s+[A-Za-z]{3}\s+\d{2,4})/);
      if (!dateMatch) return [];
      const dateAt = dateMatch.index ?? 0;
      const before = line.slice(0, dateAt).trim();
      const after = line.slice(dateAt + dateMatch[0].length).trim();
      const amountMatches = [...after.matchAll(/(?:₹\s*)?\d[\d,]*(?:\.\d{1,2})?\s*(?:CR|DR)?/gi)];
      if (!amountMatches.length) return [];
      const chosen = amountMatches[Math.max(0, amountMatches.length - 2)] || amountMatches[amountMatches.length - 1];
      const narration = `${before} ${after.slice(0, chosen.index ?? after.length)}`.trim();
      return [dateMatch[0], narration, chosen[0]];
    })
    .filter((row) => row.length);
}

async function parseFile(file: File): Promise<ParsedFile> {
  const extension = file.name.split(".").pop()?.toLowerCase();
  if (extension === "csv") return rowsToTransactions(parseCsv(await file.text()), file.name);
  if (extension === "xlsx" || extension === "xls") {
    const XLSX = await import("xlsx");
    const workbook = XLSX.read(await file.arrayBuffer(), { type: "array", cellDates: false });
    const rows = workbook.SheetNames.flatMap((name) => XLSX.utils.sheet_to_json<unknown[]>(workbook.Sheets[name], { header: 1, defval: "", raw: true }));
    return rowsToTransactions(rows, file.name);
  }
  if (extension === "pdf") {
    const pdfjs = await import("pdfjs-dist/legacy/build/pdf.mjs");
    const pdf = await pdfjs.getDocument({ data: new Uint8Array(await file.arrayBuffer()) }).promise;
    const lines: string[] = [];
    for (let pageNumber = 1; pageNumber <= pdf.numPages; pageNumber += 1) {
      const content = await (await pdf.getPage(pageNumber)).getTextContent();
      const items = content.items.filter((item): item is { str: string; transform: number[] } => "str" in item);
      const grouped = new Map<number, { x: number; str: string }[]>();
      items.forEach((item) => {
        const y = Math.round(item.transform[5]);
        grouped.set(y, [...(grouped.get(y) ?? []), { x: item.transform[4], str: item.str }]);
      });
      [...grouped.entries()].sort(([a], [b]) => b - a).forEach(([, lineItems]) => {
        lines.push(lineItems.sort((a, b) => a.x - b.x).map((item) => item.str).join(" "));
      });
    }
    const result = rowsToTransactions(pdfTextToRows(lines.join("\n")), file.name);
    if (!result.transactions.length) throw new Error("No readable transaction rows were found. If this is a scanned PDF, run OCR first or upload the CSV/XLSX statement.");
    return result;
  }
  throw new Error("Please upload a PDF, CSV, XLSX or XLS bank statement.");
}

function formatAmount(amount: number) { return numberFormatter.format(amount); }

export default function Home() {
  const fileInput = useRef<HTMLInputElement>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [fileName, setFileName] = useState("");
  const [status, setStatus] = useState<"idle" | "processing" | "ready" | "error">("idle");
  const [message, setMessage] = useState("");
  const [activeCategory, setActiveCategory] = useState<Category | "All">("All");
  const [search, setSearch] = useState("");
  const [dragging, setDragging] = useState(false);

  const targetTransactions = useMemo(() => transactions.filter((transaction) => TARGET_CATEGORIES.includes(transaction.category)), [transactions]);
  const filtered = useMemo(() => targetTransactions.filter((transaction) => {
    const categoryMatches = activeCategory === "All" || transaction.category === activeCategory;
    const searchText = `${transaction.beneficiary} ${transaction.narration} ${transaction.reference}`.toLowerCase();
    return categoryMatches && searchText.includes(search.trim().toLowerCase());
  }).sort((a, b) => b.dateIso.localeCompare(a.dateIso)), [targetTransactions, activeCategory, search]);

  const totals = useMemo(() => TARGET_CATEGORIES.map((category) => {
    const matches = targetTransactions.filter((transaction) => transaction.category === category);
    return { category, count: matches.length, amount: matches.reduce((sum, transaction) => sum + transaction.amount, 0) };
  }), [targetTransactions]);

  const handleUpload = async (file?: File) => {
    if (!file) return;
    setStatus("processing"); setMessage(""); setFileName(file.name);
    try {
      const parsed = await parseFile(file);
      setTransactions(parsed.transactions);
      setStatus("ready");
      const detected = parsed.transactions.length - parsed.unclassified;
      setMessage(`${detected} target transactions detected from ${parsed.totalRows} statement rows. ${parsed.unclassified ? `${parsed.unclassified} non-target rows were kept out of the review list.` : ""}`);
    } catch (error) {
      setTransactions([]); setStatus("error"); setMessage(error instanceof Error ? error.message : "We could not read that file.");
    }
  };

  const onInput = (event: ChangeEvent<HTMLInputElement>) => { void handleUpload(event.target.files?.[0]); event.target.value = ""; };
  const onDrop = (event: DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragging(false); void handleUpload(event.dataTransfer.files?.[0]); };

  const exportWorkbook = async () => {
    if (!targetTransactions.length) return;
    const XLSX = await import("xlsx");
    const workbook = XLSX.utils.book_new();
    const rowsFor = (items: Transaction[]) => items.map((transaction) => ({
      "Transaction date": transaction.date,
      "Category": transaction.category,
      "Direction": transaction.direction,
      "Beneficiary / payer": transaction.beneficiary,
      "Amount (INR)": transaction.amount,
      "Reference / UTR": transaction.reference === "—" ? "" : transaction.reference,
      "Statement narration": transaction.narration,
      "Source file": transaction.source,
    }));
    XLSX.utils.book_append_sheet(workbook, XLSX.utils.json_to_sheet(totals.map((total) => ({ Category: total.category, Transactions: total.count, "Total amount (INR)": total.amount }))), "Summary");
    TARGET_CATEGORIES.forEach((category) => {
      const sheet = XLSX.utils.json_to_sheet(rowsFor(targetTransactions.filter((transaction) => transaction.category === category)));
      XLSX.utils.book_append_sheet(workbook, sheet, category.replace(" ", " ").slice(0, 31));
    });
    XLSX.writeFile(workbook, `ledgerlens-category-review-${new Date().toISOString().slice(0, 10)}.xlsx`, { compression: true });
  };

  return (
    <main>
      <header className="topbar">
        <a className="brand" href="#top" aria-label="LedgerLens home"><span className="brand-mark">L</span><span>Ledger<span>Lens</span></span></a>
        <div className="topbar-note"><span className="live-dot" />Local processing. No statement storage.</div>
      </header>

      <section className="hero" id="top">
        <div className="eyebrow">Forensic transaction review</div>
        <h1>See the story inside<br /><em>every statement.</em></h1>
        <p>Upload an Indian bank statement and isolate cash, NEFT and UPI activity in a review-ready ledger.</p>
        <div className="hero-pills"><span>PDF</span><span>CSV</span><span>XLSX</span><span>Browser-based analysis</span></div>
      </section>

      <section className="workspace" aria-label="Bank statement analysis workspace">
        <div className="workspace-heading"><div><span className="section-kicker">Statement workspace</span><h2>Transaction analysis</h2></div><p>Dates and beneficiaries are extracted from the statement itself.</p></div>

        <div
          className={`upload-card ${dragging ? "is-dragging" : ""}`}
          onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
          onDragOver={(event) => event.preventDefault()}
          onDragLeave={() => setDragging(false)}
          onDrop={onDrop}
        >
          <div className="upload-icon">↑</div>
          <div><strong>{status === "processing" ? "Reading your statement…" : "Drop a bank statement here"}</strong><span>or choose a PDF, CSV, XLSX or XLS file</span></div>
          <button className="button button-dark" type="button" onClick={() => fileInput.current?.click()} disabled={status === "processing"}>{status === "processing" ? "Analysing" : "Select statement"}</button>
          <input ref={fileInput} type="file" accept=".pdf,.csv,.xlsx,.xls,application/pdf,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={onInput} />
        </div>
        <div className={`privacy-line ${status === "error" ? "error" : ""}`}><span>{status === "error" ? "!" : "✓"}</span>{message || "Your document stays on this device. Scanned PDFs should be OCR’d before upload."}</div>

        <div className="summary-grid">
          {totals.map((total) => <button key={total.category} className={`summary-card ${categoryClass[total.category]} ${activeCategory === total.category ? "active" : ""}`} onClick={() => setActiveCategory(activeCategory === total.category ? "All" : total.category)} type="button"><span>{total.category}</span><strong>{total.count.toLocaleString("en-IN")}</strong><small>{formatAmount(total.amount)}</small></button>)}
        </div>

        <div className="table-card">
          <div className="table-toolbar">
            <div><span className="section-kicker">Categorised activity</span><h3>{fileName ? fileName : "Upload a statement to begin"}</h3></div>
            <div className="toolbar-actions"><label className="search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name or reference" aria-label="Search transactions" /></label><button className="button export" type="button" onClick={() => void exportWorkbook()} disabled={!targetTransactions.length}><span>↓</span> Export Excel</button></div>
          </div>
          <div className="filters" aria-label="Transaction category filters"><button className={activeCategory === "All" ? "selected" : ""} onClick={() => setActiveCategory("All")} type="button">All detected <b>{targetTransactions.length}</b></button>{totals.map((total) => <button key={total.category} className={activeCategory === total.category ? "selected" : ""} onClick={() => setActiveCategory(total.category)} type="button">{total.category} <b>{total.count}</b></button>)}</div>
          <div className="table-wrap">
            {filtered.length ? <table><thead><tr><th>Transaction date</th><th>Category</th><th>Beneficiary / payer</th><th>Reference</th><th>Narration</th><th className="amount">Amount</th></tr></thead><tbody>{filtered.map((transaction) => <tr key={transaction.id}><td className="date-cell">{transaction.date}<small>{transaction.direction}</small></td><td><span className={`tag ${categoryClass[transaction.category]}`}>{transaction.category}</span></td><td className="beneficiary">{transaction.beneficiary}</td><td className="reference">{transaction.reference}</td><td className="narration">{transaction.narration}</td><td className="amount">{formatAmount(transaction.amount)}</td></tr>)}</tbody></table> : <div className="empty-state"><div>⌁</div><strong>{status === "ready" ? "No matching activity" : "Your forensic review starts here"}</strong><p>{status === "ready" ? "Try another category or search phrase." : "Upload a statement to extract cash deposits, cash withdrawals, NEFT and UPI transactions."}</p></div>}
          </div>
          <footer className="table-footer"><span>{targetTransactions.length ? `${filtered.length} of ${targetTransactions.length} detected transactions shown` : "No statement loaded"}</span><span>Review beneficiary inference against the original narration before relying on it.</span></footer>
        </div>
      </section>

      <section className="method"><div><span className="section-kicker">Made for the evidence trail</span><h2>Structured for review.<br />Built for speed.</h2></div><div className="method-items"><p><b>01</b>Statement is read in the browser</p><p><b>02</b>Transaction dates are normalised in Indian date format</p><p><b>03</b>Excel export creates a separate sheet for each category</p></div></section>
      <footer className="site-footer"><span>LedgerLens</span><span>Forensic statement analysis</span><span>Designed for Indian bank statement formats</span></footer>
    </main>
  );
}
