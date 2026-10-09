"use client";

import { ChangeEvent, DragEvent, useEffect, useMemo, useRef, useState } from "react";

type Category = "Cash deposit" | "Cash withdrawal" | "NEFT" | "UPI" | "IMPS" | "Other";
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
  statementId?: string; // which uploaded statement the row came from
  phone: string; // the 10-digit mobile number named in a UPI narration, or ""
  unnamed?: boolean; // no counterparty could be read; beneficiary holds the kind of row instead
};

type StatementEntry = {
  id: string;
  name: string;
  signature: string; // name + size + modified time, to recognise the same file chosen twice
  status: "processing" | "ready" | "error";
  message: string;
  transactions: Transaction[];
  totalRows: number;
  unclassified: number;
};

type ParsedFile = {
  transactions: Transaction[];
  totalRows: number;
  unclassified: number;
};

// The five categories the review is about, plus "Other" for everything the classifier left alone.
const TARGET_CATEGORIES: Category[] = ["Cash deposit", "Cash withdrawal", "NEFT", "UPI", "IMPS", "Other"];
const categoryClass: Record<Category, string> = {
  "Cash deposit": "deposit",
  "Cash withdrawal": "withdrawal",
  NEFT: "neft",
  UPI: "upi",
  IMPS: "imps",
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
  beneficiary: string; reference: string; narration: string; amount: number; source: string; channel?: string; phone?: string;
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
      // An "Other" row has no counterparty the parser could name; show what kind of row it is instead.
      unnamed: t.category === "Other" && t.beneficiary === "Review narration",
      beneficiary: t.category === "Other" && t.beneficiary === "Review narration" ? (t.channel || "Unclassified") : t.beneficiary, narration: t.narration, reference: t.reference || "—",
      amount: t.amount, source: t.source, phone: t.phone ?? "",
    })),
    totalRows: data.totalRows,
    unclassified: data.summary.otherCount,
  };
}

