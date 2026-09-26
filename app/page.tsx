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

// Extraction, OCR and narration classification now happen server-side (see backend/, a Python/
// FastAPI service using pdfplumber for PDF tables, pytesseract for scanned pages, and regex-based
// narration parsing) rather than in the browser. This keeps one classification pipeline instead of
// two, and makes scanned statements (which pdfjs-dist text extraction cannot read at all) work.
const API_BASE = (process.env.NEXT_PUBLIC_LEDGERLENS_API_URL ?? "http://localhost:8000").replace(/\/$/, "");

type ApiTransaction = {
  id: string; date: string; dateIso: string; category: Category; direction: Direction;
  beneficiary: string; reference: string; narration: string; amount: number; source: string;
};

type ApiError = { code: string; message: string };

// PASSWORD_REQUIRED (401) surfaces as a thrown error carrying the code, so the caller can prompt
// for a password and retry the same file instead of just showing a generic failure message.
class StatementApiError extends Error {
  code: string;
  constructor(code: string, message: string) {
    super(message);
    this.code = code;
  }
}

async function analyzeFile(file: File, password?: string): Promise<ParsedFile> {
  const body = new FormData();
  body.append("file", file);
  if (password) body.append("password", password);
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/api/analyze`, { method: "POST", body });
  } catch {
    throw new Error(`Could not reach the LedgerLens API at ${API_BASE}. Is the backend running?`);
  }
  if (!response.ok) {
    const error = (await response.json().catch(() => null)) as ApiError | null;
    throw new StatementApiError(error?.code ?? "UNKNOWN", error?.message ?? "That file could not be read.");
  }
  const data = await response.json() as {
    transactions: ApiTransaction[];
    totalRows: number;
    summary: { otherCount: number };
  };
  return {
    transactions: data.transactions.map((t) => ({
      id: t.id, date: t.date, dateIso: t.dateIso, category: t.category, direction: t.direction,
      beneficiary: t.beneficiary, narration: t.narration, reference: t.reference || "—",
      amount: t.amount, source: t.source,
    })),
    totalRows: data.totalRows,
    unclassified: data.summary.otherCount,
  };
}

function formatAmount(amount: number) { return numberFormatter.format(amount); }

export default function Home() {
  const fileInput = useRef<HTMLInputElement>(null);
  const [transactions, setTransactions] = useState<Transaction[]>([]);
  const [fileName, setFileName] = useState("");
  const [status, setStatus] = useState<"idle" | "processing" | "ready" | "error">("idle");
  const [message, setMessage] = useState("");
  const [activeCategory, setActiveCategory] = useState<Category | "All">("All");
  const [activeCounterparty, setActiveCounterparty] = useState("All");
  const [search, setSearch] = useState("");
  const [dragging, setDragging] = useState(false);

  // Switching category clears any counterparty drill-down from before - otherwise the two filters
  // combine (AND logic) and can silently show zero rows for a category that plainly has matches,
  // just because the previously selected counterparty doesn't happen to appear in it.
  const selectCategory = (category: Category | "All") => { setActiveCategory(category); setActiveCounterparty("All"); };

  const targetTransactions = useMemo(() => transactions.filter((transaction) => TARGET_CATEGORIES.includes(transaction.category)), [transactions]);
  // "Cash deposit" / "Cash withdrawal" / "Review narration" are display_counterparty()'s own
  // fallback labels for a transaction with no counterparty the narration parser could identify -
  // listing those as if they were real counterparty names would be misleading.
  const counterpartyOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const transaction of targetTransactions) {
      const name = transaction.beneficiary;
      if (name === "Cash deposit" || name === "Cash withdrawal" || name === "Review narration") continue;
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => a.name.localeCompare(b.name));
  }, [targetTransactions]);

  const filtered = useMemo(() => targetTransactions.filter((transaction) => {
    const categoryMatches = activeCategory === "All" || transaction.category === activeCategory;
    const counterpartyMatches = activeCounterparty === "All" || transaction.beneficiary === activeCounterparty;
    const searchText = `${transaction.beneficiary} ${transaction.narration} ${transaction.reference}`.toLowerCase();
    return categoryMatches && counterpartyMatches && searchText.includes(search.trim().toLowerCase());
  }).sort((a, b) => b.dateIso.localeCompare(a.dateIso)), [targetTransactions, activeCategory, activeCounterparty, search]);

  const totals = useMemo(() => TARGET_CATEGORIES.map((category) => {
    const matches = targetTransactions.filter((transaction) => transaction.category === category);
    return { category, count: matches.length, amount: matches.reduce((sum, transaction) => sum + transaction.amount, 0) };
  }), [targetTransactions]);

  const handleUpload = async (file?: File, password?: string) => {
    if (!file) return;
    setStatus("processing"); setMessage(""); setFileName(file.name);
    try {
      const parsed = await analyzeFile(file, password);
      setTransactions(parsed.transactions);
      // A filter left over from a previously loaded statement (a category, a counterparty, a
      // search term) can silently hide everything in a new one if it doesn't happen to match -
      // start every newly loaded statement with a clean, unfiltered view.
      setActiveCategory("All"); setActiveCounterparty("All"); setSearch("");
      setStatus("ready");
      const detected = parsed.transactions.length;
      setMessage(`${detected} target transactions detected from ${parsed.totalRows} statement rows. ${parsed.unclassified ? `${parsed.unclassified} non-target rows were kept out of the review list.` : ""}`);
    } catch (error) {
      // Password-protected PDFs get a second chance: prompt once and retry with what's typed.
      // A blank/cancelled prompt is treated as giving up rather than looping forever.
      if (error instanceof StatementApiError && (error.code === "PASSWORD_REQUIRED" || error.code === "WRONG_PASSWORD")) {
        const entered = window.prompt(error.code === "WRONG_PASSWORD" ? "That password was incorrect. Try again:" : "This PDF is password-protected. Enter the password:");
        if (entered) { void handleUpload(file, entered); return; }
      }
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
        <div className="topbar-note"><span className="live-dot" />Analysed on request. No statement storage.</div>
      </header>

      <section className="hero" id="top">
        <div className="eyebrow">Forensic transaction review</div>
        <h1>See the story inside<br /><em>every statement.</em></h1>
        <p>Upload an Indian bank statement and isolate cash, NEFT and UPI activity in a review-ready ledger.</p>
        <div className="hero-pills"><span>PDF</span><span>CSV</span><span>XLSX</span><span>Server-side OCR</span></div>
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
        <div className={`privacy-line ${status === "error" ? "error" : ""}`}><span>{status === "error" ? "!" : "✓"}</span>{message || "Statements are sent to the LedgerLens analysis service for extraction. Scanned PDFs are OCR'd automatically."}</div>

        <div className="summary-grid">
          {totals.map((total) => <button key={total.category} className={`summary-card ${categoryClass[total.category]} ${activeCategory === total.category ? "active" : ""}`} onClick={() => selectCategory(activeCategory === total.category ? "All" : total.category)} type="button"><span>{total.category}</span><strong>{total.count.toLocaleString("en-IN")}</strong><small>{formatAmount(total.amount)}</small></button>)}
          <div className={`summary-card counterparty ${activeCounterparty !== "All" ? "active" : ""}`}>
            <span>Counterparty</span>
            <select
              value={activeCounterparty}
              onChange={(event) => setActiveCounterparty(event.target.value)}
              disabled={!counterpartyOptions.length}
              aria-label="Filter by counterparty"
            >
              <option value="All">All counterparties</option>
              {counterpartyOptions.map((option) => <option key={option.name} value={option.name}>{option.name} ({option.count})</option>)}
            </select>
            <small>{counterpartyOptions.length ? `${counterpartyOptions.length} identified` : "Upload a statement"}</small>
          </div>
        </div>

        <div className="table-card">
          <div className="table-toolbar">
            <div><span className="section-kicker">Categorised activity</span><h3>{fileName ? fileName : "Upload a statement to begin"}</h3></div>
            <div className="toolbar-actions"><label className="search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name or reference" aria-label="Search transactions" /></label><button className="button export" type="button" onClick={() => void exportWorkbook()} disabled={!targetTransactions.length}><span>↓</span> Export Excel</button></div>
          </div>
          <div className="filters" aria-label="Transaction category filters"><button className={activeCategory === "All" ? "selected" : ""} onClick={() => selectCategory("All")} type="button">All detected <b>{targetTransactions.length}</b></button>{totals.map((total) => <button key={total.category} className={activeCategory === total.category ? "selected" : ""} onClick={() => selectCategory(total.category)} type="button">{total.category} <b>{total.count}</b></button>)}</div>
          <div className="table-wrap">
            {filtered.length ? <table><thead><tr><th>Transaction date</th><th>Category</th><th>Beneficiary / payer</th><th>Reference</th><th>Narration</th><th className="amount">Amount</th></tr></thead><tbody>{filtered.map((transaction) => <tr key={transaction.id}><td className="date-cell">{transaction.date}<small>{transaction.direction}</small></td><td><span className={`tag ${categoryClass[transaction.category]}`}>{transaction.category}</span></td><td className="beneficiary">{transaction.beneficiary}</td><td className="reference">{transaction.reference}</td><td className="narration">{transaction.narration}</td><td className="amount">{formatAmount(transaction.amount)}</td></tr>)}</tbody></table> : <div className="empty-state"><div>⌁</div><strong>{status === "ready" ? "No matching activity" : "Your forensic review starts here"}</strong><p>{status === "ready" ? "Try another category or search phrase." : "Upload a statement to extract cash deposits, cash withdrawals, NEFT and UPI transactions."}</p></div>}
          </div>
          <footer className="table-footer"><span>{targetTransactions.length ? `${filtered.length} of ${targetTransactions.length} detected transactions shown` : "No statement loaded"}</span><span>Review beneficiary inference against the original narration before relying on it.</span></footer>
        </div>
      </section>

      <section className="method"><div><span className="section-kicker">Made for the evidence trail</span><h2>Structured for review.<br />Built for speed.</h2></div><div className="method-items"><p><b>01</b>Statement is parsed by the analysis service</p><p><b>02</b>Transaction dates are normalised in Indian date format</p><p><b>03</b>Excel export creates a separate sheet for each category</p></div></section>
      <footer className="site-footer"><span>LedgerLens</span><span>Forensic statement analysis</span><span>Designed for Indian bank statement formats</span></footer>
    </main>
  );
}
