import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  FolderOpen,
  Inbox,
  RefreshCw,
  ScanSearch,
  Search,
} from "lucide-react";
import {
  formatBatchLabel,
  formatRunLabel,
  imagingExportCsvUrl,
  listFolders,
  OCR_STATUS_LABELS,
  type FolderSummary,
  type OcrRunStatus,
} from "./api";

const PAGE_SIZE = 15;
const LANDING_FILTERS_KEY = "advantmed_imaging_landing_filters";

type SortKey = "filename" | "pages" | "updated";
type SortDir = "asc" | "desc";

type LandingFilters = {
  query: string;
  statusFilter: "ALL" | OcrRunStatus;
  runFilter: string[];
  batchFilter: string[];
  sortKey: SortKey;
  sortDir: SortDir;
  page: number;
};

const STATUS_VALUES = new Set<string>([
  "ALL",
  "QUEUED",
  "IN_PROGRESS",
  "COMPLETED",
  "IMAGING_IN_PROGRESS",
  "IMAGING_COMPLETED",
  "FAILED",
]);

function normalizeIdList(raw: unknown): string[] {
  if (Array.isArray(raw)) {
    return raw
      .filter((v): v is string => typeof v === "string")
      .map((v) => v.trim())
      .filter((v) => v && v !== "ALL");
  }
  if (typeof raw === "string") {
    const s = raw.trim();
    if (!s || s === "ALL") return [];
    return [s];
  }
  return [];
}

function readLandingFilters(): LandingFilters {
  const defaults: LandingFilters = {
    query: "",
    statusFilter: "ALL",
    runFilter: [],
    batchFilter: [],
    sortKey: "filename",
    sortDir: "asc",
    page: 1,
  };
  try {
    const raw = sessionStorage.getItem(LANDING_FILTERS_KEY);
    if (!raw) return defaults;
    const parsed = JSON.parse(raw) as Partial<LandingFilters>;
    const statusFilter =
      typeof parsed.statusFilter === "string" && STATUS_VALUES.has(parsed.statusFilter)
        ? (parsed.statusFilter as LandingFilters["statusFilter"])
        : defaults.statusFilter;
    const sortKey =
      parsed.sortKey === "filename" || parsed.sortKey === "pages" || parsed.sortKey === "updated"
        ? parsed.sortKey
        : defaults.sortKey;
    const sortDir = parsed.sortDir === "desc" ? "desc" : "asc";
    const page =
      typeof parsed.page === "number" && parsed.page >= 1
        ? Math.floor(parsed.page)
        : 1;
    return {
      query: typeof parsed.query === "string" ? parsed.query : "",
      statusFilter,
      runFilter: normalizeIdList(parsed.runFilter),
      batchFilter: normalizeIdList(parsed.batchFilter),
      sortKey,
      sortDir,
      page,
    };
  } catch {
    return defaults;
  }
}

function toggleId(list: string[], value: string): string[] {
  return list.includes(value)
    ? list.filter((v) => v !== value)
    : [...list, value].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
}

function MultiCheckFilter({
  label,
  ariaLabel,
  options,
  selected,
  onChange,
  formatOption,
  emptyLabel,
}: {
  label: string;
  ariaLabel: string;
  options: string[];
  selected: string[];
  onChange: (next: string[]) => void;
  formatOption: (value: string) => string;
  emptyLabel: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const summary =
    selected.length === 0
      ? emptyLabel
      : selected.length === 1
        ? formatOption(selected[0])
        : `${selected.length} selected`;

  return (
    <div className="landing-select-wrap landing-multi-wrap" ref={rootRef}>
      <span>{label}</span>
      <div className="landing-multi">
        <button
          type="button"
          className="landing-multi-trigger"
          aria-label={ariaLabel}
          aria-expanded={open}
          aria-haspopup="listbox"
          onClick={() => setOpen((v) => !v)}
        >
          <span className="landing-multi-trigger-label">{summary}</span>
          <ChevronDown size={12} aria-hidden="true" />
        </button>
        {open ? (
          <div className="landing-multi-panel" role="listbox" aria-multiselectable="true">
            <label className="landing-multi-option">
              <input
                type="checkbox"
                checked={selected.length === 0}
                onChange={() => onChange([])}
              />
              <span>{emptyLabel}</span>
            </label>
            {options.length === 0 ? (
              <div className="landing-multi-empty">No values yet</div>
            ) : (
              options.map((value) => (
                <label key={value} className="landing-multi-option">
                  <input
                    type="checkbox"
                    checked={selected.includes(value)}
                    onChange={() => onChange(toggleId(selected, value))}
                  />
                  <span>{formatOption(value)}</span>
                </label>
              ))
            )}
          </div>
        ) : null}
      </div>
    </div>
  );
}

type Props = {
  onView: (folderId: string, mode?: "ocr" | "imaging") => void;
  onOpenFileViewer?: () => void;
};

function fmtUpdated(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return new Intl.DateTimeFormat("en-IN", {
    timeZone: "Asia/Kolkata",
    day: "2-digit",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: true,
  }).format(d);
}

function statusClass(status: OcrRunStatus): string {
  switch (status) {
    case "COMPLETED":
    case "IMAGING_COMPLETED":
      return "status-pill status-completed";
    case "IMAGING_IN_PROGRESS":
      return "status-pill status-imaging";
    case "IN_PROGRESS":
      return "status-pill status-progress";
    case "FAILED":
      return "status-pill status-failed";
    default:
      return "status-pill status-queued";
  }
}

function compareFolders(a: FolderSummary, b: FolderSummary, key: SortKey, dir: SortDir): number {
  const sign = dir === "asc" ? 1 : -1;
  if (key === "filename") {
    return a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) * sign;
  }
  if (key === "pages") {
    if (a.page_count !== b.page_count) return (a.page_count - b.page_count) * sign;
    return a.name.localeCompare(b.name) * sign;
  }
  const at = a.last_updated_at ? Date.parse(a.last_updated_at) : 0;
  const bt = b.last_updated_at ? Date.parse(b.last_updated_at) : 0;
  if (at !== bt) return (at - bt) * sign;
  return a.name.localeCompare(b.name) * sign;
}

