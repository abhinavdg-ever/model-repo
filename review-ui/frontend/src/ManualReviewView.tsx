import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  ExternalLink,
  FileSpreadsheet,
  Info,
  Link2,
  Loader2,
  Search,
  Upload,
  X,
} from "lucide-react";
import {
  streamManualReview,
  downloadManualReviewReport,
  getManualReviewColumns,
  type ManualReviewColumns,
  type ManualReviewResult,
  type ManualReviewRow,
  type ManualReviewStatus,
} from "./api";
import { saveChartOrder } from "./chartSequence";
import { readGroundTruthSource } from "./groundTruthSource";
import { readFile, readSources, writeFile, writeSources, type Source } from "./manualReviewStore";
import { useViewState } from "./viewState";

type Props = {
  onBack: () => void;
  onOpenChart: (folderId: string) => void;
  chartHref: (folderId: string) => string;
};

const ROWS_PER_PAGE = 10;
const ACCEPT = ".xlsx,.xls,.csv";

const STATUS_CLASS: Record<ManualReviewStatus, string> = {
  CORRECT: "status-pill status-completed",
  ERROR: "status-pill status-failed",
  MISSING: "status-pill status-imaging",
  EXTRA: "status-pill status-progress",
};

type Filter = "ALL" | ManualReviewStatus;

/** An input card: its title row (with any controls), then the body. The title row
 * sits outside the drop zone's <label> so its buttons never open the file picker. */
function InputCard({
  title,
  extra,
  children,
}: {
  title: string;
  extra?: ReactNode;
  children: ReactNode;
}) {
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

/** Upload file / Database toggle in a card's title row. */
function SourceToggle({
  label,
  value,
  onChange,
}: {
  label: string;
  value: Source;
  onChange: (source: Source) => void;
}) {
  return (
    <div className="excel-compare-filters" role="radiogroup" aria-label={label}>
      {(
        [
          ["upload", "Upload file", Upload],
          ["db", "Database", Database],
        ] as const
      ).map(([id, text, Icon]) => (
        <button
          key={id}
          type="button"
          role="radio"
          aria-checked={value === id}
          className={`excel-compare-filter${value === id ? " active" : ""}`}
          onClick={() => onChange(id)}
        >
          <Icon size={12} aria-hidden="true" />
          {text}
        </button>
      ))}
    </div>
  );
}

/** The database side of a card: what is read and from where. */
function DatabasePick({ title }: { title: string }) {
  return (
    <div className="excel-compare-pick has-file excel-compare-db">
      <span className="excel-compare-pick-file">
        <Database size={14} aria-hidden="true" />
        {title}
      </span>
      <span className="excel-compare-pick-hint">
        <Link2 size={13} aria-hidden="true" />
        Connected to Local Folder/Database
      </span>
    </div>
  );
}

/** Drop zone: click to choose, or drop a file on it. A chosen file can be
 * replaced the same way, or cleared. */
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
    <div className="excel-compare-pick-wrap">
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
            const chosen = e.target.files?.[0];
            // Cancelling the picker keeps the file already chosen.
            if (chosen) onFile(chosen);
            // Picking the same file again after a change still fires.
            e.target.value = "";
          }}
        />
        {file ? (
          <>
            <span className="excel-compare-pick-file">
              <FileSpreadsheet size={14} aria-hidden="true" />
              {file.name}
            </span>
            <span className="excel-compare-pick-hint">Click or drop to upload a different file</span>
          </>
        ) : (
          <span className="excel-compare-pick-hint">
            <Upload size={14} aria-hidden="true" />
            {hint}
          </span>
        )}
      </label>
      {file ? (
        <button
          type="button"
          className="excel-compare-clear"
          onClick={() => onFile(null)}
          aria-label={`Clear ${file.name}`}
        >
          <X size={13} aria-hidden="true" />
          Clear
        </button>
      ) : null}
    </div>
  );
}

