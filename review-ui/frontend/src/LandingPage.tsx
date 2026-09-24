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
  X,
} from "lucide-react";
import {
  formatBatchLabel,
  formatRunLabel,
  getFolderImaging,
  listFolders,
  OCR_STATUS_LABELS,
  type FolderSummary,
  type ImagingDocumentResponse,
  type ImagingPageResult,
  type OcrRunStatus,
} from "./api";
import { formatDuplicateLabel } from "./duplicateLabel";

const PAGE_SIZE = 15;
const LANDING_FILTERS_KEY = "advantmed_imaging_landing_filters";

type SortKey = "filename" | "pages" | "updated";
type SortDir = "asc" | "desc";

type LandingFilters = {
  query: string;
  statusFilter: OcrRunStatus[];
  runFilter: string[];
  batchFilter: string[];
  sortKey: SortKey;
  sortDir: SortDir;
  page: number;
};

const STATUS_OPTIONS: OcrRunStatus[] = [
  "QUEUED",
  "IN_PROGRESS",
  "COMPLETED",
  "IMAGING_IN_PROGRESS",
  "IMAGING_COMPLETED",
  "FAILED",
];

const STATUS_VALUES = new Set<string>(STATUS_OPTIONS);

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

function normalizeStatusList(raw: unknown): OcrRunStatus[] {
  return normalizeIdList(raw).filter((v): v is OcrRunStatus => STATUS_VALUES.has(v));
}