export default function LandingPage({ onView, onOpenFileViewer }: Props) {
  const saved = useMemo(() => readLandingFilters(), []);
  const [folders, setFolders] = useState<FolderSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(saved.page);
  const [query, setQuery] = useState(saved.query);
  const [sortKey, setSortKey] = useState<SortKey>(saved.sortKey);
  const [sortDir, setSortDir] = useState<SortDir>(saved.sortDir);
  const [statusFilter, setStatusFilter] = useState<"ALL" | OcrRunStatus>(
    saved.statusFilter,
  );
  const [runFilter, setRunFilter] = useState<string[]>(saved.runFilter);
  const [batchFilter, setBatchFilter] = useState<string[]>(saved.batchFilter);
  const skipFilterPageReset = useRef(true);

  async function load() {
    setLoading(true);
    setError(null);
    try {
      const data = await listFolders();
      setFolders(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load history");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void load();
  }, []);

  useEffect(() => {
    try {
      const payload: LandingFilters = {
        query,
        statusFilter,
        runFilter,
        batchFilter,
        sortKey,
        sortDir,
        page,
      };
      sessionStorage.setItem(LANDING_FILTERS_KEY, JSON.stringify(payload));
    } catch {
      /* ignore */
    }
  }, [query, statusFilter, runFilter, batchFilter, sortKey, sortDir, page]);

  const runOptions = useMemo(() => {
    const values = new Set<string>();
    for (const f of folders) {
      if (f.run_id) values.add(f.run_id);
    }
    return [...values].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }, [folders]);

  const batchOptions = useMemo(() => {
    const values = new Set<string>();
    for (const f of folders) {
      if (f.batch_id) values.add(f.batch_id);
    }
    return [...values].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
  }, [folders]);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    let rows = folders;
    if (q) {
      rows = rows.filter((f) => f.name.toLowerCase().includes(q));
    }
    if (statusFilter !== "ALL") {
      rows = rows.filter((f) => f.ocr_status === statusFilter);
    }
    if (runFilter.length > 0) {
      const allowed = new Set(runFilter);
      rows = rows.filter((f) => f.run_id != null && allowed.has(f.run_id));
    }
    if (batchFilter.length > 0) {
      const allowed = new Set(batchFilter);
      rows = rows.filter((f) => f.batch_id != null && allowed.has(f.batch_id));
    }
    return [...rows].sort((a, b) => compareFolders(a, b, sortKey, sortDir));
  }, [folders, query, sortKey, sortDir, statusFilter, runFilter, batchFilter]);

  const totals = useMemo(() => {
    const pages = filtered.reduce((sum, f) => sum + f.page_count, 0);
    const ocr = filtered.reduce((sum, f) => sum + f.ocr_processed, 0);
    return { folders: filtered.length, pages, ocr };
  }, [filtered]);

  const totalPages = Math.max(1, Math.ceil(filtered.length / PAGE_SIZE));

  useEffect(() => {
    if (skipFilterPageReset.current) {
      skipFilterPageReset.current = false;
      return;
    }
    setPage(1);
  }, [query, sortKey, sortDir, statusFilter, runFilter, batchFilter]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const pageFolders = useMemo(() => {
    const start = (page - 1) * PAGE_SIZE;
    return filtered.slice(start, start + PAGE_SIZE);
  }, [filtered, page]);

  const rangeStart = filtered.length === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const rangeEnd = Math.min(page * PAGE_SIZE, filtered.length);

  const pageNumbers = useMemo(() => {
    const maxButtons = 7;
    if (totalPages <= maxButtons) {
      return Array.from({ length: totalPages }, (_, i) => i + 1);
    }
    const pages = new Set<number>([1, totalPages, page]);
    for (let d = 1; pages.size < maxButtons - 1; d++) {
      if (page - d >= 1) pages.add(page - d);
      if (page + d <= totalPages) pages.add(page + d);
    }
    return [...pages].sort((a, b) => a - b);
  }, [page, totalPages]);

  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "filename" ? "asc" : "desc");
    }
  }

  function sortIndicator(key: SortKey): string {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  }

  function downloadImagingCsv() {
    const url = imagingExportCsvUrl({
      status: statusFilter,
      q: query,
    });
    const a = document.createElement("a");
    a.href = url;
    a.download = "imaging_export.csv";
    a.rel = "noopener";
    document.body.appendChild(a);
    a.click();
    a.remove();
  }

  return (
    <div className="landing">
      <div className="landing-home">
        <div className="landing-intro">
          <div>
            <h1>History</h1>
            <p>Browse processed folders, and view OCR and Imaging Pipeline Results.</p>
          </div>
          <div className="landing-intro-actions">
            <button
              type="button"
              className="landing-file-viewer-btn"
              onClick={downloadImagingCsv}
              disabled={folders.length === 0 || loading}
              title={
                statusFilter !== "ALL" || query.trim()
                  ? "Download imaging CSV for folders matching current search/status"
                  : "Download all imaging pipeline outputs as CSV"
              }
            >
              <Download size={15} aria-hidden="true" />
              Download Imaging CSV
            </button>
            {onOpenFileViewer ? (
              <button
                type="button"
                className="landing-file-viewer-btn"
                onClick={onOpenFileViewer}
              >
                <FolderOpen size={15} aria-hidden="true" />
                File Viewer
              </button>
            ) : null}
          </div>
        </div>

        {folders.length > 0 && (
          <div className="landing-stats" aria-label="Summary">
            <div className="landing-stat">
              <span className="landing-stat-value">{totals.folders}</span>
              <span className="landing-stat-label">folders</span>
            </div>
            <div className="landing-stat-divider" />
            <div className="landing-stat">
              <span className="landing-stat-value">{totals.pages}</span>
              <span className="landing-stat-label">pages</span>
            </div>
            <div className="landing-stat-divider" />
            <div className="landing-stat">
              <span className="landing-stat-value">{totals.ocr}</span>
              <span className="landing-stat-label">OCR processed</span>
            </div>
          </div>
        )}

        {error && <div className="error-banner">{error}</div>}

        <section className="landing-history" aria-label="History">
          <div className="landing-history-header">
            <div className="landing-history-title">
              <h2>History</h2>
              <span className="landing-history-count">{filtered.length}</span>
            </div>

            <div className="landing-toolbar">
              <label className="landing-search">
                <Search size={14} aria-hidden="true" />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search file / folder…"
                  aria-label="Search folders"
                />
              </label>

              <label className="landing-select-wrap">
                <span>Status</span>
                <select
                  value={statusFilter}
                  onChange={(e) => setStatusFilter(e.target.value as "ALL" | OcrRunStatus)}
                  aria-label="Filter by status"
                >
                  <option value="ALL">All</option>
                  <option value="QUEUED">Queued</option>
                  <option value="IN_PROGRESS">OCR in Progress</option>
                  <option value="COMPLETED">OCR Completed</option>
                  <option value="IMAGING_IN_PROGRESS">Imaging in Progress</option>
                  <option value="IMAGING_COMPLETED">Imaging Completed</option>
                  <option value="FAILED">Failed</option>
                </select>
              </label>

              <MultiCheckFilter
                label="Run"
                ariaLabel="Filter by run"
                options={runOptions}
                selected={runFilter}
                onChange={setRunFilter}
                formatOption={formatRunLabel}
                emptyLabel="All runs"
              />

              <MultiCheckFilter
                label="Batch"
                ariaLabel="Filter by batch"
                options={batchOptions}
                selected={batchFilter}
                onChange={setBatchFilter}
                formatOption={formatBatchLabel}
                emptyLabel="All batches"
              />

              <button
                type="button"
                className="landing-icon-btn"
                onClick={() => void load()}
                title="Refresh"
                aria-label="Refresh history"
                disabled={loading}
              >
                <RefreshCw size={15} />
              </button>
            </div>
          </div>

          <div className="landing-history-scroll">
            <table className="landing-history-table">
              <thead>
                <tr>
                  <th>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "filename" ? " active" : ""}`}
                      onClick={() => toggleSort("filename")}
                    >
                      Folder{sortIndicator("filename")}
                    </button>
                  </th>
                  <th>Run</th>
                  <th>Batch</th>
                  <th>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "pages" ? " active" : ""}`}
                      onClick={() => toggleSort("pages")}
                    >
                      Pages{sortIndicator("pages")}
                    </button>
                  </th>
                  <th>OCR</th>
                  <th>Imaging</th>
                  <th>Status</th>
                  <th>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "updated" ? " active" : ""}`}
                      onClick={() => toggleSort("updated")}
                    >
                      Last Updated{sortIndicator("updated")}
                    </button>
                  </th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {loading && folders.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="landing-history-empty">
                      <div className="landing-empty-state">
                        <div className="landing-empty-icon">
                          <RefreshCw size={18} />
                        </div>
                        <h3>Loading history…</h3>
                      </div>
                    </td>
                  </tr>
                ) : filtered.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="landing-history-empty">
                      <div className="landing-empty-state">
                        <div className="landing-empty-icon">
                          <Inbox size={18} />
                        </div>
                        <h3>{folders.length === 0 ? "No history found" : "No matching folders"}</h3>
                        <p>
                          {folders.length === 0
                            ? "Add folders under DATA_ROOT with pages/ and ocr/ outputs."
                            : "Try a different search, status, run, or batch filter."}
                        </p>
                      </div>
                    </td>
                  </tr>
                ) : (
                  pageFolders.map((folder) => (
                    <tr key={folder.id}>
                      <td className="landing-col-name" title={folder.name}>
                        {folder.name}
                      </td>
                      <td className="landing-col-num">{formatRunLabel(folder.run_id)}</td>
                      <td className="landing-col-num">{formatBatchLabel(folder.batch_id)}</td>
                      <td className="landing-col-num">{folder.page_count}</td>
                      <td className="landing-col-num">{folder.ocr_processed}</td>
                      <td className="landing-col-num">{folder.imaging_processed}</td>
                      <td>
                        <span className={statusClass(folder.ocr_status)}>
                          {OCR_STATUS_LABELS[folder.ocr_status]}
                        </span>
                      </td>
                      <td className="landing-col-updated">
                        {fmtUpdated(folder.last_updated_at)}
                      </td>
                      <td>
                        <div className="landing-row-actions">
                          <button
                            type="button"
                            className="action-icon-btn"
                            onClick={() => onView(folder.id, "ocr")}
                            title="View OCR"
                            aria-label={`View OCR for ${folder.name}`}
                          >
                            <FileText size={16} aria-hidden="true" />
                            OCR
                          </button>
                          <button
                            type="button"
                            className="action-icon-btn"
                            onClick={() => onView(folder.id, "imaging")}
                            title="View Imaging"
                            aria-label={`View Imaging for ${folder.name}`}
                          >
                            <ScanSearch size={16} aria-hidden="true" />
                            Imaging
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {filtered.length > 0 && (
            <div className="landing-pagination" aria-label="History pagination">
              <span className="landing-pagination-meta">
                {rangeStart}–{rangeEnd} of {filtered.length}
              </span>
              <div className="landing-pagination-controls">
                <button
                  type="button"
                  className="landing-page-btn"
                  disabled={page <= 1}
                  onClick={() => setPage((p) => Math.max(1, p - 1))}
                  aria-label="Previous page"
                >
                  <ChevronLeft size={14} />
                </button>
                {pageNumbers.map((n, idx) => {
                  const prev = pageNumbers[idx - 1];
                  const showEllipsis = prev != null && n - prev > 1;
                  return (
                    <span key={n} className="landing-page-num-wrap">
                      {showEllipsis && <span className="landing-page-ellipsis">…</span>}
                      <button
                        type="button"
                        className={`landing-page-num${n === page ? " active" : ""}`}
                        onClick={() => setPage(n)}
                        aria-label={`Page ${n}`}
                        aria-current={n === page ? "page" : undefined}
                      >
                        {n}
                      </button>
                    </span>
                  );
                })}
                <button
                  type="button"
                  className="landing-page-btn"
                  disabled={page >= totalPages}
                  onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
                  aria-label="Next page"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}