/** ⓘ listing the columns an uploaded file needs, from the backend's own lists. */
function ColumnsInfo() {
  const [open, setOpen] = useState(false);
  const [columns, setColumns] = useState<ManualReviewColumns | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open || columns) return;
    getManualReviewColumns()
      .then(setColumns)
      .catch(() => setFailed(true));
  }, [open, columns]);

  const spellings = (aliases: string[]) =>
    aliases.length ? (
      <span className="excel-compare-aliases">
        {" "}
        — also {aliases.join(", ")}
      </span>
    ) : null;

  return (
    <span className="accuracy-info">
      <button
        type="button"
        className="accuracy-info-btn"
        aria-label="Columns an uploaded file needs"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setOpen(false)}
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {open ? (
        <span
          className="accuracy-info-pop excel-compare-columns-pop"
          role="tooltip"
        >
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
                Headers ignore case, spaces and punctuation. One row per Chart
                + Page Number. CSV, XLSX or XLS.
              </p>
            </>
          )}
        </span>
      ) : null}
    </span>
  );
}

/** "GT: a | Algo: b" -> the two sides; anything else is one agreed value. */
function splitDisplay(display: string): { gt: string; system: string } | null {
  const cut = display.indexOf(" | Algo: ");
  if (!display.startsWith("GT: ") || cut < 0) return null;
  return { gt: display.slice(4, cut), system: display.slice(cut + 9) };
}

/** One side's value. A mismatched cell holds "GT: a | Algo: b"; any other
 * matched cell is the value both agree on. A Missing record has only ground
 * truth and an Extra one only algo output. */
function sideValue(row: ManualReviewRow, field: string, side: "gt" | "algo"): string {
  const display = row.values[field] ?? "";
  if (row.status === "MISSING") return side === "gt" ? display : "";
  if (row.status === "EXTRA") return side === "algo" ? display : "";
  const sides = row.mismatched.includes(field) ? splitDisplay(display) : null;
  if (!sides) return display;
  return side === "gt" ? sides.gt : sides.system;
}

function percent(value: number | undefined): string {
  return value === undefined ? "—" : `${value.toFixed(2)}%`;
}

/** The last comparison, kept while review-ui is open so coming back to the
 * page shows it again. Cleared whenever an input changes. */
let lastResult: ManualReviewResult | null = null;