function formatDay(iso: string) { return new Date(`${iso}T00:00:00Z`).toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" }); }
function formatAmount(amount: number) { return numberFormatter.format(amount); }
// Amounts are stored as absolute values (see backend Money model) with direction tracked
// separately, so the sign shown here is derived, not part of the number itself.
function directionClass(direction: Direction) { return direction === "Debit" ? "debit" : direction === "Credit" ? "credit" : ""; }
function signedAmount(direction: Direction, amount: number) { return `${direction === "Debit" ? "−" : direction === "Credit" ? "+" : ""}${formatAmount(amount)}`; }
function formatNet(amount: number) { return `${amount < 0 ? "−" : "+"}${formatAmount(Math.abs(amount))}`; }

export default function Home() {
  const fileInput = useRef<HTMLInputElement>(null);
  const [statements, setStatements] = useState<StatementEntry[]>([]);
  const [activeStatement, setActiveStatement] = useState("All");
  const nextStatementId = useRef(1);
  const [activeCategory, setActiveCategory] = useState<Category | "All">("All");
  const [activeCounterparty, setActiveCounterparty] = useState("All");
  const [activePhone, setActivePhone] = useState("All");
  const [search, setSearch] = useState("");
  // Materiality: keep only transactions at/above ("above") or at/below ("below") an amount.
  const [materialityMode, setMaterialityMode] = useState<"above" | "below">("above");
  const [materialityInput, setMaterialityInput] = useState("");
  const [materialityOpen, setMaterialityOpen] = useState(false);
  const materialityAmount = (() => { const value = Number(materialityInput.replace(/[,\s₹]/g, "")); return materialityInput.trim() !== "" && Number.isFinite(value) && value >= 0 ? value : null; })();
  const [dragging, setDragging] = useState(false);

  // Switching category clears any counterparty drill-down from before - otherwise the two filters
  // combine (AND logic) and can silently show zero rows for a category that plainly has matches,
  // just because the previously selected counterparty doesn't happen to appear in it.
  const selectCategory = (category: Category | "All") => { setActiveCategory(category); setActiveCounterparty("All"); setActivePhone("All"); };

  // Statements that cover the same dates can repeat the same transactions; counting both copies
  // would double the totals. A repeat is the same date, direction and amount with the same
  // reference (or, with no reference, the same narration) in a *different* statement.
  const overlap = useMemo(() => {
    const ready = statements.filter((statement) => statement.status === "ready" && statement.transactions.length);
    if (ready.length < 2) return null;
    const keyOf = (t: Transaction) => `${t.dateIso}|${t.direction}|${t.amount.toFixed(2)}|${t.reference !== "—" ? t.reference : t.narration.slice(0, 40)}`;
    const firstSeen = new Map<string, string>();
    const duplicates = new Set<string>(); // "statementId:rowId" of the later copy
    for (const statement of ready) for (const t of statement.transactions) {
      const key = keyOf(t);
      const owner = firstSeen.get(key);
      if (owner === undefined) firstSeen.set(key, statement.id);
      else if (owner !== statement.id) duplicates.add(`${statement.id}:${t.id}`);
    }
    const spans = ready.map((statement) => { const dates = statement.transactions.map((t) => t.dateIso).sort(); return { name: statement.name, from: dates[0], to: dates[dates.length - 1] }; });
    const periods: { a: string; b: string; from: string; to: string }[] = [];
    for (let i = 0; i < spans.length; i++) for (let j = i + 1; j < spans.length; j++) {
      const from = spans[i].from > spans[j].from ? spans[i].from : spans[j].from;
      const to = spans[i].to < spans[j].to ? spans[i].to : spans[j].to;
      if (from <= to) periods.push({ a: spans[i].name, b: spans[j].name, from, to });
    }
    return periods.length || duplicates.size ? { periods, duplicates } : null;
  }, [statements]);
  const [dropDuplicates, setDropDuplicates] = useState(false);

  // Every statement that has been read, merged into one list. Row ids are only unique within a
  // statement, so they are prefixed with the statement's own id here.
  const transactions = useMemo(() => statements.flatMap((statement) => statement.transactions
    .filter((transaction) => !(dropDuplicates && overlap?.duplicates.has(`${statement.id}:${transaction.id}`)))
    .map((transaction) => ({ ...transaction, id: `${statement.id}:${transaction.id}`, statementId: statement.id }))), [statements, dropDuplicates, overlap]);
  const status: "idle" | "processing" | "ready" | "error" = !statements.length ? "idle" : statements.some((statement) => statement.status === "processing") ? "processing" : statements.some((statement) => statement.status === "ready") ? "ready" : "error";
  const multiple = statements.length > 1;
  const readyStatements = statements.filter((statement) => statement.status === "ready");
  const fileName = statements.length === 1 ? statements[0].name : statements.length ? `${statements.length} statements combined` : "";
  const message = (() => {
    if (!statements.length) return "";
    const failed = statements.filter((statement) => statement.status === "error");
    if (status === "processing") { const done = statements.filter((statement) => statement.status !== "processing").length; return `Reading statement ${done + 1} of ${statements.length}…`; }
    const rows = readyStatements.reduce((sum, statement) => sum + statement.totalRows, 0);
    const other = readyStatements.reduce((sum, statement) => sum + statement.unclassified, 0);
    const detected = readyStatements.reduce((sum, statement) => sum + statement.transactions.length - statement.unclassified, 0);
    const failure = failed.length ? ` ${failed.length === 1 ? failed[0].message : `${failed.length} files could not be read.`}` : "";
    if (!readyStatements.length) return failed[0]?.message ?? "We could not read that file.";
    return `${detected} target transactions detected from ${rows} statement rows${multiple ? ` across ${readyStatements.length} statements` : ""}.${other ? ` ${other} other transactions are listed under Other.` : ""}${failure}`;
  })();

  const targetTransactions = useMemo(() => transactions.filter((transaction) => TARGET_CATEGORIES.includes(transaction.category)), [transactions]);
  // "Cash deposit" / "Cash withdrawal" / "Review narration" are display_counterparty()'s own
  // fallback labels for a transaction with no counterparty the narration parser could identify -
  // listing those as if they were real counterparty names would be misleading.
  const counterpartyOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const transaction of targetTransactions) {
      if (transaction.unnamed) continue;
      const name = transaction.beneficiary;
      if (name === "Cash deposit" || name === "Cash withdrawal" || name === "Review narration") continue;
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts.entries()].map(([name, count]) => ({ name, count })).sort((a, b) => a.name.localeCompare(b.name));
  }, [targetTransactions]);

  // Phone numbers found in the rows being looked at (the selected category / statement), most
  // frequent first, so the dropdown only offers numbers that would actually match something.
  const phoneOptions = useMemo(() => {
    const counts = new Map<string, number>();
    for (const transaction of targetTransactions) {
      if (!transaction.phone) continue;
      if (activeCategory !== "All" && transaction.category !== activeCategory) continue;
      if (activeStatement !== "All" && transaction.statementId !== activeStatement) continue;
      counts.set(transaction.phone, (counts.get(transaction.phone) ?? 0) + 1);
    }
    return [...counts.entries()].map(([phone, count]) => ({ phone, count })).sort((a, b) => b.count - a.count || a.phone.localeCompare(b.phone));
  }, [targetTransactions, activeCategory, activeStatement]);

  const chronological = useMemo(() => targetTransactions.filter((transaction) => {
    const categoryMatches = activeCategory === "All" || transaction.category === activeCategory;
    const counterpartyMatches = activeCounterparty === "All" || transaction.beneficiary === activeCounterparty;
    const phoneMatches = activePhone === "All" || transaction.phone === activePhone;
    const statementMatches = activeStatement === "All" || transaction.statementId === activeStatement;
    const searchText = `${transaction.beneficiary} ${transaction.narration} ${transaction.reference}`.toLowerCase();
    const materialityMatches = materialityAmount === null || (materialityMode === "above" ? transaction.amount >= materialityAmount : transaction.amount <= materialityAmount);
    return categoryMatches && counterpartyMatches && phoneMatches && statementMatches && materialityMatches && searchText.includes(search.trim().toLowerCase());
  }).sort((a, b) => a.dateIso.localeCompare(b.dateIso)), [targetTransactions, activeCategory, activeCounterparty, activePhone, activeStatement, search, materialityMode, materialityAmount]);
  // Oldest first is the statement's own order. Newest first is that order reversed outright, so
  // transactions on the same day also run latest to earliest. The exports follow the table.
  const [newestFirst, setNewestFirst] = useState(false);
  const filtered = useMemo(() => (newestFirst ? [...chronological].reverse() : chronological), [chronological, newestFirst]);

  // Net, not gross: a category like NEFT or UPI can hold both incoming and outgoing transactions,
  // so summing every amount as positive would overstate what actually moved. Cash deposit/
  // withdrawal are effectively one-directional already, so this only changes NEFT/UPI in practice.
  const totals = useMemo(() => TARGET_CATEGORIES.map((category) => {
    const matches = targetTransactions.filter((transaction) => transaction.category === category);
    const net = matches.reduce((sum, transaction) => sum + (transaction.direction === "Debit" ? -transaction.amount : transaction.amount), 0);
    return { category, count: matches.length, amount: net };
  }), [targetTransactions]);

  const patchStatement = (id: string, patch: Partial<StatementEntry>) => setStatements((current) => current.map((statement) => (statement.id === id ? { ...statement, ...patch } : statement)));

  // Read one file into its own entry. Password-protected PDFs get a second chance: prompt once and
  // retry with what's typed. A blank/cancelled prompt is treated as giving up rather than looping.
  const readStatement = async (id: string, file: File, password?: string): Promise<void> => {
    try {
      const parsed = await analyzeFile(file, password);
      patchStatement(id, { status: "ready", message: "", transactions: parsed.transactions, totalRows: parsed.totalRows, unclassified: parsed.unclassified });
    } catch (error) {
      if (error instanceof StatementApiError && (error.code === "PASSWORD_REQUIRED" || error.code === "WRONG_PASSWORD")) {
        const entered = window.prompt(`${file.name}: ${error.code === "WRONG_PASSWORD" ? "that password was incorrect. Try again:" : "this PDF is password-protected. Enter the password:"}`);
        if (entered) return readStatement(id, file, entered);
      }
      patchStatement(id, { status: "error", message: error instanceof Error ? error.message : "We could not read that file." });
    }
  };

  // Uploading adds to what is already loaded (use Clear all to start over). Files are read one at a
  // time: a scanned statement is OCR'd page by page, and several at once would only slow each other.
  const handleFiles = async (incoming?: FileList | File[] | null) => {
    const files = Array.from(incoming ?? []);
    if (!files.length) return;
    const known = new Set(statements.map((statement) => statement.signature));
    const fresh = files.filter((file) => { const signature = `${file.name}|${file.size}|${file.lastModified}`; if (known.has(signature)) return false; known.add(signature); return true; });
    if (!fresh.length) return;
    const entries: { entry: StatementEntry; file: File }[] = fresh.map((file) => ({
      file,
      entry: { id: `s${nextStatementId.current++}`, name: file.name, signature: `${file.name}|${file.size}|${file.lastModified}`, status: "processing", message: "", transactions: [], totalRows: 0, unclassified: 0 },
    }));
    setStatements((current) => [...current, ...entries.map(({ entry }) => entry)]);
    // The counterparty list changes with the data; start it fresh so a stale pick cannot hide everything.
    setActiveCounterparty("All"); setActivePhone("All"); setMaterialityOpen(false);
    for (const { entry, file } of entries) await readStatement(entry.id, file);
  };

  const removeStatement = (id: string) => {
    setStatements((current) => current.filter((statement) => statement.id !== id));
    setActiveCounterparty("All"); setActivePhone("All");
    if (activeStatement === id) setActiveStatement("All");
  };
  const clearStatements = () => { setStatements([]); setDropDuplicates(false); setActiveStatement("All"); setActiveCategory("All"); setActiveCounterparty("All"); setActivePhone("All"); setSearch(""); setMaterialityInput(""); setMaterialityOpen(false); };

  const onInput = (event: ChangeEvent<HTMLInputElement>) => { void handleFiles(event.target.files); event.target.value = ""; };
  const onDrop = (event: DragEvent<HTMLDivElement>) => { event.preventDefault(); setDragging(false); void handleFiles(event.dataTransfer.files); };

  // The selections currently narrowing the table, as label/value pairs (used by both exports).
  const describeSelection = () => {
    const applied: { Filter: string; Value: string }[] = [];
    if (activeCategory !== "All") applied.push({ Filter: "Category", Value: activeCategory });
    if (activeCounterparty !== "All") applied.push({ Filter: "Counterparty", Value: activeCounterparty });
    if (activePhone !== "All") applied.push({ Filter: "Phone number", Value: activePhone });
    if (activeStatement !== "All") applied.push({ Filter: "Statement", Value: statements.find((statement) => statement.id === activeStatement)?.name ?? activeStatement });
    if (materialityAmount !== null) applied.push({ Filter: "Materiality", Value: `${materialityMode === "above" ? "at or above" : "at or below"} ${formatAmount(materialityAmount)}` });
    if (search.trim()) applied.push({ Filter: "Search", Value: search.trim() });
    if (dropDuplicates && overlap?.duplicates.size) applied.push({ Filter: "Repeated transactions", Value: `${overlap.duplicates.size} hidden (appear in more than one statement)` });
    return applied;
  };

  const exportWorkbook = async () => {
    if (!filtered.length) return;
    const XLSX = await import("xlsx-js-style");
    const workbook = XLSX.utils.book_new();
    // json_to_sheet + a bold header row (the first row of every sheet holds the column headers).
    const toSheet = (rows: Record<string, unknown>[]) => {
      const sheet = XLSX.utils.json_to_sheet(rows);
      const range = XLSX.utils.decode_range(sheet["!ref"] ?? "A1");
      for (let col = range.s.c; col <= range.e.c; col++) {
        const cell = sheet[XLSX.utils.encode_cell({ r: range.s.r, c: col })];
        if (cell) cell.s = { font: { bold: true } };
      }
      return sheet;
    };
    const rowsFor = (items: Transaction[]) => items.map((transaction) => ({
      "Transaction date": transaction.date,
      "Category": transaction.category,
      "Direction": transaction.direction,
      "Beneficiary / payer": transaction.beneficiary,
      "Phone number": transaction.phone,
      "Amount (INR)": transaction.amount,
      "Reference / UTR": transaction.reference === "—" ? "" : transaction.reference,
      "Statement narration": transaction.narration,
      "Source file": transaction.source,
    }));
    const stamp = new Date().toISOString().slice(0, 10);
    // The export is what the table shows: every selection made on the dashboard (category,
    // counterparty, phone number, statement, materiality, search, hidden repeats) narrows the rows.
    const applied = describeSelection();
    const filtersSheet = () => toSheet([...applied, { Filter: "Rows exported", Value: String(filtered.length) }, { Filter: "Exported on", Value: new Date().toLocaleString("en-IN") }]);

    if (activeCategory !== "All") {
      XLSX.utils.book_append_sheet(workbook, toSheet(rowsFor(filtered)), activeCategory.slice(0, 31));
      XLSX.utils.book_append_sheet(workbook, filtersSheet(), "Filters applied");
      XLSX.writeFile(workbook, `ledgerlens-${activeCategory.toLowerCase().replace(/\s+/g, "-")}${applied.length > 1 ? "-filtered" : ""}-${stamp}.xlsx`, { compression: true });
      return;
    }
    // "All" categories: with nothing else selected this is the full review (every category sheet);
    // with other selections it is the same layout built from just the rows that match them.
    const narrowed = applied.length > 0;
    const summaryRows = TARGET_CATEGORIES.map((category) => {
      const matches = filtered.filter((transaction) => transaction.category === category);
      const net = matches.reduce((sum, transaction) => sum + (transaction.direction === "Debit" ? -transaction.amount : transaction.amount), 0);
      return { Category: category, Transactions: matches.length, "Net amount (INR)": net };
    });
    XLSX.utils.book_append_sheet(workbook, toSheet(summaryRows), "Summary");
    TARGET_CATEGORIES.forEach((category) => {
      const matches = filtered.filter((transaction) => transaction.category === category);
      if (narrowed && !matches.length) return;
      XLSX.utils.book_append_sheet(workbook, toSheet(rowsFor(matches)), category.slice(0, 31));
    });
    if (narrowed) XLSX.utils.book_append_sheet(workbook, filtersSheet(), "Filters applied");
    XLSX.writeFile(workbook, `ledgerlens-${narrowed ? "filtered-review" : "category-review"}-${stamp}.xlsx`, { compression: true });
  };

  // PDF: the same rows and selections as the Excel export, laid out as a printable report.
  const exportPdf = async () => {
    if (!filtered.length) return;
    const [{ jsPDF }, { autoTable }] = await Promise.all([import("jspdf"), import("jspdf-autotable")]);
    const doc = new jsPDF({ orientation: "landscape", unit: "pt", format: "a4" });
    const margin = 30;
    const pageWidth = doc.internal.pageSize.getWidth();
    const applied = describeSelection();
    const stamp = new Date().toISOString().slice(0, 10);
    // jsPDF's built-in fonts have no rupee sign, so amounts are plain numbers under an "INR" heading.
    const money = (value: number) => new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(value);
    const signed = (t: Transaction) => `${t.direction === "Debit" ? "-" : t.direction === "Credit" ? "+" : ""}${money(t.amount)}`;

    doc.setFont("helvetica", "bold"); doc.setFontSize(16); doc.text("LedgerLens - transaction review", margin, 40);
    doc.setFont("helvetica", "normal"); doc.setFontSize(9); doc.setTextColor(90);
    const sources = statements.filter((statement) => statement.status === "ready").map((statement) => statement.name).join(", ");
    doc.text(`Statement${statements.length > 1 ? "s" : ""}: ${sources}`, margin, 56, { maxWidth: pageWidth - margin * 2 });
    doc.text(`Exported ${new Date().toLocaleString("en-IN")}  |  ${filtered.length.toLocaleString("en-IN")} transaction${filtered.length === 1 ? "" : "s"}`, margin, 69);
    doc.text(applied.length ? `Selection: ${applied.map((item) => `${item.Filter} = ${item.Value}`).join("; ")}` : "Selection: all transactions", margin, 82, { maxWidth: pageWidth - margin * 2 });
    doc.setTextColor(0);

    const summary = TARGET_CATEGORIES.map((category) => {
      const matches = filtered.filter((transaction) => transaction.category === category);
      const net = matches.reduce((sum, transaction) => sum + (transaction.direction === "Debit" ? -transaction.amount : transaction.amount), 0);
      return { category, count: matches.length, net };
    }).filter((row) => row.count > 0 || !applied.length);
    autoTable(doc, {
      startY: 100, margin: { left: margin, right: margin }, tableWidth: 330,
      head: [["Category", "Transactions", "Net amount (INR)"]],
      body: summary.map((row) => [row.category, row.count.toLocaleString("en-IN"), `${row.net < 0 ? "-" : "+"}${money(Math.abs(row.net))}`]),
      styles: { fontSize: 8, cellPadding: 3 }, headStyles: { fillColor: [30, 30, 30], fontStyle: "bold" },
      columnStyles: { 1: { halign: "right" }, 2: { halign: "right" } },
      didParseCell: (data) => { if (data.section === "head" && data.column.index > 0) data.cell.styles.halign = "right"; },
    });

    const afterSummary = (doc as unknown as { lastAutoTable: { finalY: number } }).lastAutoTable.finalY;
    autoTable(doc, {
      startY: afterSummary + 18, margin: { left: margin, right: margin, bottom: 34 },
      head: [["Date", "Category", "Beneficiary / payer", "Phone", "Reference", "Narration", ...(multiple ? ["Statement"] : []), "Amount (INR)"]],
      body: filtered.map((t) => [
        t.date, t.category, t.beneficiary, t.phone, t.reference === "—" ? "" : t.reference, t.narration,
        ...(multiple ? [statements.find((statement) => statement.id === t.statementId)?.name ?? ""] : []), signed(t),
      ]),
      styles: { fontSize: 7, cellPadding: 2.5, overflow: "linebreak", valign: "top" },
      headStyles: { fillColor: [30, 30, 30], fontStyle: "bold" },
      alternateRowStyles: { fillColor: [247, 247, 247] },
      columnStyles: multiple
        ? { 0: { cellWidth: 52 }, 1: { cellWidth: 52 }, 2: { cellWidth: 95 }, 3: { cellWidth: 52 }, 4: { cellWidth: 70 }, 6: { cellWidth: 70 }, 7: { cellWidth: 62, halign: "right" } }
        : { 0: { cellWidth: 52 }, 1: { cellWidth: 52 }, 2: { cellWidth: 100 }, 3: { cellWidth: 52 }, 4: { cellWidth: 74 }, 6: { cellWidth: 70, halign: "right" } },
      didParseCell: (data) => {
        // Colour the amount like the dashboard does: debits red, credits green. Headers stay as set.
        if (data.section === "body" && data.column.index === data.table.columns.length - 1) {
          const text = String(data.cell.raw ?? "");
          data.cell.styles.textColor = text.startsWith("-") ? [156, 52, 44] : text.startsWith("+") ? [31, 112, 64] : [0, 0, 0];
        }
      },
    });

    const pages = doc.getNumberOfPages();
    for (let page = 1; page <= pages; page++) {
      doc.setPage(page); doc.setFontSize(8); doc.setTextColor(120);
      doc.text(`Page ${page} of ${pages}`, pageWidth - margin, doc.internal.pageSize.getHeight() - 16, { align: "right" });
      doc.text("Generated by LedgerLens. Check beneficiary inference against the original narration.", margin, doc.internal.pageSize.getHeight() - 16);
    }
    const label = activeCategory !== "All" ? `${activeCategory.toLowerCase().replace(/\s+/g, "-")}${applied.length > 1 ? "-filtered" : ""}` : applied.length ? "filtered-review" : "category-review";
    doc.save(`ledgerlens-${label}-${stamp}.pdf`);
  };

  const [exportMenuOpen, setExportMenuOpen] = useState(false);
  useEffect(() => {
    if (!exportMenuOpen) return;
    const close = (event: MouseEvent) => { if (!(event.target as HTMLElement).closest(".export-group")) setExportMenuOpen(false); };
    document.addEventListener("click", close);
    return () => document.removeEventListener("click", close);
  }, [exportMenuOpen]);
  const exportAs = (format: "excel" | "pdf") => { setExportMenuOpen(false); void (format === "pdf" ? exportPdf() : exportWorkbook()); };

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
        <div className="hero-pills"><span>PDF</span><span>Server-side OCR</span></div>
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
          <div><strong>{status === "processing" ? "Reading your statements…" : statements.length ? "Drop more statements to add them" : "Drop bank statements here"}</strong><span>or choose one or more PDF statements</span></div>
          <button className="button button-dark" type="button" onClick={() => fileInput.current?.click()} disabled={status === "processing"}>{status === "processing" ? "Analysing" : statements.length ? "Add statements" : "Select statements"}</button>
          <input ref={fileInput} type="file" multiple accept=".pdf,.csv,.xlsx,.xls,application/pdf,text/csv,application/vnd.ms-excel,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" onChange={onInput} />
        </div>
        {statements.length > 0 && <div className="statement-list" aria-label="Uploaded statements">{statements.map((statement) => <span key={statement.id} className={`statement-chip ${statement.status}`} title={statement.message || statement.name}><b>{statement.status === "processing" ? "…" : statement.status === "error" ? "!" : "✓"}</b><span className="statement-name">{statement.name}</span>{statement.status === "ready" && <small>{statement.transactions.length.toLocaleString("en-IN")}</small>}<button type="button" onClick={() => removeStatement(statement.id)} aria-label={`Remove ${statement.name}`}>×</button></span>)}{statements.length > 1 && <button className="statement-clear" type="button" onClick={clearStatements}>Clear all</button>}</div>}
        {overlap && <div className="overlap-note" role="status"><strong>Overlapping statements.</strong> {overlap.periods.length ? overlap.periods.map((period, index) => <span key={index}>{period.a} and {period.b} both cover {formatDay(period.from)}{period.from !== period.to ? ` – ${formatDay(period.to)}` : ""}. </span>) : null}{overlap.duplicates.size ? <>{overlap.duplicates.size.toLocaleString("en-IN")} transaction{overlap.duplicates.size === 1 ? " appears" : "s appear"} in more than one statement and {dropDuplicates ? "the repeats are hidden" : "are counted twice"}. <button type="button" onClick={() => setDropDuplicates((on) => !on)}>{dropDuplicates ? "Show repeats" : `Remove ${overlap.duplicates.size.toLocaleString("en-IN")} repeat${overlap.duplicates.size === 1 ? "" : "s"}`}</button></> : "No repeated transactions were found."}</div>}
        <div className={`privacy-line ${status === "error" ? "error" : ""}`}><span>{status === "error" ? "!" : "✓"}</span>{message || "Statements are sent to the LedgerLens analysis service for extraction. Scanned PDFs are OCR'd automatically."}</div>

        <div className="summary-grid">
          {totals.map((total) => <button key={total.category} className={`summary-card ${categoryClass[total.category]} ${activeCategory === total.category ? "active" : ""}`} onClick={() => selectCategory(activeCategory === total.category ? "All" : total.category)} type="button"><span>{total.category}</span><strong>{total.count.toLocaleString("en-IN")}</strong><small className={total.amount < 0 ? "debit" : "credit"}>{formatNet(total.amount)} net</small></button>)}
        </div>

        <div className="table-card">
          <div className="table-toolbar">
            <div><span className="section-kicker">Categorised activity</span><h3>{fileName ? fileName : "Upload statements to begin"}</h3></div>
            <div className="toolbar-actions"><label className="search"><span>⌕</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="Search name or reference" aria-label="Search transactions" /></label>{multiple && <select className="counterparty-filter" value={activeStatement} onChange={(event) => setActiveStatement(event.target.value)} aria-label="Filter by statement"><option value="All">All statements</option>{readyStatements.map((statement) => <option key={statement.id} value={statement.id}>{statement.name}</option>)}</select>}<select className="counterparty-filter" value={activeCounterparty} onChange={(event) => setActiveCounterparty(event.target.value)} disabled={!counterpartyOptions.length} aria-label="Filter by counterparty"><option value="All">All counterparties</option>{counterpartyOptions.map((option) => <option key={option.name} value={option.name}>{option.name} ({option.count})</option>)}</select><div className="materiality"><button className={`button materiality-toggle ${materialityAmount !== null ? "active" : ""}`} type="button" onClick={() => setMaterialityOpen((open) => !open)} aria-expanded={materialityOpen} aria-haspopup="dialog" disabled={!targetTransactions.length}>Materiality{materialityAmount !== null ? `: ${materialityMode === "above" ? "≥" : "≤"} ${formatAmount(materialityAmount)}` : ""}</button>{materialityOpen && <div className="materiality-panel" role="dialog" aria-label="Materiality filter"><label>Show transactions<select value={materialityMode} onChange={(event) => setMaterialityMode(event.target.value as "above" | "below")}><option value="above">at or above</option><option value="below">at or below</option></select></label><label>Amount (₹)<input inputMode="decimal" value={materialityInput} onChange={(event) => setMaterialityInput(event.target.value)} placeholder="e.g. 50000" autoFocus /></label><div className="materiality-actions"><button className="button" type="button" onClick={() => setMaterialityInput("")} disabled={!materialityInput}>Clear</button><button className="button button-dark" type="button" onClick={() => setMaterialityOpen(false)}>Done</button></div></div>}</div><select className="counterparty-filter phone-filter" value={activePhone} onChange={(event) => setActivePhone(event.target.value)} disabled={!phoneOptions.length} aria-label="Filter by phone number"><option value="All">All phone numbers</option>{phoneOptions.map((option) => <option key={option.phone} value={option.phone}>{option.phone} ({option.count})</option>)}</select><div className="export-group"><button className="button export" type="button" onClick={() => exportAs("excel")} title={filtered.length ? `Export the ${filtered.length.toLocaleString("en-IN")} transaction${filtered.length === 1 ? "" : "s"} currently shown` : "Nothing to export with the current selection"} disabled={!filtered.length}><span>↓</span> Export Excel</button><button className="button export export-caret" type="button" onClick={() => setExportMenuOpen((open) => !open)} aria-haspopup="menu" aria-expanded={exportMenuOpen} aria-label="Choose export format" disabled={!filtered.length}>▾</button>{exportMenuOpen && <div className="export-menu" role="menu"><button type="button" role="menuitem" onClick={() => exportAs("excel")}>Excel</button><button type="button" role="menuitem" onClick={() => exportAs("pdf")}>PDF</button></div>}</div></div>
          </div>
          <div className="filters" aria-label="Transaction category filters"><button className={activeCategory === "All" ? "selected" : ""} onClick={() => selectCategory("All")} type="button">All transactions <b>{targetTransactions.length}</b></button>{totals.map((total) => <button key={total.category} className={activeCategory === total.category ? "selected" : ""} onClick={() => selectCategory(total.category)} type="button">{total.category} <b>{total.count}</b></button>)}</div>
          <div className="table-wrap">
            {filtered.length ? <table><thead><tr><th aria-sort={newestFirst ? "descending" : "ascending"}><button className="sort-button" type="button" onClick={() => setNewestFirst((on) => !on)} title={newestFirst ? "Showing newest first - click for oldest first" : "Showing oldest first - click for newest first"}>Transaction date <span aria-hidden="true">{newestFirst ? "↓" : "↑"}</span></button></th><th>Category</th><th>Beneficiary / payer</th><th>Phone number</th><th>Reference</th><th>Narration</th>{multiple && <th>Statement</th>}<th className="amount">Amount</th></tr></thead><tbody>{filtered.map((transaction) => <tr key={transaction.id}><td className="date-cell">{transaction.date}<small className={directionClass(transaction.direction)}>{transaction.direction}</small></td><td><span className={`tag ${categoryClass[transaction.category]}`}>{transaction.category}</span></td><td className="beneficiary">{transaction.beneficiary}</td><td className="phone-cell">{transaction.phone || "—"}</td><td className="reference">{transaction.reference}</td><td className="narration">{transaction.narration}</td>{multiple && <td className="statement-cell">{statements.find((statement) => statement.id === transaction.statementId)?.name}</td>}<td className={`amount ${directionClass(transaction.direction)}`}>{signedAmount(transaction.direction, transaction.amount)}</td></tr>)}</tbody></table> : <div className="empty-state"><div>⌁</div><strong>{status === "ready" ? "No matching activity" : "Your forensic review starts here"}</strong><p>{status === "ready" ? "Try another category or search phrase." : "Upload statements to extract cash deposits, cash withdrawals, NEFT and UPI transactions."}</p></div>}
          </div>
          <footer className="table-footer"><span>{targetTransactions.length ? `${filtered.length} of ${targetTransactions.length} detected transactions shown` : "No statement loaded"}</span><span>Review beneficiary inference against the original narration before relying on it.</span></footer>
        </div>
      </section>

      <section className="method"><div><span className="section-kicker">Made for the evidence trail</span><h2>Structured for review.<br />Built for speed.</h2></div><div className="method-items"><p><b>01</b>Statement is parsed by the analysis service</p><p><b>02</b>Transaction dates are normalised in Indian date format</p><p><b>03</b>Excel export creates a separate sheet for each category</p></div></section>
      <footer className="site-footer"><span>LedgerLens</span><span>Forensic statement analysis</span><span>Designed for Indian bank statement formats</span></footer>
    </main>
  );
}
