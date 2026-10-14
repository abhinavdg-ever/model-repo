import { useEffect, useMemo, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  FileSpreadsheet,
  Info,
  Loader2,
  Upload,
} from "lucide-react";
import {
  compareExcel,
  downloadExcelCompareReport,
  getExcelCompareColumns,
  type ExcelCompareColumns,
  type ExcelCompareResult,
  type ExcelCompareStatus,
} from "./api";
import { readGroundTruthSource } from "./groundTruthSource";

type Props = {
  onBack: () => void;
};

const ROWS_PER_PAGE = 50;
const ACCEPT = ".xlsx,.xls,.csv";

const STATUS_CLASS: Record<ExcelCompareStatus, string> = {
  CORRECT: "status-pill status-completed",
  ERROR: "status-pill status-failed",
  MISSING: "status-pill status-imaging",
  EXTRA: "status-pill status-progress",
};

type Filter = "ALL" | ExcelCompareStatus;
type GroundSource = "upload" | "db";

/** An input card: its title row (with any controls), then the body. The title row
 * sits outside the drop zone's <label> so its buttons never open the file picker. */
function InputCard({ title, extra, children }: { title: string; extra?: ReactNode; children: ReactNode }) {
  return (
    <div className="excel-compare-card">
      <div className="excel-compare-card-head">
        <span className="excel-compare-pick-title">{title}</span>
        {extra}
      </div>
      {children}
    </div>
  );
}

/** Drop zone: click to choose, or drop a file on it. */
function FilePick({
  hint,
  file,
  onFile,
}: {
  hint: string;
  file: File | null;
  onFile: (file: File | null) => void;
}) {
  const [over, setOver] = useState(false);
  return (
    <label
      className={`excel-compare-pick${over ? " is-over" : ""}${file ? " has-file" : ""}`}
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={(e) => {
        e.preventDefault();
        setOver(false);
        onFile(e.dataTransfer.files?.[0] ?? null);
      }}
    >
      <input
        type="file"
        accept={ACCEPT}
        onChange={(e) => {
          onFile(e.target.files?.[0] ?? null);
          // Picking the same file again after a change still fires.
          e.target.value = "";
        }}
      />
      {file ? (
        <span className="excel-compare-pick-file">
          <FileSpreadsheet size={14} aria-hidden="true" />
          {file.name}
        </span>
      ) : (
        <span className="excel-compare-pick-hint">
          <Upload size={14} aria-hidden="true" />
          {hint}
        </span>
      )}
    </label>
  );
}

/** ⓘ listing the columns a System Generated file needs, from the backend's own lists. */
function ColumnsInfo() {
  const [open, setOpen] = useState(false);
  const [columns, setColumns] = useState<ExcelCompareColumns | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open || columns) return;
    getExcelCompareColumns()
      .then(setColumns)
      .catch(() => setFailed(true));
  }, [open, columns]);

  const spellings = (aliases: string[]) =>
    aliases.length ? <span className="excel-compare-aliases"> — also {aliases.join(", ")}</span> : null;

  return (
    <span className="accuracy-info">
      <button
        type="button"
        className="accuracy-info-btn"
        aria-label="Columns the system file needs"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setOpen(false)}
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {open ? (
        <span className="accuracy-info-pop excel-compare-columns-pop" role="tooltip">
          {failed ? (
            "Could not load the column list."
          ) : !columns ? (
            "Loading…"
          ) : (
            <>
              <strong>Required</strong>
              <ul>
                {columns.required.map((c) => (
                  <li key={c.column}>
                    <b>{c.column}</b>
                    {spellings(c.aliases)}
                  </li>
                ))}
              </ul>
              <strong>Compared when the column is in both files</strong>
              <ul>
                {columns.compared.map((c) => (
                  <li key={c.column}>
                    <b>{c.column}</b>
                    {spellings(c.aliases)}
                  </li>
                ))}
              </ul>
              <strong>Never compared</strong>
              <p>{columns.exempt.join(", ")}</p>
              <p className="excel-compare-columns-note">
                Headers ignore case, spaces and punctuation. One row per PDF ID + Page Number. CSV, XLSX or XLS.
              </p>
            </>
          )}
        </span>
      ) : null}
    </span>
  );
}

/** "GT: a | System: b" -> the two sides; anything else is one agreed value. */
function splitDisplay(display: string): { gt: string; system: string } | null {
  const cut = display.indexOf(" | System: ");
  if (!display.startsWith("GT: ") || cut < 0) return null;
  return { gt: display.slice(4, cut), system: display.slice(cut + 11) };
}

function percent(value: number | undefined): string {
  return value === undefined ? "—" : `${value.toFixed(2)}%`;
}

