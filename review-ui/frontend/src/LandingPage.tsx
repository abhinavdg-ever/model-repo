import { useEffect, useMemo, useRef, useState } from "react";
import {
  CalendarClock,
  ChevronLeft,
  ChevronRight,
  Download,
  FileText,
  Filter,
  FileSpreadsheet,
  Gauge,
  Inbox,
  RefreshCw,
  ScanSearch,
  Search,
  Trash2,
  CheckCircle2,
  X,
} from "lucide-react";
import { readSession } from "./session";
import {
  deleteFolder,
  formatBatchLabel,
  formatRunLabel,
  getFolderImaging,
  listFolders,
  OCR_STATUS_LABELS,
  type FolderListParams,
  type FolderSummary,
  type ImagingDocumentResponse,
  type ImagingPageResult,
  type OcrRunStatus,
  type PageGroundTruth,
} from "./api";
import { formatDuplicateDownload } from "./duplicateLabel";
import { RunAfterForm } from "./runAfter";
import {
  CheckListFilter,
  ClearAllFilters,
  ColumnFilterButton,
  META_FILTERS,
  MetaFilterBar,
  metaFilterCount,
  type MetaFilters,
} from "./columnFilter";
import { fillDocDosForDownload } from "./ImagingPanel";

const PAGE_SIZE = 15;
const filterIcon = <Filter size={11} aria-hidden="true" />;
const LANDING_FILTERS_KEY = "advantmed_imaging_landing_filters";

type SortKey = "filename" | "pages" | "updated";
type SortDir = "asc" | "desc";

type LandingFilters = {
  query: string;
  statusFilter: OcrRunStatus[];
  runFilter: string[];
  batchFilter: string[];
  /** Source / Measure / Retrieval. */
  metaFilter: MetaFilters;
  /** ISO date-time (India time) the chart must have last run at or after; null = any time. */
  runAfter: string | null;
  sortKey: SortKey;
  sortDir: SortDir;
  page: number;
};

const STATUS_OPTIONS: OcrRunStatus[] = [
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

/** The History list's saved search, filters and sort, unpaged: the order the
 * chart view's Next button walks. */
export function savedHistoryListParams(): FolderListParams {
  const saved = readLandingFilters();
  return {
    q: saved.query,
    status: saved.statusFilter,
    run: saved.runFilter,
    batch: saved.batchFilter,
    ...saved.metaFilter,
    run_after: saved.runAfter,
    sort: saved.sortKey,
    sort_dir: saved.sortDir,
  };
}

/** History starts unfiltered (search and filters cleared, sort kept): arriving from
 * Accuracy View must not look as if its filters came along. */
export function clearLandingFilters(): void {
  try {
    const saved = readLandingFilters();
    const cleared: LandingFilters = {
      ...saved,
      query: "",
      statusFilter: [],
      runFilter: [],
      batchFilter: [],
      metaFilter: {},
      runAfter: null,
      page: 1,
    };
    sessionStorage.setItem(LANDING_FILTERS_KEY, JSON.stringify(cleared));
  } catch {
    // Blocked storage: nothing saved, so History starts unfiltered anyway.
  }
}

function readLandingFilters(): LandingFilters {
  const defaults: LandingFilters = {
    query: "",
    statusFilter: [],
    runFilter: [],
    batchFilter: [],
    metaFilter: {},
    runAfter: null,
    // Newest run first.
    sortKey: "updated",
    sortDir: "desc",
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
      metaFilter: Object.fromEntries(
        META_FILTERS.map(({ key }) => [key, normalizeIdList(parsed.metaFilter?.[key])]),
      ),
      runAfter: typeof parsed.runAfter === "string" && parsed.runAfter ? parsed.runAfter : null,
      sortKey,
      sortDir,
      page,
    };
  } catch {
    return defaults;
  }
}