export default function ManualReviewView({ onBack, onOpenChart, chartHref }: Props) {
  const [ground, setGround] = useState<File | null>(null);
  const [system, setSystem] = useState<File | null>(null);
  const [groundSource, setGroundSource] = useState<Source>(() => readSources().ground);
  const [systemSource, setSystemSource] = useState<Source>(() => readSources().system);
  const [showExempt, setShowExempt] = useState(false);
  const [result, setResultState] = useState<ManualReviewResult | null>(lastResult);

  function setResult(next: ManualReviewResult | null) {
    lastResult = next;
    setResultState(next);
  }

  // Files uploaded earlier, back from the browser's storage.
  useEffect(() => {
    let live = true;
    void Promise.all([readFile("ground"), readFile("system")]).then(([savedGround, savedSystem]) => {
      if (!live) return;
      setGround((current) => current ?? savedGround);
      setSystem((current) => current ?? savedSystem);
    });
    return () => {
      live = false;
    };
  }, []);

  useEffect(() => {
    writeSources({ ground: groundSource, system: systemSource });
  }, [groundSource, systemSource]);
  const [comparing, setComparing] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Status filter, search and page survive opening a chart and coming Back.
  const [view, setView] = useViewState<{ filter: Filter; page: number; query: string }>(
    "manual-review",
    { filter: "ALL", page: 1, query: "" },
  );
  const { filter, page, query } = view;
  const setFilter = (value: Filter) => setView({ filter: value });
  const setPage = (value: number) => setView({ page: value });
  const setQuery = (value: string) => setView({ query: value });
  // Charts read so far while a comparison streams in; null when idle.
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const streamRef = useRef<AbortController | null>(null);

  // Leaving the page stops a comparison still streaming; a partial one is not kept.
  useEffect(
    () => () => {
      if (streamRef.current) {
        streamRef.current.abort();
        lastResult = null;
      }
    },
    [],
  );

  function pick(side: "ground" | "system", setter: (file: File | null) => void) {
    return (file: File | null) => {
      streamRef.current?.abort();
      setter(file);
      void writeFile(side, file);
      // A result belongs to the files it was run on.
      setResult(null);
      setError(null);
    };
  }

  const ready = Boolean(
    (groundSource === "db" || ground) && (systemSource === "db" || system),
  );
  const input = () => ({ ground, groundSource, system, systemSource });

  function chooseSource(setter: (source: Source) => void) {
    return (source: Source) => {
      streamRef.current?.abort();
      setter(source);
      setResult(null);
      setError(null);
    };
  }

  async function runCompare() {
    if (!ready) return;
    streamRef.current?.abort();
    const controller = new AbortController();
    streamRef.current = controller;
    setComparing(true);
    setError(null);
    setFilter("ALL");
    setPage(1);
    // Filled in chart by chart as the backend reads each one.
    let current: ManualReviewResult | null = null;
    try {
      await streamManualReview(
        input(),
        (event) => {
          if (event.type === "start") {
            current = {
              groundCount: 0,
              systemCount: 0,
              correctCount: 0,
              errorCount: 0,
              missingCount: 0,
              extraCount: 0,
              fields: event.fields,
              exemptedColumns: event.exemptedColumns,
              summary: [],
              rows: [],
            };
            setProgress({ loaded: 0, total: event.charts });
          } else if (event.type === "error") {
            setError(`Stopped at chart ${event.pdfId}: ${event.detail}`);
            return;
          } else if (current) {
            const { type: _type, ...rest } = event;
            const rows = event.type === "chart" ? [...current.rows, ...event.rows] : current.rows;
            current = { ...current, ...rest, rows };
            if (event.type === "chart") setProgress((p) => (p ? { ...p, loaded: p.loaded + 1 } : p));
          }
          setResult(current);
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      if (!current) setResult(null);
      setError(err instanceof Error ? err.message : "Comparison failed");
    } finally {
      if (streamRef.current === controller) {
        streamRef.current = null;
        setComparing(false);
        setProgress(null);
      }
    }
  }

  async function runDownload() {
    if (!ready) return;
    setDownloading(true);
    setError(null);
    try {
      const blob = await downloadManualReviewReport(input());
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = "manual_review_report.xlsx";
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not build the report",
      );
    } finally {
      setDownloading(false);
    }
  }

  const rate = (metric: string) =>
    result?.summary.find((row) => row.metric === metric)?.percentage;

  // Search by chart name, as on the Accuracy tab; the status pills count what it leaves.
  const searched = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = result?.rows ?? [];
    return needle ? rows.filter((row) => row.pdfId.toLowerCase().includes(needle)) : rows;
  }, [result, query]);
  const filtered = useMemo(
    () => (filter === "ALL" ? searched : searched.filter((row) => row.status === filter)),
    [searched, filter],
  );
  // Next on a chart opened from here walks the charts in this filtered order.
  const chartOrder = useMemo(
    () => [...new Set(filtered.map((row) => row.pdfId))].map((id) => ({ id, name: id })),
    [filtered],
  );
  const totalPages = Math.max(1, Math.ceil(filtered.length / ROWS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = filtered.slice(
    (currentPage - 1) * ROWS_PER_PAGE,
    currentPage * ROWS_PER_PAGE,
  );

  // Exempted columns grouped by why.
  const exempted = useMemo(() => {
    const byReason = new Map<string, string[]>();
    for (const entry of result?.exemptedColumns ?? []) {
      byReason.set(entry.reason, [
        ...(byReason.get(entry.reason) ?? []),
        entry.column,
      ]);
    }
    return [...byReason.entries()];
  }, [result]);

  const statusCount = (status: ManualReviewStatus) =>
    searched.filter((row) => row.status === status).length;
  const filters: { id: Filter; label: string; count: number }[] = result
    ? [
        { id: "ALL", label: "All", count: searched.length },
        { id: "ERROR", label: "Errors", count: statusCount("ERROR") },
        { id: "MISSING", label: "Missing", count: statusCount("MISSING") },
        { id: "EXTRA", label: "Extra", count: statusCount("EXTRA") },
        { id: "CORRECT", label: "Correct", count: statusCount("CORRECT") },
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
            <h1>Manual Review</h1>
          </div>
        </div>
      </div>

      <section className="accuracy-section" aria-label="Input files">
        <h2>Input files</h2>
        <p className="accuracy-note">
          Ground truth against the algo output, matched on Chart and Page
          Number. Nothing is stored.
        </p>
        <div className="excel-compare-picks">
          <InputCard
            title="Ground Truth"
            extra={
              <div className="excel-compare-card-tools">
                <SourceToggle
                  label="Ground truth source"
                  value={groundSource}
                  onChange={chooseSource(setGroundSource)}
                />
                <ColumnsInfo />
              </div>
            }
          >
            {groundSource === "upload" ? (
              <FilePick
                hint="Manual QA file (CSV or Excel) — click or drop"
                file={ground}
                onFile={pick("ground", setGround)}
              />
            ) : (
              <DatabasePick
                title={
                  readGroundTruthSource() === "bulk_upload"
                    ? "Ground Truth — latest bulk upload per page"
                    : "Ground Truth — latest per page"
                }
              />
            )}
          </InputCard>
          <InputCard
            title="Algo Generated Output"
            extra={
              <SourceToggle
                label="Algo output source"
                value={systemSource}
                onChange={chooseSource(setSystemSource)}
              />
            }
          >
            {systemSource === "upload" ? (
              <FilePick
                hint="Algo-generated file (CSV or Excel) — click or drop"
                file={system}
                onFile={pick("system", setSystem)}
              />
            ) : (
              <DatabasePick title="Pipeline Output" />
            )}
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
              <Loader2
                size={15}
                className="accuracy-calculating-spin"
                aria-hidden="true"
              />
            ) : (
              <FileSpreadsheet size={15} aria-hidden="true" />
            )}
            {comparing
              ? progress && progress.total > 0
                ? `Comparing… ${progress.loaded} of ${progress.total} charts`
                : "Comparing…"
              : "Compare Files"}
          </button>
          {result && !comparing ? (
            <button
              type="button"
              className="landing-file-viewer-btn"
              disabled={downloading}
              onClick={runDownload}
            >
              {downloading ? (
                <Loader2
                  size={15}
                  className="accuracy-calculating-spin"
                  aria-hidden="true"
                />
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
                {
                  label: "Records",
                  items: [
                    ["ground truth", result.groundCount],
                    ["algo generated", result.systemCount],
                    ["matched", result.correctCount + result.errorCount],
                  ],
                },
                {
                  label: "Outcome",
                  items: [
                    ["exact matches", result.correctCount],
                    ["field errors", result.errorCount],
                    ["missing", result.missingCount],
                    ["extra", result.extraCount],
                  ],
                },
                {
                  label: "Rates",
                  items: [
                    ["exact match rate", percent(rate("Match Percentage"))],
                    [
                      "discrepancy rate",
                      percent(rate("Overall Disparity Rate")),
                    ],
                  ],
                },
              ].map((group) => (
                <div key={group.label} className="excel-compare-stat-group">
                  <span className="excel-compare-stat-group-label">
                    {group.label}
                  </span>
                  <div className="landing-stats">
                    {group.items.map(([label, value], index) => (
                      <span
                        key={String(label)}
                        className="excel-compare-stat-item"
                      >
                        {index > 0 ? (
                          <span className="landing-stat-divider" />
                        ) : null}
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
                <button
                  type="button"
                  className="excel-compare-exempt-toggle"
                  aria-expanded={showExempt}
                  onClick={() => setShowExempt((v) => !v)}
                >
                  <ChevronDown
                    size={14}
                    aria-hidden="true"
                    className={showExempt ? "is-open" : undefined}
                  />
                  {result.exemptedColumns.length} column
                  {result.exemptedColumns.length === 1 ? "" : "s"} not compared
                </button>
                {showExempt ? (
                  <ul>
                    {exempted.map(([reason, columns]) => (
                      <li key={reason}>
                        <span>{reason}</span> {columns.join(", ")}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            ) : null}
          </section>

          <section className="accuracy-section" aria-label="Comparison results">
            <div className="accuracy-chart-head">
              <h2>Results</h2>
              <div
                className="excel-compare-filters excel-compare-status-filters"
                role="radiogroup"
                aria-label="Filter by status"
              >
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
              <label className="landing-search accuracy-search">
                <Search size={14} aria-hidden="true" />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => {
                    setQuery(e.target.value);
                    setPage(1);
                  }}
                  placeholder="Search file / chart…"
                  aria-label="Search charts"
                />
              </label>
            </div>
            <div className="accuracy-table-scroll">
              <table className="landing-history-table accuracy-table excel-compare-table">
                <thead>
                  <tr>
                    <th scope="col">Chart</th>
                    <th scope="col">Page</th>
                    <th scope="col">Source</th>
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
                      <td
                        colSpan={result.fields.length + 5}
                        className="accuracy-empty"
                      >
                        No records match.
                      </td>
                    </tr>
                  ) : null}
                  {pageRows.map((row, index) => {
                    const key = `${row.pdfId}:${row.pageNumber}:${row.status}:${index}`;
                    // A record with nothing to tell apart is one row: every value agrees.
                    const sides = row.status === "CORRECT" ? (["gt"] as const) : (["gt", "algo"] as const);
                    const span = sides.length > 1 ? 2 : undefined;
                    return sides.map((side) => (
                      <tr
                        key={`${key}:${side}`}
                        className={`excel-compare-row-${side}`}
                      >
                        {side === "gt" ? (
                          <>
                            <th scope="rowgroup" rowSpan={span}>
                              <a
                                className="accuracy-chart-link"
                                href={chartHref(row.pdfId)}
                                onClick={(event) => {
                                  // Cmd / Ctrl / Shift / middle click keep the browser's new-tab behaviour.
                                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                                  event.preventDefault();
                                  saveChartOrder("manual-review", chartOrder);
                                  onOpenChart(row.pdfId);
                                }}
                              >
                                {row.pdfId}
                              </a>
                              <button
                                type="button"
                                className="accuracy-chart-newtab"
                                title="Open in new tab"
                                aria-label={`Open ${row.pdfId} in a new tab`}
                                // window.open (not a target=_blank link) so the new tab keeps this tab's sign-in.
                                onClick={() => {
                                  saveChartOrder("manual-review", chartOrder);
                                  window.open(chartHref(row.pdfId), "_blank");
                                }}
                              >
                                <ExternalLink size={12} aria-hidden="true" />
                              </button>
                            </th>
                            <td rowSpan={span}>{row.pageNumber}</td>
                          </>
                        ) : null}
                        <td className="excel-compare-source">
                          {span ? (side === "gt" ? "GT" : "Algo") : "GT = Algo"}
                        </td>
                        {result.fields.map((field) => {
                          const mismatched = row.mismatched.includes(field);
                          // A value both sides agree on is shown once, across both rows.
                          const agreed = !mismatched && row.status === "ERROR";
                          if (agreed && side === "algo") return null;
                          return (
                            <td
                              key={field}
                              rowSpan={agreed ? 2 : undefined}
                              className={mismatched ? "excel-compare-mismatch" : undefined}
                            >
                              {sideValue(row, field, side) || "—"}
                            </td>
                          );
                        })}
                        {side === "gt" ? (
                          <>
                            <td rowSpan={span}>
                              <span className={STATUS_CLASS[row.status]}>
                                {row.status}
                              </span>
                            </td>
                            <td rowSpan={span} className="excel-compare-reason">
                              {row.reason}
                            </td>
                          </>
                        ) : null}
                      </tr>
                    ));
                  })}
                </tbody>
              </table>
            </div>
            {filtered.length > ROWS_PER_PAGE ? (
              <div
                className="landing-pagination"
                aria-label="Results pagination"
              >
                <span className="landing-pagination-meta">
                  {(currentPage - 1) * ROWS_PER_PAGE + 1}–
                  {Math.min(currentPage * ROWS_PER_PAGE, filtered.length)} of{" "}
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