export default function ExcelCompareView({ onBack }: Props) {
  const [ground, setGround] = useState<File | null>(null);
  const [system, setSystem] = useState<File | null>(null);
  const [groundSource, setGroundSource] = useState<GroundSource>("upload");
  const [result, setResult] = useState<ExcelCompareResult | null>(null);
  const [comparing, setComparing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<Filter>("ALL");
  const [page, setPage] = useState(1);

  function pick(setter: (file: File | null) => void) {
    return (file: File | null) => {
      setter(file);
      // A result belongs to the files it was run on.
      setResult(null);
      setError(null);
    };
  }

  const ready = Boolean(system && (groundSource === "db" || ground));
  const input = () => ({ system: system as File, ground, groundSource });

  async function runCompare() {
    if (!ready) return;
    setComparing(true);
    setError(null);
    try {
      setResult(await compareExcel(input()));
      setFilter("ALL");
      setPage(1);
    } catch (err) {
      setResult(null);
      setError(err instanceof Error ? err.message : "Comparison failed");
    } finally {
      setComparing(false);
    }
  }

  async function runDownload() {
    if (!ready) return;
    setDownloading(true);
    setError(null);
    try {
      const blob = await downloadExcelCompareReport(input());
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "comparison_report.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not build the report");
    } finally {
      setDownloading(false);
    }
  }

  const rate = (metric: string) => result?.summary.find((row) => row.metric === metric)?.percentage;

  const filtered = useMemo(
    () => (result ? (filter === "ALL" ? result.rows : result.rows.filter((row) => row.status === filter)) : []),
    [result, filter],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / ROWS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = filtered.slice((currentPage - 1) * ROWS_PER_PAGE, currentPage * ROWS_PER_PAGE);

  // Exempted columns grouped by why.
  const exempted = useMemo(() => {
    const byReason = new Map<string, string[]>();
    for (const entry of result?.exemptedColumns ?? []) {
      byReason.set(entry.reason, [...(byReason.get(entry.reason) ?? []), entry.column]);
    }
    return [...byReason.entries()];
  }, [result]);

  const filters: { id: Filter; label: string; count: number }[] = result
    ? [
        { id: "ALL", label: "All", count: result.rows.length },
        { id: "ERROR", label: "Errors", count: result.errorCount },
        { id: "MISSING", label: "Missing", count: result.missingCount },
        { id: "EXTRA", label: "Extra", count: result.extraCount },
        { id: "CORRECT", label: "Correct", count: result.correctCount },
      ]
    : [];

  return (
    <div className="workspace accuracy-view excel-compare-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Excel Compare</h1>
          </div>
        </div>
      </div>

      <section className="accuracy-section" aria-label="Input files">
        <h2>Input files</h2>
        <p className="accuracy-note">
          Ground truth against system-generated output, matched on PDF ID and Page Number. Nothing is stored.
        </p>
        <div className="excel-compare-picks">
          <InputCard
            title="Ground Truth"
            extra={
              <div className="excel-compare-filters" role="radiogroup" aria-label="Ground truth source">
                {(
                  [
                    ["upload", "Upload file", Upload],
                    ["db", "Pull from DB", Database],
                  ] as const
                ).map(([id, label, Icon]) => (
                  <button
                    key={id}
                    type="button"
                    role="radio"
                    aria-checked={groundSource === id}
                    className={`excel-compare-filter${groundSource === id ? " active" : ""}`}
                    onClick={() => {
                      setGroundSource(id);
                      setResult(null);
                      setError(null);
                    }}
                  >
                    <Icon size={12} aria-hidden="true" />
                    {label}
                  </button>
                ))}
              </div>
            }
          >
            {groundSource === "upload" ? (
              <FilePick hint="Manual QA file (CSV or Excel) — click or drop" file={ground} onFile={pick(setGround)} />
            ) : (
              <div className="excel-compare-pick has-file excel-compare-db">
                <span className="excel-compare-pick-file">
                  <Database size={14} aria-hidden="true" />
                  {readGroundTruthSource() === "bulk_upload"
                    ? "Latest bulk upload per page (manual edits hidden)"
                    : "Latest ground truth per page"}
                </span>
                <span className="excel-compare-pick-hint">
                  For the PDF IDs in the system file — PDF ID must be the chart name. Compares DOS, page type,
                  codability, blank / junk and duplicate. Manual edits follow the user menu setting.
                </span>
              </div>
            )}
          </InputCard>
          <InputCard title="System Generated Output" extra={<ColumnsInfo />}>
            <FilePick hint="System-generated file (CSV or Excel) — click or drop" file={system} onFile={pick(setSystem)} />
          </InputCard>
        </div>
        <div className="excel-compare-actions">
          <button
            type="button"
            className="landing-file-viewer-btn"
            disabled={!ready || comparing}
            onClick={runCompare}
          >
            {comparing ? (
              <Loader2 size={15} className="accuracy-calculating-spin" aria-hidden="true" />
            ) : (
              <FileSpreadsheet size={15} aria-hidden="true" />
            )}
            {comparing ? "Comparing…" : "Compare Files"}
          </button>
          {result ? (
            <button
              type="button"
              className="landing-file-viewer-btn"
              disabled={downloading}
              onClick={runDownload}
            >
              {downloading ? (
                <Loader2 size={15} className="accuracy-calculating-spin" aria-hidden="true" />
              ) : (
                <Download size={15} aria-hidden="true" />
              )}
              Download Excel Report
            </button>
          ) : null}
        </div>
      </section>

      {error ? <div className="error-banner">{error}</div> : null}

      {result ? (
        <>
          <section className="accuracy-section" aria-label="Comparison summary">
            <h2>Summary</h2>
            <div className="excel-compare-stats">
              {[
                { label: "Records", items: [
                  ["ground truth", result.groundCount],
                  ["system generated", result.systemCount],
                  ["matched", result.correctCount + result.errorCount],
                ] },
                { label: "Outcome", items: [
                  ["exact matches", result.correctCount],
                  ["field errors", result.errorCount],
                  ["missing", result.missingCount],
                  ["extra", result.extraCount],
                ] },
                { label: "Rates", items: [
                  ["exact match rate", percent(rate("Match Percentage"))],
                  ["discrepancy rate", percent(rate("Overall Disparity Rate"))],
                ] },
              ].map((group) => (
                <div key={group.label} className="excel-compare-stat-group">
                  <span className="excel-compare-stat-group-label">{group.label}</span>
                  <div className="landing-stats">
                    {group.items.map(([label, value], index) => (
                      <span key={String(label)} className="excel-compare-stat-item">
                        {index > 0 ? <span className="landing-stat-divider" /> : null}
                        <span className="landing-stat">
                          <span className="landing-stat-value">{value}</span>
                          <span className="landing-stat-label">{label}</span>
                        </span>
                      </span>
                    ))}
                  </div>
                </div>
              ))}
            </div>
            {exempted.length > 0 ? (
              <div className="excel-compare-exempt">
                <strong>
                  {result.exemptedColumns.length} column{result.exemptedColumns.length === 1 ? "" : "s"} not compared
                </strong>
                <ul>
                  {exempted.map(([reason, columns]) => (
                    <li key={reason}>
                      <span>{reason}</span> {columns.join(", ")}
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </section>

          <section className="accuracy-section" aria-label="Comparison results">
            <div className="accuracy-chart-head">
              <h2>Results</h2>
              <div className="excel-compare-filters" role="radiogroup" aria-label="Filter by status">
                {filters.map((f) => (
                  <button
                    key={f.id}
                    type="button"
                    role="radio"
                    aria-checked={filter === f.id}
                    className={`excel-compare-filter${filter === f.id ? " active" : ""}`}
                    onClick={() => {
                      setFilter(f.id);
                      setPage(1);
                    }}
                  >
                    {f.label} <span>{f.count}</span>
                  </button>
                ))}
              </div>
            </div>
            <div className="accuracy-table-scroll">
              <table className="landing-history-table accuracy-table excel-compare-table">
                <thead>
                  <tr>
                    <th scope="col">PDF ID</th>
                    <th scope="col">Page</th>
                    {result.fields.map((field) => (
                      <th key={field} scope="col">
                        {field}
                      </th>
                    ))}
                    <th scope="col">Status</th>
                    <th scope="col">Reason</th>
                  </tr>
                </thead>
                <tbody>
                  {pageRows.length === 0 ? (
                    <tr>
                      <td colSpan={result.fields.length + 4} className="accuracy-empty">
                        No records with this status.
                      </td>
                    </tr>
                  ) : null}
                  {pageRows.map((row, index) => (
                    <tr key={`${row.pdfId}:${row.pageNumber}:${row.status}:${index}`}>
                      <th scope="row">{row.pdfId}</th>
                      <td>{row.pageNumber}</td>
                      {result.fields.map((field) => {
                        const display = row.values[field] ?? "";
                        const sides = row.mismatched.includes(field) ? splitDisplay(display) : null;
                        return (
                          <td key={field} className={sides ? "excel-compare-mismatch" : undefined}>
                            {sides ? (
                              <>
                                <span>
                                  <em>GT</em> {sides.gt || "—"}
                                </span>
                                <span>
                                  <em>System</em> {sides.system || "—"}
                                </span>
                              </>
                            ) : (
                              display || "—"
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <span className={STATUS_CLASS[row.status]}>{row.status}</span>
                      </td>
                      <td className="excel-compare-reason">{row.reason}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {filtered.length > ROWS_PER_PAGE ? (
              <div className="landing-pagination" aria-label="Results pagination">
                <span className="landing-pagination-meta">
                  {(currentPage - 1) * ROWS_PER_PAGE + 1}–{Math.min(currentPage * ROWS_PER_PAGE, filtered.length)} of{" "}
                  {filtered.length}
                </span>
                <div className="landing-pagination-controls">
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage <= 1}
                    onClick={() => setPage(currentPage - 1)}
                    aria-label="Previous page"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <span className="landing-pagination-meta">
                    Page {currentPage} of {totalPages}
                  </span>
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage >= totalPages}
                    onClick={() => setPage(currentPage + 1)}
                    aria-label="Next page"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            ) : null}
          </section>
        </>
      ) : null}
    </div>
  );
}