type Props = {
  onView: (folderId: string, mode?: "ocr" | "imaging") => void;
  onOpenAccuracy?: () => void;
  onOpenManualReview?: () => void;
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

/** One column of the results CSV: its value, and where the page has one, the
 * ground-truth label written in a gt_<header> column right after it. */
type ResultsColumn = {
  header: string;
  value: (page: ImagingPageResult, status: string) => unknown;
  truth?: (gt: PageGroundTruth) => string | null | undefined;
};

const RESULTS_COLUMNS: ResultsColumn[] = [
  { header: "chartName", value: () => "" },
  { header: "pageName", value: (p) => p.fileName },
  // Member name / DOB / ID ground truth is Yes or No: whether it is on the page.
  { header: "memberName", value: (p) => p.memberName, truth: (gt) => gt.memberName },
  { header: "memberID", value: (p) => p.memberId, truth: (gt) => gt.memberId },
  { header: "confidence", value: (p) => p.memberConfidence },
  { header: "memberDob", value: (p) => p.memberDob, truth: (gt) => gt.memberDob },
  { header: "handwrittenOrPrinted", value: (p) => p.handwrittenOrPrinted },
  { header: "handwrittenOrPrintedConfidence", value: (p) => p.handwrittenOrPrintedConfidence ?? "" },
  { header: "documentType", value: (p) => p.documentType ?? "" },
  { header: "isVisible", value: (p) => p.isVisible ?? "", truth: (gt) => gt.isVisible },
  { header: "handwrittenAreaPct", value: (p) => p.handwrittenAreaPct ?? "" },
  { header: "orientationAngle", value: (p) => p.orientationAngle, truth: (gt) => gt.rotation },
  { header: "tiltAngle", value: (p) => p.tiltAngle },
  { header: "mirrored", value: (p) => p.mirrored },
  { header: "pageQualityTag", value: (p) => p.pageQualityTag ?? "" },
  { header: "pageQualityConfidence", value: (p) => p.pageQualityConfidence },
  {
    header: "blankOrJunk",
    value: (p) => p.blankOrJunk ?? "NA",
    truth: (gt) => gtBlankOrJunk(gt),
  },
  {
    header: "isDuplicate",
    value: (p) =>
      p.isDuplicate == null
        ? "NA"
        : formatDuplicateDownload(p.isDuplicate, p.pageTypeConfidence, p.duplicateOf, p.isPartialDuplicate),
  },
  {
    header: "pageType",
    value: (p) => p.pageType ?? "Not Available",
    // The accuracy report's rule: the Encounter Type label, else Page Type.
    truth: (gt) => gt.encounterType || gt.pageType,
  },
  { header: "pageTypeConfidence", value: (p) => p.pageTypeConfidence },
  { header: "dosFrom", value: (p) => p.docDosFrom ?? p.dosFrom, truth: (gt) => gt.dosFrom },
  { header: "dosTo", value: (p) => p.docDosTo ?? p.dosTo, truth: (gt) => gt.dosTo },
  { header: "dosConfidence", value: (p) => p.dosConfidence ?? "" },
  { header: "member_verification_status", value: (_p, status) => status },
];

/** Ground-truth labels with no matching result column: last, with ground truth only. */
const GROUND_TRUTH_ONLY_COLUMNS: { header: string; truth: (gt: PageGroundTruth) => string | null | undefined }[] = [
  { header: "gt_codable", truth: (gt) => gt.codeable },
  { header: "gt_providerSignature", truth: (gt) => gt.providerSignature },
];

/** "Blank" / "Junk" / "No" from the separate Blank Page and Junk Page labels;
 * empty when neither is labelled. */
function gtBlankOrJunk(gt: PageGroundTruth): string {
  const yes = (v: string | null | undefined) => (v ?? "").trim().toLowerCase().startsWith("y");
  if (yes(gt.blankPage)) return "Blank";
  if (yes(gt.junkPage)) return "Junk";
  return (gt.blankPage ?? "").trim() || (gt.junkPage ?? "").trim() ? "No" : "";
}

function resultsHeaders(withTruth: boolean): string[] {
  const headers = RESULTS_COLUMNS.flatMap((column) =>
    withTruth && column.truth ? [column.header, `gt_${column.header}`] : [column.header],
  );
  return withTruth ? [...headers, ...GROUND_TRUTH_ONLY_COLUMNS.map((column) => column.header)] : headers;
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

function imagingDocToCsvRows(
  chartName: string,
  doc: ImagingDocumentResponse,
  withTruth: boolean,
): string[] {
  const status = verificationStatus(doc);
  // Blank where the page has no ground truth, or no label for the column.
  const truthOf = (page: ImagingPageResult, read: (gt: PageGroundTruth) => string | null | undefined) =>
    page.groundTruth ? (read(page.groundTruth) ?? "") : "";
  return fillDocDosForDownload(doc.pages).map((page) =>
    [
      ...RESULTS_COLUMNS.flatMap((column) => {
        const value = column.header === "chartName" ? chartName : column.value(page, status);
        return withTruth && column.truth ? [value, truthOf(page, column.truth)] : [value];
      }),
      ...(withTruth ? GROUND_TRUTH_ONLY_COLUMNS.map((column) => truthOf(page, column.truth)) : []),
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

export default function LandingPage({ onView, onOpenAccuracy, onOpenManualReview }: Props) {
  const saved = useMemo(() => readLandingFilters(), []);
  const [folders, setFolders] = useState<FolderSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [summary, setSummary] = useState({ charts: 0, pages: 0, truth: 0 });
  const [runOptions, setRunOptions] = useState<string[]>([]);
  const [batchOptions, setBatchOptions] = useState<string[]>([]);
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
  const [batchFilter, setBatchFilter] = useState<string[]>(
    saved.runFilter.length > 0 ? saved.batchFilter : [],
  );
  const [runAfter, setRunAfter] = useState<string | null>(saved.runAfter);
  const [metaFilter, setMetaFilter] = useState<MetaFilters>(saved.metaFilter);
  const [metaOptions, setMetaOptions] = useState<Record<string, string[]>>({});
  const skipFilterPageReset = useRef(true);
  const isAdmin = readSession()?.user.role === "admin";
  const [deleting, setDeleting] = useState<string | null>(null);
  // The in-app "are you sure" for an admin's Delete, and the note after it.
  const [deleteTarget, setDeleteTarget] = useState<{ id: string; name: string; pages: number } | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  useEffect(() => {
    if (!notice) return;
    const t = window.setTimeout(() => setNotice(null), 6000);
    return () => window.clearTimeout(t);
  }, [notice]);
  const [downloadOpen, setDownloadOpen] = useState(false);
  const [downloadBusy, setDownloadBusy] = useState(false);
  const [downloadWithTruth, setDownloadWithTruth] = useState(false);
  const [downloadDone, setDownloadDone] = useState(0);
  const [downloadTotal, setDownloadTotal] = useState(0);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const downloadCancelRef = useRef(false);
  const loadSeq = useRef(0);

  async function load() {
    const seq = ++loadSeq.current;
    setLoading(true);
    setError(null);
    try {
      const data = await listFolders({
        limit: PAGE_SIZE,
        offset: (page - 1) * PAGE_SIZE,
        q: query,
        status: statusFilter,
        run: runFilter,
        batch: batchFilter,
        ...metaFilter,
        run_after: runAfter,
        sort: sortKey,
        sort_dir: sortDir,
      });
      if (seq !== loadSeq.current) return;
      setFolders(data.items);
      setTotal(data.total);
      setSummary({
        charts: data.imaging_completed_charts ?? 0,
        pages: data.imaging_completed_pages ?? 0,
        truth: data.ground_truth_charts ?? 0,
      });
      setRunOptions(data.run_options);
      setBatchOptions(data.batch_options);
      setMetaOptions({
        scope: data.scope_options ?? [],
        source: data.source_options ?? [],
        measure: data.measure_options ?? [],
        retrieval: data.retrieval_options ?? [],
      });
      // Drop batch picks that are no longer valid for the selected run(s).
      setBatchFilter((prev) => {
        if (runFilter.length === 0) return prev.length === 0 ? prev : [];
        const allowed = new Set(data.batch_options);
        const next = prev.filter((b) => allowed.has(b));
        return next.length === prev.length ? prev : next;
      });
    } catch (err) {
      if (seq !== loadSeq.current) return;
      setError(err instanceof Error ? err.message : "Failed to load history");
    } finally {
      if (seq === loadSeq.current) setLoading(false);
    }
  }

  useEffect(() => {
    const t = window.setTimeout(() => {
      void load();
    }, query.trim() ? 200 : 0);
    return () => window.clearTimeout(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- intentional: reload on list controls
  }, [page, query, statusFilter, runFilter, batchFilter, metaFilter, runAfter, sortKey, sortDir]);

  useEffect(() => {
    try {
      const payload: LandingFilters = {
        query,
        statusFilter,
        runFilter,
        batchFilter,
        metaFilter,
        runAfter,
        sortKey,
        sortDir,
        page,
      };
      sessionStorage.setItem(LANDING_FILTERS_KEY, JSON.stringify(payload));
    } catch {
      /* ignore */
    }
  }, [query, statusFilter, runFilter, batchFilter, metaFilter, runAfter, sortKey, sortDir, page]);


  const totalPages = Math.max(1, Math.ceil(total / PAGE_SIZE));

  useEffect(() => {
    if (skipFilterPageReset.current) {
      skipFilterPageReset.current = false;
      return;
    }
    setPage(1);
  }, [query, sortKey, sortDir, statusFilter, runFilter, batchFilter, metaFilter, runAfter]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const pageFolders = folders;

  const rangeStart = total === 0 ? 0 : (page - 1) * PAGE_SIZE + 1;
  const rangeEnd = Math.min(page * PAGE_SIZE, total);

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

  const filterCount =
    (statusFilter.length ? 1 : 0) + (runFilter.length ? 1 : 0) + (batchFilter.length ? 1 : 0) + (runAfter ? 1 : 0) +
    metaFilterCount(metaFilter);

  function clearFilters() {
    setStatusFilter([]);
    setRunFilter([]);
    setBatchFilter([]);
    setMetaFilter({});
    setRunAfter(null);
  }

  /** Admin: delete the chart's pipeline results after a confirmation naming it. */
  function askDelete(id: string, name: string, pages: number) {
    setDeleteError(null);
    setDeleteTarget({ id, name, pages });
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    const { id, name } = deleteTarget;
    setDeleting(id);
    setDeleteError(null);
    try {
      await deleteFolder(id);
      setDeleteTarget(null);
      setNotice(`Chart ${name} deleted. Its ground truth, manifest and files on disk are kept.`);
      await load();
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : "Could not delete the chart");
    } finally {
      setDeleting(null);
    }
  }

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
    setDownloadTotal(total);
    setDownloadBusy(false);
    setDownloadOpen(true);
  }

  function closeDownloadDialog() {
    // While charts are being fetched, Cancel stops after the current chart and
    // downloads the charts already finished (startDownloadResults closes the dialog).
    if (downloadBusy) {
      downloadCancelRef.current = true;
      return;
    }
    setDownloadOpen(false);
    setDownloadError(null);
  }

  async function startDownloadResults() {
    if (total === 0 || downloadBusy) return;
    downloadCancelRef.current = false;
    setDownloadBusy(true);
    setDownloadError(null);
    setDownloadDone(0);

    const withTruth = downloadWithTruth;
    const lines: string[] = [resultsHeaders(withTruth).join(",")];
    let processed = 0;
    try {
      const all = await listFolders({
        q: query,
        status: statusFilter,
        run: runFilter,
        batch: batchFilter,
        ...metaFilter,
        run_after: runAfter,
        sort: sortKey,
        sort_dir: sortDir,
      });
      setDownloadTotal(all.items.length);
      for (const folder of all.items) {
        if (downloadCancelRef.current) break;
        try {
          const doc = await getFolderImaging(folder.id);
          lines.push(...imagingDocToCsvRows(folder.name, doc, withTruth));
        } catch {
          /* skip charts that fail to load; continue the pack */
        }
        processed += 1;
        setDownloadDone(processed);
      }
      const cancelled = downloadCancelRef.current;
      // Cancelled: the charts finished so far, if any.
      if (!cancelled || processed > 0) {
        const name = withTruth ? "imaging_export_with_ground_truth" : "imaging_export";
        downloadTextFile(
          cancelled ? `${name}_partial_${processed}_of_${all.items.length}.csv` : `${name}.csv`,
          `${lines.join("\n")}\n`,
          "text/csv;charset=utf-8",
        );
      }
      setDownloadOpen(false);
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
            <p>Browse processed charts, and view OCR and Imaging Pipeline Results.</p>
          </div>
          <div className="landing-intro-actions">
            <button
              type="button"
              className="landing-file-viewer-btn"
              onClick={openDownloadDialog}
              disabled={total === 0 || loading}
              title={
                statusFilter.length > 0 ||
                query.trim() ||
                runFilter.length > 0 ||
                batchFilter.length > 0 ||
                metaFilterCount(metaFilter) > 0 ||
                runAfter
                  ? "Download results for charts matching current filters"
                  : "Download all imaging pipeline outputs as CSV"
              }
            >
              <Download size={15} aria-hidden="true" />
              Download Results
            </button>
            {onOpenAccuracy ? (
              <button
                type="button"
                className="landing-file-viewer-btn"
                onClick={onOpenAccuracy}
              >
                <Gauge size={15} aria-hidden="true" />
                Accuracy View
              </button>
            ) : null}
            {onOpenManualReview ? (
              <button
                type="button"
                className="landing-file-viewer-btn"
                onClick={onOpenManualReview}
              >
                <FileSpreadsheet size={15} aria-hidden="true" />
                Manual Review
              </button>
            ) : null}
          </div>
        </div>

        {!loading && total === 0 && filterCount > 0 ? (
          // A filter that leaves nothing says so at the top, not only in the empty table.
          <div className="filter-empty-banner" role="status">
            No chart matches the filters.
            <ClearAllFilters count={filterCount} onClear={clearFilters} />
          </div>
        ) : null}

        {total > 0 && (
          <div
            className="landing-stats"
            aria-label="Summary"
            title="Imaging Completed charts among those listed (every stage finished)"
          >
            <div className="landing-stat">
              <span className="landing-stat-value">{summary.charts}</span>
              <span className="landing-stat-label">charts</span>
            </div>
            <div className="landing-stat-divider" />
            <div className="landing-stat">
              <span className="landing-stat-value">{summary.pages}</span>
              <span className="landing-stat-label">pages</span>
            </div>
            <div className="landing-stat-divider" />
            <div className="landing-stat">
              <span className="landing-stat-value">{summary.truth}</span>
              <span className="landing-stat-label">with ground truth available</span>
            </div>
            <span className="landing-stat-note">Imaging Completed only</span>
          </div>
        )}

        {error && <div className="error-banner">{error}</div>}
        {notice ? (
          <div className="success-banner" role="status">
            <CheckCircle2 size={15} aria-hidden="true" />
            <span>{notice}</span>
            <button type="button" className="success-banner-close" onClick={() => setNotice(null)} aria-label="Dismiss">
              <X size={14} />
            </button>
          </div>
        ) : null}

        <section className="landing-history" aria-label="History">
          <div className="landing-history-header">
            <div className="landing-history-title">
              <h2>History</h2>
              <span className="landing-history-count">{total}</span>
            </div>

            <div className="landing-toolbar">
              <label className="landing-search">
                <Search size={14} aria-hidden="true" />
                <input
                  type="search"
                  value={query}
                  onChange={(e) => setQuery(e.target.value)}
                  placeholder="Search file / chart…"
                  aria-label="Search charts"
                />
              </label>

              <MetaFilterBar options={metaOptions} filters={metaFilter} onChange={setMetaFilter} />

              <ClearAllFilters count={filterCount} onClear={clearFilters} />

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
                      Chart{sortIndicator("filename")}
                    </button>
                  </th>
                  <th className={runFilter.length ? "is-filtered" : undefined}>
                    <span className="column-th">
                      <span className="column-th-label">Run</span>
                      <ColumnFilterButton label="Run" active={runFilter.length > 0} icon={filterIcon}>
                        {() => (
                          <CheckListFilter
                            options={runOptions}
                            selected={runFilter}
                            onChange={(next) => {
                              setRunFilter(next);
                              if (next.length === 0) setBatchFilter([]);
                            }}
                            formatOption={formatRunLabel}
                          />
                        )}
                      </ColumnFilterButton>
                    </span>
                  </th>
                  <th className={batchFilter.length ? "is-filtered" : undefined}>
                    <span className="column-th">
                      <span className="column-th-label">Batch</span>
                      <ColumnFilterButton label="Batch" active={batchFilter.length > 0} icon={filterIcon}>
                        {() => (
                          <CheckListFilter
                            options={batchOptions}
                            selected={batchFilter}
                            onChange={setBatchFilter}
                            formatOption={formatBatchLabel}
                            lockedHint={runFilter.length === 0 ? "Select a run first (Run column)." : undefined}
                          />
                        )}
                      </ColumnFilterButton>
                    </span>
                  </th>
                  <th>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "pages" ? " active" : ""}`}
                      onClick={() => toggleSort("pages")}
                    >
                      Pages{sortIndicator("pages")}
                    </button>
                  </th>
                  <th className={statusFilter.length ? "is-filtered" : undefined}>
                    <span className="column-th">
                      <span className="column-th-label">Status</span>
                      <ColumnFilterButton label="Status" active={statusFilter.length > 0} icon={filterIcon}>
                        {() => (
                          <CheckListFilter
                            options={STATUS_OPTIONS}
                            selected={statusFilter}
                            onChange={(next) => setStatusFilter(next as OcrRunStatus[])}
                            formatOption={(v) => OCR_STATUS_LABELS[v as OcrRunStatus] ?? v}
                          />
                        )}
                      </ColumnFilterButton>
                    </span>
                  </th>
                  <th title="Client page labels loaded for this chart">Ground Truth</th>
                  <th className={runAfter ? "is-filtered" : undefined}>
                    <span className="column-th">
                      <button
                        type="button"
                        className={`th-sort${sortKey === "updated" ? " active" : ""}`}
                        onClick={() => toggleSort("updated")}
                      >
                        Last Run At{sortIndicator("updated")}
                      </button>
                      <ColumnFilterButton
                        label="Last Run At"
                        active={Boolean(runAfter)}
                        icon={<CalendarClock size={11} aria-hidden="true" />}
                        width={288}
                        popoverClassName="run-after-popover"
                      >
                        {(close) => (
                          <RunAfterForm
                            value={runAfter}
                            onApply={(next) => {
                              setRunAfter(next);
                              close();
                            }}
                          />
                        )}
                      </ColumnFilterButton>
                    </span>
                  </th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {loading && folders.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="landing-history-empty">
                      <div className="landing-empty-state">
                        <div className="landing-empty-icon">
                          <RefreshCw size={18} />
                        </div>
                        <h3>Loading history…</h3>
                      </div>
                    </td>
                  </tr>
                ) : total === 0 ? (
                  <tr>
                    <td colSpan={8} className="landing-history-empty">
                      <div className="landing-empty-state">
                        <div className="landing-empty-icon">
                          <Inbox size={18} />
                        </div>
                        <h3>
                          {!query.trim() &&
                          statusFilter.length === 0 &&
                          runFilter.length === 0 &&
                          batchFilter.length === 0 &&
                          !runAfter
                            ? "No history found"
                            : "No matching charts"}
                        </h3>
                        <p>
                          {!query.trim() &&
                          statusFilter.length === 0 &&
                          runFilter.length === 0 &&
                          batchFilter.length === 0 &&
                          !runAfter
                            ? "Add charts under DATA_ROOT with pages/ and ocr/ outputs."
                            : "Try a different search, status, run, batch, or run-after filter."}
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
                      <td>
                        <span className={statusClass(folder.ocr_status)}>
                          {OCR_STATUS_LABELS[folder.ocr_status]}
                        </span>
                      </td>
                      <td className="landing-col-num">
                        {folder.ground_truth_available ? "Yes" : "No"}
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
                          {isAdmin ? (
                            <button
                              type="button"
                              className="action-icon-btn action-delete-btn"
                              disabled={deleting === folder.id}
                              onClick={() => askDelete(folder.id, folder.name, folder.page_count)}
                              title="Delete this chart's pipeline results (admin)"
                              aria-label={`Delete ${folder.name}`}
                            >
                              <Trash2 size={16} aria-hidden="true" />
                              Delete
                            </button>
                          ) : null}
                        </div>
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>

          {total > 0 && (
            <div className="landing-pagination" aria-label="History pagination">
              <span className="landing-pagination-meta">
                {rangeStart}–{rangeEnd} of {total}
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

      {deleteTarget ? (
        <div
          className="modal-backdrop"
          role="presentation"
          onClick={() => {
            if (!deleting) setDeleteTarget(null);
          }}
        >
          <div
            className="modal-card"
            role="alertdialog"
            aria-modal="true"
            aria-labelledby="delete-chart-title"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="modal-card-header">
              <div className="blob-auth-title-row">
                <Trash2 size={18} aria-hidden="true" />
                <h2 id="delete-chart-title">Delete chart?</h2>
              </div>
              <button
                type="button"
                className="modal-close"
                onClick={() => setDeleteTarget(null)}
                disabled={Boolean(deleting)}
                aria-label="Close"
              >
                <X size={16} />
              </button>
            </div>
            <p className="blob-auth-copy">
              <strong>{deleteTarget.name}</strong> ({deleteTarget.pages} page{deleteTarget.pages === 1 ? "" : "s"}):
              every pipeline result for it is removed from the database. Its ground truth, manifest
              row and files on disk are kept, so a re-run rebuilds it. This cannot be undone here.
            </p>
            {deleteError ? (
              <div className="error-banner" style={{ marginTop: "0.75rem" }}>
                Could not delete: {deleteError}
              </div>
            ) : null}
            <div className="download-results-actions">
              <button
                type="button"
                className="landing-file-viewer-btn"
                onClick={() => setDeleteTarget(null)}
                disabled={Boolean(deleting)}
              >
                Cancel
              </button>
              <button
                type="button"
                className="landing-file-viewer-btn landing-file-viewer-btn-primary"
                onClick={() => void confirmDelete()}
                disabled={Boolean(deleting)}
                autoFocus
              >
                <Trash2 size={15} aria-hidden="true" />
                {deleting ? "Deleting…" : "Delete"}
              </button>
            </div>
          </div>
        </div>
      ) : null}

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
              the {total} chart{total === 1 ? "" : "s"} in
              the current filter. Cancel while it runs to download the charts
              finished so far.
            </p>
            <div className="download-results-choice" role="radiogroup" aria-label="What to download">
              {(
                [
                  [false, "Results only", "The pipeline output, one row per page."],
                  [
                    true,
                    "Results with ground truth",
                    "Adds a gt_ column after each result that has a ground-truth label (member, visibility, orientation, blank / junk, page type, DOS), then gt_codable and gt_providerSignature at the end. Blank where a page has no label.",
                  ],
                ] as const
              ).map(([value, title, detail]) => (
                <label key={title} className={downloadWithTruth === value ? "active" : undefined}>
                  <input
                    type="radio"
                    name="download-results-choice"
                    checked={downloadWithTruth === value}
                    disabled={downloadBusy}
                    onChange={() => setDownloadWithTruth(value)}
                  />
                  <span>
                    <strong>{title}</strong>
                    <span>{detail}</span>
                  </span>
                </label>
              ))}
            </div>
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
                title={
                  downloadBusy
                    ? "Stop and download the charts finished so far"
                    : undefined
                }
              >
                {downloadBusy
                  ? `Cancel (download ${downloadDone} finished)`
                  : "Cancel"}
              </button>
              <button
                type="button"
                className="landing-file-viewer-btn landing-file-viewer-btn-primary"
                onClick={() => void startDownloadResults()}
                disabled={downloadBusy || total === 0}
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