function readLandingFilters(): LandingFilters {
  const defaults: LandingFilters = {
    query: "",
    statusFilter: [],
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
      statusFilter: normalizeStatusList(parsed.statusFilter),
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

  const allSelected = selected.length === 0;
  const summary = allSelected
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
            <button
              type="button"
              className={
                allSelected
                  ? "landing-multi-option landing-multi-select-all is-active"
                  : "landing-multi-option landing-multi-select-all"
              }
              role="option"
              aria-selected={allSelected}
              onClick={() => onChange([])}
            >
              <span className="landing-multi-select-all-mark" aria-hidden="true">
                {allSelected ? "✓" : ""}
              </span>
              <span>Select all</span>
            </button>
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

const RESULTS_CSV_HEADERS = [
  "chartName",
  "pageName",
  "memberName",
  "memberID",
  "confidence",
  "memberDob",
  "handwrittenOrPrinted",
  "handwrittenOrPrintedConfidence",
  "orientationAngle",
  "tiltAngle",
  "mirrored",
  "pageQualityTag",
  "pageQualityConfidence",
  "blankOrJunk",
  "isDuplicate",
  "pageType",
  "pageTypeConfidence",
  "dosFrom",
  "dosTo",
  "dosConfidence",
  "member_verification_status",
] as const;

const DEFAULT_DOC_DOS = "2/2/2022";
const DEFAULT_DOC_DOS_CONFIDENCE = 0.8;

function fillDocDosForDownload(pages: ImagingPageResult[]): ImagingPageResult[] {
  let prevFrom: string | null = null;
  let prevTo: string | null = null;
  let prevConf: number | null = null;
  return [...pages]
    .sort((a, b) => a.pageNumber - b.pageNumber)
    .map((page) => {
      let dosFrom = (page.docDosFrom || page.dosFrom || "").trim() || null;
      let dosTo = (page.docDosTo || page.dosTo || "").trim() || null;
      let dosConfidence = page.dosConfidence ?? null;
      if (!dosFrom && !dosTo) {
        if (prevFrom) {
          dosFrom = prevFrom;
          dosTo = prevTo ?? prevFrom;
          dosConfidence = prevConf;
        } else {
          dosFrom = DEFAULT_DOC_DOS;
          dosTo = DEFAULT_DOC_DOS;
          dosConfidence = DEFAULT_DOC_DOS_CONFIDENCE;
        }
      } else {
        if (!dosFrom) dosFrom = dosTo ?? prevFrom ?? DEFAULT_DOC_DOS;
        if (!dosTo) dosTo = dosFrom ?? prevTo ?? DEFAULT_DOC_DOS;
        if (
          (dosFrom === DEFAULT_DOC_DOS || dosTo === DEFAULT_DOC_DOS) &&
          dosConfidence == null
        ) {
          dosConfidence = DEFAULT_DOC_DOS_CONFIDENCE;
        }
      }
      prevFrom = dosFrom;
      prevTo = dosTo;
      prevConf = dosConfidence ?? prevConf;
      return {
        ...page,
        docDosFrom: dosFrom,
        docDosTo: dosTo,
        dosConfidence,
      };
    });
}

function csvEscape(value: unknown): string {
  const s = value === null || value === undefined ? "" : String(value);
  if (/[",\n\r]/.test(s)) return `"${s.replace(/"/g, '""')}"`;
  return s;
}

function verificationStatus(doc: ImagingDocumentResponse): string {
  return (
    doc.verifications?.[0]?.finalStatus ??
    doc.verification?.finalStatus ??
    ""
  );
}

function imagingDocToCsvRows(chartName: string, doc: ImagingDocumentResponse): string[] {
  const status = verificationStatus(doc);
  return fillDocDosForDownload(doc.pages).map((p) =>
    [
      chartName,
      p.fileName,
      p.memberName,
      p.memberId,
      p.memberConfidence,
      p.memberDob,
      p.handwrittenOrPrinted,
      p.handwrittenOrPrintedConfidence ?? "",
      p.orientationAngle,
      p.tiltAngle,
      p.mirrored,
      p.pageQualityTag ?? "",
      p.pageQualityConfidence,
      p.blankOrJunk ?? "NA",
      p.isDuplicate == null
        ? "NA"
        : formatDuplicateLabel(p.isDuplicate, p.pageTypeConfidence),
      p.pageType ?? "Not Available",
      p.pageTypeConfidence,
      p.docDosFrom ?? p.dosFrom,
      p.docDosTo ?? p.dosTo,
      p.dosConfidence ?? "",
      status,
    ]
      .map(csvEscape)
      .join(","),
  );
}

function downloadTextFile(filename: string, text: string, mime: string) {
  const blob = new Blob([text], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.rel = "noopener";
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
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
  const [statusFilter, setStatusFilter] = useState<OcrRunStatus[]>(
    saved.statusFilter,
  );
  const [runFilter, setRunFilter] = useState<string[]>(saved.runFilter);
  const [batchFilter, setBatchFilter] = useState<string[]>(saved.batchFilter);
  const skipFilterPageReset = useRef(true);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [downloadDone, setDownloadDone] = useState(0);
  const [downloadTotal, setDownloadTotal] = useState(0);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const downloadCancelRef = useRef(false);

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
    if (statusFilter.length > 0) {
      const allowed = new Set(statusFilter);
      rows = rows.filter((f) => allowed.has(f.ocr_status));
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

  function openDownloadDialog() {
    setDownloadError(null);
    setDownloadDone(0);
    setDownloadTotal(filtered.length);
    setDownloadBusy(false);
    setDownloadOpen(true);
  }

  function closeDownloadDialog() {
    if (downloadBusy) {
      downloadCancelRef.current = true;
    }
    setDownloadOpen(false);
    setDownloadBusy(false);
    setDownloadError(null);
  }

  async function startDownloadResults() {
    if (filtered.length === 0 || downloadBusy) return;
    downloadCancelRef.current = false;
    setDownloadBusy(true);
    setDownloadError(null);
    setDownloadDone(0);
    setDownloadTotal(filtered.length);

    const lines: string[] = [RESULTS_CSV_HEADERS.join(",")];
    let processed = 0;
    try {
      for (const folder of filtered) {
        if (downloadCancelRef.current) break;
        try {
          const doc = await getFolderImaging(folder.id);
          lines.push(...imagingDocToCsvRows(folder.name, doc));
        } catch {
          /* skip charts that fail to load; continue the pack */
        }
        processed += 1;
        setDownloadDone(processed);
      }
      if (!downloadCancelRef.current) {
        downloadTextFile(
          "imaging_export.csv",
          `${lines.join("\n")}\n`,
          "text/csv;charset=utf-8",
        );
        setDownloadOpen(false);
      }
    } catch (err) {
      setDownloadError(err instanceof Error ? err.message : "Download failed");
    } finally {
      setDownloadBusy(false);
      downloadCancelRef.current = false;
    }
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
              onClick={openDownloadDialog}
              disabled={folders.length === 0 || loading}
              title={
                statusFilter.length > 0 ||
                query.trim() ||
                runFilter.length > 0 ||
                batchFilter.length > 0
                  ? "Download results for folders matching current filters"
                  : "Download all imaging pipeline outputs as CSV"
              }
            >
              <Download size={15} aria-hidden="true" />
              Download Results
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

              <MultiCheckFilter
                label="Status"
                ariaLabel="Filter by status"
                options={STATUS_OPTIONS}
                selected={statusFilter}
                onChange={(next) => setStatusFilter(next as OcrRunStatus[])}
                formatOption={(v) => OCR_STATUS_LABELS[v as OcrRunStatus] ?? v}
                emptyLabel="All statuses"
              />

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

      {downloadOpen ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!downloadBusy) closeDownloadDialog();
          }}
        >
          <div
            className="modal-card"
            role="dialog"
            aria-modal="true"
            aria-labelledby="download-results-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-card-header">
              <div className="blob-auth-title-row">
                <Download size={18} aria-hidden="true" />
                <h2 id="download-results-title">Download Results</h2>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={closeDownloadDialog}
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>
            <p className="blob-auth-copy">
              This will take a few minutes. Results are built chart by chart for
              the {filtered.length} folder{filtered.length === 1 ? "" : "s"} in
              the current filter.
            </p>
            {downloadBusy || downloadDone > 0 ? (
              <div className="download-results-progress" aria-live="polite">
                <div className="download-results-progress-meta">
                  <span>
                    Charts processed: {downloadDone} / {downloadTotal}
                  </span>
                  <span>
                    {downloadTotal === 0
                      ? "0%"
                      : `${Math.round((downloadDone / downloadTotal) * 100)}%`}
                  </span>
                </div>
                <div
                  className="download-results-progress-track"
                  role="progressbar"
                  aria-valuemin={0}
                  aria-valuemax={downloadTotal}
                  aria-valuenow={downloadDone}
                >
                  <div
                    className="download-results-progress-fill"
                    style={{
                      width:
                        downloadTotal === 0
                          ? "0%"
                          : `${(downloadDone / downloadTotal) * 100}%`,
                    }}
                  />
                </div>
              </div>
            ) : null}
            {downloadError ? (
              <div className="error-banner" style={{ marginTop: "0.75rem" }}>
                {downloadError}
              </div>
            ) : null}
            <div className="download-results-actions">
              <button
                type="button"
                className="landing-file-viewer-btn"
                onClick={closeDownloadDialog}
                disabled={downloadBusy}
              >
                Cancel
              </button>
              <button
                type="button"
                className="landing-file-viewer-btn landing-file-viewer-btn-primary"
                onClick={() => void startDownloadResults()}
                disabled={downloadBusy || filtered.length === 0}
              >
                {downloadBusy ? "Preparing…" : "Start download"}
              </button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  );
}
