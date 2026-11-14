import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  ArrowLeft,
  Ban,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Database,
  Download,
  ExternalLink,
  FileSpreadsheet,
  FileText,
  Filter as FilterIcon,
  Info,
  Link2,
  Loader2,
  Search,
  Square,
  Upload,
  X,
} from "lucide-react";
import {
  streamManualReview,
  downloadManualReviewReport,
  downloadManualReviewSummary,
  getManualReviewColumns,
  type ManualReviewBundle,
  type ManualReviewColumns,
  type ManualReviewComparison,
  type ManualReviewComparisonKey,
  type ManualReviewComparisonMeta,
  type ManualReviewRow,
  type ManualReviewStatus,
} from "./api";
import { DateLines } from "./AccuracyView";
import { CheckListFilter, ColumnFilterButton } from "./columnFilter";
import { saveChartOrder } from "./chartSequence";
import { readGroundTruthSource } from "./groundTruthSource";
import { readFile, readSources, writeFile, writeSources, type Source } from "./manualReviewStore";
import { useViewState } from "./viewState";

type Props = {
  onBack: () => void;
  /** ``page``: open the chart on that page (the row's), not its first. */
  onOpenChart: (folderId: string, page?: number) => void;
  chartHref: (folderId: string, page?: number) => string;
};

/** A row's page number as the viewer takes it; none when it is not a single page. */
function rowPage(pageNumber: string): number | undefined {
  const page = Number(String(pageNumber).trim());
  return Number.isInteger(page) && page > 0 ? page : undefined;
}

const ROWS_PER_PAGE = 10;
const ACCEPT = ".xlsx,.xls,.csv";

const STATUS_CLASS: Record<ManualReviewStatus, string> = {
  CORRECT: "status-pill status-completed",
  ERROR: "status-pill status-failed",
  MISSING: "status-pill status-imaging",
  EXTRA: "status-pill status-progress",
  UNVALIDATED: "status-pill status-queued",
  EXCLUDED: "status-pill status-queued",
};

type Filter = "ALL" | ManualReviewStatus;

/** A results column narrowed to pages where that field matched or did not. */
type FieldFilter = "match" | "mismatch";

/** Whether a row passes every column filter. Only a compared row (Correct or
 * Error) has a field outcome, so a Missing / Extra / Unvalidated row is left
 * out while any column filter is on. */
function passesFieldFilters(row: ManualReviewRow, fieldFilters: Record<string, FieldFilter>): boolean {
  const active = Object.entries(fieldFilters);
  if (active.length === 0) return true;
  if (row.status !== "CORRECT" && row.status !== "ERROR") return false;
  return active.every(([field, want]) => row.mismatched.includes(field) === (want === "mismatch"));
}

/** A cell with no value, as a value filter's option. */
const BLANK_VALUE = "(Blank)";

/** Which side a value filter reads: any, or one source -- the comparison's first (gt),
 * second (algo) or, for All 3, third. */
type ValueSide = "either" | "gt" | "algo" | "third";
const SIDE_IDS = ["gt", "algo", "third"] as const;

/** A row's values for one column on the chosen side(s); a Missing record has only the
 * first source and an Extra one only the second. */
function rowValues(row: ManualReviewRow, field: string, side: ValueSide, tokens: string[]): string[] {
  const out: string[] = [];
  tokens.forEach((_token, index) => {
    if (side !== "either" && side !== SIDE_IDS[index]) return;
    if (row.status === "EXTRA" && index !== 1) return;
    if (row.status === "MISSING" && index !== 0) return;
    out.push(sideValue(row, field, index, tokens).trim() || BLANK_VALUE);
  });
  return out;
}

/** 2026-11-05 (user): a column narrowed to some of its values, on the ground truth, the
 * algo output, or either. */
function passesValueFilters(
  row: ManualReviewRow,
  valueFilters: Record<string, string[]>,
  valueSides: Record<string, ValueSide>,
  tokens: string[],
): boolean {
  return Object.entries(valueFilters).every(([field, wanted]) => {
    if (!wanted.length) return true;
    return rowValues(row, field, valueSides[field] ?? "either", tokens).some((v) => wanted.includes(v));
  });
}

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

/** Database / Upload file / Not used toggle in a card's title row. */
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
          ["db", "Database", Database],
          ["upload", "Upload file", Upload],
          ["none", "Not used", Ban],
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

/** A source left out of the comparison. */
function NotUsedPick() {
  return (
    <div className="excel-compare-pick has-file excel-compare-db excel-compare-unused">
      <span className="excel-compare-pick-file">
        <Ban size={14} aria-hidden="true" />
        Not used
      </span>
      <span className="excel-compare-pick-hint">Left out of the comparison</span>
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

/** "GT: a | Algo: b" (the comparison's ``tokens``; three for All 3) -> each side's value;
 * anything else is one agreed value (null). */
function splitDisplay(display: string, tokens: string[]): string[] | null {
  const first = `${tokens[0]}: `;
  if (!display.startsWith(first)) return null;
  const out: string[] = [];
  let rest = display.slice(first.length);
  for (const token of tokens.slice(1)) {
    const separator = ` | ${token}: `;
    const cut = rest.indexOf(separator);
    if (cut < 0) return null;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut + separator.length);
  }
  out.push(rest);
  return out;
}

/** One side's value. A mismatched cell holds "GT: a | Algo: b", and so does a match
 * written differently (credentials "MD" / "MD, PhD", a blank / junk page's Skipped);
 * any other cell is the value every side agrees on. A Missing record has only the first
 * source and an Extra one only the second. */
function sideValue(row: ManualReviewRow, field: string, side: number, tokens: string[]): string {
  const display = row.values[field] ?? "";
  if (row.status === "MISSING") return side === 0 ? display : "";
  if (row.status === "EXTRA") return side === 1 ? display : "";
  const sides = splitDisplay(display, tokens);
  if (!sides) return display;
  return sides[side] ?? "";
}

/** A Correct record whose sides read the same everywhere: one row. Otherwise a row per
 * source, so the algo values show as the chart view shows them (2026-10-29). */
function oneRow(row: ManualReviewRow, fields: string[], tokens: string[]): boolean {
  return row.status === "CORRECT" && !fields.some((field) => splitDisplay(row.values[field] ?? "", tokens));
}

function percent(value: number | null | undefined): string {
  return value == null ? "—" : `${value.toFixed(2)}%`;
}

/** "Display all": every comparison's headline numbers and per-field accuracy side by
 * side; a column's heading opens that comparison. */
function DisplayAll({
  bundle,
  onPick,
}: {
  bundle: ManualReviewBundle;
  onPick: (key: ManualReviewComparisonKey) => void;
}) {
  const comparisons = bundle.order
    .map((key) => bundle.comparisons[key])
    .filter((c): c is ManualReviewComparison => Boolean(c));
  const fields = [...new Set(comparisons.flatMap((c) => c.fields))];
  const rate = (c: ManualReviewComparison, metric: string) =>
    percent(c.summary.find((row) => row.metric === metric)?.percentage);
  const matched = (c: ManualReviewComparison) =>
    c.matchedCount ?? c.correctCount + c.errorCount + c.unvalidatedCount + (c.excludedCount ?? 0);
  const metrics: [string, (c: ManualReviewComparison) => ReactNode][] = [
    ["Base pages", (c) => c.groundCount],
    ["Compared-against pages", (c) => c.systemCount],
    ["Matched", matched],
    ["Exact matches", (c) => c.correctCount],
    ["Field errors", (c) => c.errorCount],
    ["Missing", (c) => c.missingCount],
    ["Exact match rate", (c) => rate(c, "Match Percentage")],
    ["Discrepancy rate", (c) => rate(c, "Overall Disparity Rate")],
  ];

  function fieldCell(c: ManualReviewComparison, field: string): ReactNode {
    if (!c.fields.includes(field)) return <span className="excel-compare-all-na">not compared</span>;
    if (!c.fieldErrors) return "…";
    const evaluated = c.evaluatedCount ?? matched(c);
    if (!evaluated) return "—";
    const entry = c.fieldErrors.find((e) => e.field === field);
    const accuracy = 100 - (entry?.percentage ?? 0);
    const tone = accuracy >= 95 ? "good" : accuracy >= 80 ? "fair" : "poor";
    return (
      <span className="excel-compare-all-field">
        <span className="excel-compare-field-bar" aria-hidden="true">
          <span className={`is-${tone}`} style={{ width: `${accuracy}%` }} />
        </span>
        {accuracy.toFixed(2)}%
      </span>
    );
  }

  return (
    <section className="accuracy-section" aria-label="Every comparison">
      <h2>Every comparison</h2>
      <p className="accuracy-note">
        Each comparison's totals, and each field's accuracy (evaluated pages that agreed on it).
        Open a comparison for its pages.
      </p>
      <div className="accuracy-table-scroll">
        <table className="landing-history-table accuracy-table excel-compare-all-table">
          <thead>
            <tr>
              <th scope="col">Metric</th>
              {comparisons.map((c) => (
                <th key={c.key} scope="col">
                  <button type="button" className="excel-compare-all-open" onClick={() => onPick(c.key)}>
                    {c.label}
                  </button>
                  <span className="excel-compare-all-sides">{c.shortSides.join(" · ")}</span>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {metrics.map(([label, value]) => (
              <tr key={label}>
                <th scope="row">{label}</th>
                {comparisons.map((c) => (
                  <td key={c.key}>{value(c)}</td>
                ))}
              </tr>
            ))}
            {fields.map((field) => (
              <tr key={field}>
                <th scope="row">{field}</th>
                {comparisons.map((c) => (
                  <td key={c.key}>{fieldCell(c, field)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

/** The two-source stream's one comparison (no QA file): Client vs Pipeline. */
const CLASSIC_META = {
  key: "client_pipeline" as const,
  label: "Client vs Pipeline",
  sides: ["Client Ground Truth", "Pipeline Output"],
  shortSides: ["Client", "Pipeline"],
  tokens: ["GT", "Algo"],
};

const COMPARISON_LABELS: Record<ManualReviewComparisonKey, string> = {
  qa_client: "QA vs Client",
  qa_pipeline: "QA vs Pipeline",
  client_pipeline: "Client vs Pipeline",
  all: "All 3",
};

/** One comparison, or every comparison's summary side by side. */
type ViewKey = ManualReviewComparisonKey | "display_all";

function emptyComparison(
  meta: ManualReviewComparisonMeta,
  fields: string[],
  exemptedColumns: { column: string; reason: string }[],
): ManualReviewComparison {
  return {
    ...meta,
    groundCount: 0,
    systemCount: 0,
    correctCount: 0,
    errorCount: 0,
    missingCount: 0,
    extraCount: 0,
    unvalidatedCount: 0,
    excludedCount: 0,
    fields,
    exemptedColumns,
    summary: [],
    rows: [],
  };
}

/** The last comparison, kept while review-ui is open so coming back to the
 * page shows it again. Cleared whenever an input changes. */
let lastBundle: ManualReviewBundle | null = null;
let lastViewKey: ViewKey | null = null;

export default function ManualReviewView({ onBack, onOpenChart, chartHref }: Props) {
  const [qa, setQa] = useState<File | null>(null);
  const [ground, setGround] = useState<File | null>(null);
  const [system, setSystem] = useState<File | null>(null);
  const [groundSource, setGroundSource] = useState<Source>(() => readSources().ground);
  const [systemSource, setSystemSource] = useState<Source>(() => readSources().system);
  const [showExempt, setShowExempt] = useState(false);
  const [bundle, setBundleState] = useState<ManualReviewBundle | null>(lastBundle);
  const [viewKey, setViewKeyState] = useState<ViewKey | null>(lastViewKey);

  function setBundle(next: ManualReviewBundle | null) {
    lastBundle = next;
    setBundleState(next);
  }

  // The comparison on screen: the one chosen, else the first (Client vs Pipeline without QA).
  const activeKey: ViewKey | null = bundle
    ? viewKey && (viewKey === "display_all" ? bundle.order.length > 1 : bundle.comparisons[viewKey])
      ? viewKey
      : bundle.order[0] ?? null
    : null;
  const result: ManualReviewComparison | null =
    bundle && activeKey && activeKey !== "display_all" ? bundle.comparisons[activeKey] ?? null : null;
  const tokens = result?.tokens ?? CLASSIC_META.tokens;
  const shortSides = result?.shortSides ?? CLASSIC_META.shortSides;

  // Files uploaded earlier, back from the browser's storage.
  useEffect(() => {
    let live = true;
    void Promise.all([readFile("qa"), readFile("ground"), readFile("system")]).then(([savedQa, savedGround, savedSystem]) => {
      if (!live) return;
      setQa((current) => current ?? savedQa);
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
  const [downloading, setDownloading] = useState<"xlsx" | "pdf" | null>(null);
  // Seconds since the download began, so a long build visibly progresses.
  const [downloadSeconds, setDownloadSeconds] = useState(0);
  useEffect(() => {
    if (!downloading) return;
    setDownloadSeconds(0);
    const started = Date.now();
    const timer = window.setInterval(
      () => setDownloadSeconds(Math.floor((Date.now() - started) / 1000)),
      1000,
    );
    return () => window.clearInterval(timer);
  }, [downloading]);
  const [error, setError] = useState<string | null>(null);
  // Status filter, column filters, search and page survive opening a chart and coming Back.
  const [view, setView] = useViewState<{
    filter: Filter;
    page: number;
    query: string;
    fieldFilters: Record<string, FieldFilter>;
    valueFilters?: Record<string, string[]>;
    valueSides?: Record<string, ValueSide>;
    filterField?: string;
  }>("manual-review", { filter: "ALL", page: 1, query: "", fieldFilters: {}, valueFilters: {}, valueSides: {}, filterField: "" });
  const { filter, page, query } = view;
  const fieldFilters = view.fieldFilters ?? {};
  const valueFilters = view.valueFilters ?? {};
  const valueSides = view.valueSides ?? {};
  const setValueFilter = (field: string, values: string[]) => {
    const next = { ...valueFilters };
    if (values.length) next[field] = values;
    else delete next[field];
    setView({ valueFilters: next, page: 1 });
  };
  const setFilter = (value: Filter) => setView({ filter: value });
  const setFieldFilter = (field: string, value: FieldFilter | "") => {
    const next = { ...fieldFilters };
    if (value) next[field] = value;
    else delete next[field];
    setView({ fieldFilters: next, page: 1 });
  };
  const setPage = (value: number) => setView({ page: value });
  const setQuery = (value: string) => setView({ query: value });
  // Charts read so far while a comparison streams in; null when idle.
  const [progress, setProgress] = useState<{ loaded: number; total: number } | null>(null);
  const streamRef = useRef<AbortController | null>(null);
  // Set when Stop ends a comparison early: the charts loaded so far stay on screen.
  const [stopped, setStopped] = useState<{ loaded: number; total: number } | null>(null);

  function stopCompare() {
    const controller = streamRef.current;
    if (!controller) return;
    setStopped(progress ?? { loaded: 0, total: 0 });
    controller.abort();
  }

  // Leaving the page stops a comparison still streaming; a partial one is not kept.
  useEffect(
    () => () => {
      if (streamRef.current) {
        streamRef.current.abort();
        lastBundle = null;
      }
    },
    [],
  );

  function pick(side: "qa" | "ground" | "system", setter: (file: File | null) => void) {
    return (file: File | null) => {
      streamRef.current?.abort();
      setStopped(null);
      setter(file);
      void writeFile(side, file);
      // A result belongs to the files it was run on.
      setBundle(null);
      setError(null);
    };
  }

  // Any two sources are enough; a source set to Upload needs its file.
  const groundUsed = groundSource !== "none";
  const systemUsed = systemSource !== "none";
  const uploadMissing = (groundSource === "upload" && !ground) || (systemSource === "upload" && !system);
  const sourceCount = (qa ? 1 : 0) + (groundUsed ? 1 : 0) + (systemUsed ? 1 : 0);
  const ready = sourceCount >= 2 && !uploadMissing;
  const planned: ManualReviewComparisonKey[] = [
    ...(qa && groundUsed ? (["qa_client"] as const) : []),
    ...(qa && systemUsed ? (["qa_pipeline"] as const) : []),
    ...(groundUsed && systemUsed ? (["client_pipeline"] as const) : []),
    ...(qa && groundUsed && systemUsed ? (["all"] as const) : []),
  ];
  const input = () => ({ qa, ground, groundSource, system, systemSource });

  function setViewKey(next: ViewKey) {
    lastViewKey = next;
    setViewKeyState(next);
    // Each comparison has its own fields: its filters start fresh.
    setView({ filter: "ALL", page: 1, fieldFilters: {}, valueFilters: {}, valueSides: {}, filterField: "" });
  }

  function chooseSource(setter: (source: Source) => void) {
    return (source: Source) => {
      streamRef.current?.abort();
      setStopped(null);
      setter(source);
      setBundle(null);
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
    setStopped(null);
    setView({ filter: "ALL", page: 1, fieldFilters: {}, valueFilters: {}, valueSides: {}, filterField: "" });
    lastViewKey = null;
    setViewKeyState(null);
    // Filled in chart by chart as the backend reads each one.
    let current: ManualReviewBundle | null = null;
    const update = (
      key: ManualReviewComparisonKey,
      change: (comparison: ManualReviewComparison) => ManualReviewComparison,
    ) => {
      const comparison = current?.comparisons[key];
      if (current && comparison) current = { ...current, comparisons: { ...current.comparisons, [key]: change(comparison) } };
    };
    try {
      await streamManualReview(
        input(),
        (event) => {
          if (event.type === "start") {
            const metas = event.comparisons ?? [
              { ...CLASSIC_META, fields: event.fields, exemptedColumns: event.exemptedColumns },
            ];
            current = {
              order: metas.map((meta) => meta.key),
              comparisons: Object.fromEntries(
                metas.map(({ fields, exemptedColumns, ...meta }) => [meta.key, emptyComparison(meta, fields, exemptedColumns)]),
              ),
              lastRun: {},
            };
            setProgress({ loaded: 0, total: event.charts });
          } else if (event.type === "error") {
            setError(`Stopped at chart ${event.pdfId}: ${event.detail}`);
            return;
          } else if (current && event.type === "chart") {
            const parts = event.comparisons ?? (() => {
              const { type: _type, pdfId: _pdfId, lastRunAt: _lastRunAt, comparisons: _none, ...part } = event;
              return { client_pipeline: part };
            })();
            for (const [key, part] of Object.entries(parts)) {
              if (!part) continue;
              const { rows, ...counts } = part;
              update(key as ManualReviewComparisonKey, (comparison) => ({
                ...comparison,
                ...counts,
                rows: [...comparison.rows, ...rows],
              }));
            }
            current = { ...current, lastRun: { ...current.lastRun, [event.pdfId]: event.lastRunAt } };
            setProgress((p) => (p ? { ...p, loaded: p.loaded + 1 } : p));
          } else if (current && event.type === "done") {
            const reportId = event.reportId;
            const parts = event.comparisons ?? (() => {
              const { type: _type, comparisons: _none, ...totals } = event;
              return { client_pipeline: totals };
            })();
            for (const [key, totals] of Object.entries(parts)) {
              if (totals) update(key as ManualReviewComparisonKey, (comparison) => ({ ...comparison, ...totals, reportId }));
            }
            current = { ...current, reportId };
          }
          setBundle(current);
        },
        controller.signal,
      );
    } catch (err) {
      if (controller.signal.aborted) return;
      if (!current) setBundle(null);
      setError(err instanceof Error ? err.message : "Comparison failed");
    } finally {
      if (streamRef.current === controller) {
        streamRef.current = null;
        setComparing(false);
        setProgress(null);
      }
    }
  }

  async function runDownload(kind: "xlsx" | "pdf") {
    if (!ready) return;
    setDownloading(kind);
    setError(null);
    try {
      // The comparison on screen, so the file matches it and nothing is compared again.
      const reportId = bundle?.reportId;
      const blob =
        kind === "pdf"
          ? await downloadManualReviewSummary(input(), reportId)
          : await downloadManualReviewReport(input(), reportId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = kind === "pdf" ? "manual_review_master_summary.pdf" : "manual_review_report.xlsx";
      a.click();
      // Revoked once the browser has started the download, not before.
      window.setTimeout(() => URL.revokeObjectURL(url), 10_000);
    } catch (err) {
      setError(
        err instanceof Error ? err.message : "Could not build the report",
      );
    } finally {
      setDownloading(null);
    }
  }

  const rate = (metric: string) =>
    result?.summary.find((row) => row.metric === metric)?.percentage;

  // Search by chart name, as on the Accuracy tab, and the column filters; the status
  // pills count what they leave.
  const searched = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const rows = result?.rows ?? [];
    return rows.filter(
      (row) =>
        (!needle || row.pdfId.toLowerCase().includes(needle)) &&
        passesFieldFilters(row, fieldFilters) &&
        passesValueFilters(row, valueFilters, valueSides, tokens),
    );
  }, [result, query, fieldFilters, valueFilters, valueSides, tokens]);
  const fieldFilterCount = new Set([...Object.keys(fieldFilters), ...Object.keys(valueFilters)]).size;
  // The column the filter bar works on, and that column's values in the results.
  const filterField = view.filterField && result?.fields.includes(view.filterField) ? view.filterField : "";
  const fieldValues = useMemo(() => {
    if (!filterField || !result) return [] as string[];
    const seen = new Set<string>();
    for (const row of result.rows) {
      for (const v of rowValues(row, filterField, valueSides[filterField] ?? "either", tokens)) seen.add(v);
    }
    const values = [...seen].filter((v) => v !== BLANK_VALUE).sort((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    return seen.has(BLANK_VALUE) ? [...values, BLANK_VALUE] : values;
  }, [result, filterField, valueSides, tokens]);
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
        { id: "CORRECT", label: "Correct", count: statusCount("CORRECT") },
        ...(statusCount("UNVALIDATED") > 0
          ? [{ id: "UNVALIDATED" as const, label: "Unvalidated", count: statusCount("UNVALIDATED") }]
          : []),
        ...(statusCount("EXCLUDED") > 0
          ? [{ id: "EXCLUDED" as const, label: "Excluded (Blank/Junk)", count: statusCount("EXCLUDED") }]
          : []),
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
          Up to three sources, matched on Chart and Page Number: any two are
          enough. Every pair given is compared, and with all three, All 3 (a
          field matches only where every source agrees). Nothing is stored.
        </p>
        <div className="excel-compare-picks">
          <InputCard title="QA Ground Truth" extra={<ColumnsInfo />}>
            <FilePick
              hint="QA ground truth file (CSV or Excel) — click or drop; optional"
              file={qa}
              onFile={pick("qa", setQa)}
            />
          </InputCard>
          <InputCard
            title="Client Ground Truth"
            extra={
              <SourceToggle
                label="Client ground truth source"
                value={groundSource}
                onChange={chooseSource(setGroundSource)}
              />
            }
          >
            {groundSource === "upload" ? (
              <FilePick
                hint="Client ground truth file (CSV or Excel) — click or drop"
                file={ground}
                onFile={pick("ground", setGround)}
              />
            ) : groundSource === "db" ? (
              <DatabasePick
                title={
                  readGroundTruthSource() === "bulk_upload"
                    ? "Ground Truth — latest bulk upload per page"
                    : "Ground Truth — latest per page"
                }
              />
            ) : (
              <NotUsedPick />
            )}
          </InputCard>
          <InputCard
            title="Pipeline Output"
            extra={
              <SourceToggle
                label="Pipeline output source"
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
            ) : systemSource === "db" ? (
              <DatabasePick title="Pipeline Output" />
            ) : (
              <NotUsedPick />
            )}
          </InputCard>
        </div>
        <p className="accuracy-note excel-compare-plan" role="status">
          {sourceCount < 2
            ? "Choose at least two sources."
            : uploadMissing
              ? "Upload the file for each source set to Upload file."
              : `Compares: ${planned.map((key) => COMPARISON_LABELS[key]).join(", ")}.`}
        </p>
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
          {comparing ? (
            <button type="button" className="landing-file-viewer-btn excel-compare-stop" onClick={stopCompare}>
              <Square size={13} aria-hidden="true" />
              Stop
            </button>
          ) : null}
          {bundle && !comparing ? (
            <>
              <button
                type="button"
                className="landing-file-viewer-btn"
                disabled={downloading !== null}
                onClick={() => runDownload("xlsx")}
              >
                {downloading === "xlsx" ? (
                  <Loader2
                    size={15}
                    className="accuracy-calculating-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <Download size={15} aria-hidden="true" />
                )}
                {downloading === "xlsx" ? "Building Excel Report…" : "Download Excel Report"}
              </button>
              <button
                type="button"
                className="landing-file-viewer-btn"
                disabled={downloading !== null}
                onClick={() => runDownload("pdf")}
              >
                {downloading === "pdf" ? (
                  <Loader2
                    size={15}
                    className="accuracy-calculating-spin"
                    aria-hidden="true"
                  />
                ) : (
                  <FileText size={15} aria-hidden="true" />
                )}
                {downloading === "pdf" ? "Building Master Summary…" : "Download Master Summary"}
              </button>
            </>
          ) : null}
        </div>
      </section>

      <p className="accuracy-note excel-compare-downloading" role="status" aria-live="polite">
        {downloading
          ? `Building the ${downloading === "pdf" ? "Master Summary (PDF)" : "Excel report"}… ${downloadSeconds}s` +
            (bundle?.reportId
              ? ""
              : " — this comparison was stopped part-way, so it is compared again in full first; that can take a few minutes.")
          : ""}
      </p>
      {error ? <div className="error-banner">{error}</div> : null}
      {stopped && bundle ? (
        <p className="accuracy-note excel-compare-stopped">
          Stopped after {stopped.loaded} of {stopped.total} charts. Counts and results cover those charts only;
          Compare Files runs it again in full.
        </p>
      ) : null}

      {bundle && bundle.order.length > 0 ? (
        <section className="accuracy-section excel-compare-views" aria-label="Comparison to show">
          <h2>Comparison</h2>
          <div className="excel-compare-filters excel-compare-view-toggle" role="radiogroup" aria-label="Comparison to show">
            {[...bundle.order, ...(bundle.order.length > 1 ? (["display_all"] as const) : [])].map((key) => (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={activeKey === key}
                className={`excel-compare-filter${activeKey === key ? " active" : ""}`}
                onClick={() => setViewKey(key)}
              >
                {key === "display_all" ? "Display all" : COMPARISON_LABELS[key]}
              </button>
            ))}
          </div>
          {result ? (
            <p className="accuracy-note">
              {result.key === "all"
                ? "QA's pages; a field matches only where QA, Client and Pipeline all agree (every pair matches). " +
                  "A page missing from Client or Pipeline is Missing."
                : `${result.sides[0]} (base) against ${result.sides[1]}.`}
            </p>
          ) : null}
        </section>
      ) : null}

      {bundle && activeKey === "display_all" ? <DisplayAll bundle={bundle} onPick={setViewKey} /> : null}

      {result ? (
        <>
          <section className="accuracy-section" aria-label="Comparison summary">
            <h2>Summary — {result.label}</h2>
            <div className="excel-compare-stats">
              {[
                {
                  label: "Records",
                  items: [
                    [shortSides[0].toLowerCase(), result.groundCount],
                    [result.key === "all" ? "in all 3" : shortSides[1].toLowerCase(), result.systemCount],
                    [
                      "matched",
                      result.matchedCount ??
                        result.correctCount + result.errorCount + result.unvalidatedCount + (result.excludedCount ?? 0),
                    ],
                    ...((result.excludedCount ?? 0) > 0
                      ? [
                          ["excluded (blank/junk)", result.excludedCount ?? 0],
                          ["evaluated", result.evaluatedCount ?? result.correctCount + result.errorCount + result.unvalidatedCount],
                        ]
                      : []),
                  ],
                },
                ...(result.groundCharts !== undefined
                  ? [
                      {
                        label: "Charts",
                        items: [
                          [shortSides[0].toLowerCase(), result.groundCharts],
                          [result.key === "all" ? "in all 3" : shortSides[1].toLowerCase(), result.systemCharts ?? 0],
                          ["compared", result.chartsCompared ?? 0],
                        ],
                      },
                    ]
                  : []),
                {
                  label: "Outcome",
                  items: [
                    ["exact matches", result.correctCount],
                    ["field errors", result.errorCount],
                    ["missing", result.missingCount],
                    ...(result.unvalidatedCount > 0
                      ? [["unvalidated", result.unvalidatedCount]]
                      : []),
                  ],
                },
                {
                  label: "Rates",
                  items: [
                    ["exact match rate", percent(rate("Match Percentage"))],
                    ...((result.excludedCount ?? 0) > 0
                      ? [["evaluated accuracy", percent(rate("Evaluated Accuracy Rate"))]]
                      : []),
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
            {result.fieldErrors && result.fieldErrors.length > 0 ? (
              <div className="excel-compare-fields">
                <span className="excel-compare-stat-group-label">By field</span>
                <ul>
                  {result.fieldErrors.map((entry) => {
                    // Accuracy = evaluated pages (Blank/Blank and Junk/Junk left out) that agreed
                    // on the field, as in the Master Summary.
                    const accuracy = 100 - entry.percentage;
                    const tone = accuracy >= 95 ? "good" : accuracy >= 80 ? "fair" : "poor";
                    return (
                      <li key={entry.field}>
                        <span className="excel-compare-field-name">{entry.field}</span>
                        <span className="excel-compare-field-bar" aria-hidden="true">
                          <span className={`is-${tone}`} style={{ width: `${accuracy}%` }} />
                        </span>
                        <span className="excel-compare-field-value">
                          {accuracy.toFixed(2)}%
                          <span>
                            {entry.count} mismatch{entry.count === 1 ? "" : "es"}
                          </span>
                        </span>
                      </li>
                    );
                  })}
                </ul>
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
              {/* 2026-11-05 (user): pick a column, then Match / No match and its values. */}
              <span className="excel-compare-value-filter">
                <select
                  className={`excel-compare-col-filter${filterField ? " is-active" : ""}`}
                  value={filterField}
                  onChange={(e) => setView({ filterField: e.target.value })}
                  aria-label="Column to filter"
                >
                  <option value="">Filter a column…</option>
                  {result.fields.map((field) => (
                    <option key={field} value={field}>
                      {field}
                      {fieldFilters[field] || valueFilters[field]?.length ? " •" : ""}
                    </option>
                  ))}
                </select>
                {filterField ? (
                  <>
                    <select
                      className={`excel-compare-col-filter${fieldFilters[filterField] ? " is-active" : ""}`}
                      value={fieldFilters[filterField] ?? ""}
                      onChange={(e) => setFieldFilter(filterField, e.target.value as FieldFilter | "")}
                      aria-label={`Filter ${filterField} by match`}
                    >
                      <option value="">Match: All</option>
                      <option value="match">Match</option>
                      <option value="mismatch">No match</option>
                    </select>
                    <select
                      className={`excel-compare-col-filter${(valueSides[filterField] ?? "either") !== "either" ? " is-active" : ""}`}
                      value={valueSides[filterField] ?? "either"}
                      onChange={(e) =>
                        setView({ valueSides: { ...valueSides, [filterField]: e.target.value as ValueSide }, page: 1 })
                      }
                      aria-label={`Filter ${filterField} values on`}
                    >
                      <option value="either">{shortSides.join(" or ")}</option>
                      {shortSides.map((name, index) => (
                        <option key={name} value={SIDE_IDS[index]}>
                          {name}
                        </option>
                      ))}
                    </select>
                    <ColumnFilterButton
                      label={`${filterField} values`}
                      active={Boolean(valueFilters[filterField]?.length)}
                      icon={<FilterIcon size={12} aria-hidden="true" />}
                      align="start"
                      width={280}
                      text={
                        <span className="top-filter-text">
                          <span className="top-filter-label">Values:</span>{" "}
                          {valueFilters[filterField]?.length ? (
                            <span className="top-filter-count">{valueFilters[filterField].length}</span>
                          ) : (
                            "All"
                          )}
                        </span>
                      }
                    >
                      {() => (
                        <CheckListFilter
                          options={fieldValues}
                          selected={valueFilters[filterField] ?? []}
                          onChange={(next) => setValueFilter(filterField, next)}
                          formatOption={(v) => v}
                        />
                      )}
                    </ColumnFilterButton>
                  </>
                ) : null}
              </span>
              {fieldFilterCount > 0 ? (
                <button
                  type="button"
                  className="excel-compare-filter excel-compare-clear-columns"
                  onClick={() => setView({ fieldFilters: {}, valueFilters: {}, valueSides: {}, page: 1 })}
                >
                  <X size={12} aria-hidden="true" />
                  Clear column filters <span>{fieldFilterCount}</span>
                </button>
              ) : null}
            </div>
            <div className="accuracy-table-scroll">
              <table className="landing-history-table accuracy-table excel-compare-table">
                <thead>
                  <tr>
                    <th scope="col">Chart</th>
                    <th scope="col">Last Run At</th>
                    <th scope="col">Page</th>
                    <th scope="col">Source</th>
                    {result.fields.map((field) => (
                      <th key={field} scope="col">
                        <span className="excel-compare-col-head">
                          {field}
                          <select
                            className={`excel-compare-col-filter${fieldFilters[field] ? " is-active" : ""}`}
                            value={fieldFilters[field] ?? ""}
                            onChange={(e) => setFieldFilter(field, e.target.value as FieldFilter | "")}
                            aria-label={`Filter ${field} by match`}
                          >
                            <option value="">All</option>
                            <option value="match">Match</option>
                            <option value="mismatch">No match</option>
                          </select>
                        </span>
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
                        colSpan={result.fields.length + 6}
                        className="accuracy-empty"
                      >
                        {fieldFilterCount > 0
                          ? "No compared pages match these column filters."
                          : "No records match."}
                      </td>
                    </tr>
                  ) : null}
                  {pageRows.map((row, index) => {
                    const key = `${row.pdfId}:${row.pageNumber}:${row.status}:${index}`;
                    // A record with nothing to tell apart is one row: every value agrees.
                    const sides = oneRow(row, result.fields, tokens) ? [0] : tokens.map((_token, index) => index);
                    const span = sides.length > 1 ? sides.length : undefined;
                    return sides.map((side) => (
                      <tr
                        key={`${key}:${side}`}
                        className={`excel-compare-row-${side === 0 ? "gt" : "algo"}`}
                      >
                        {side === 0 ? (
                          <>
                            <th scope="rowgroup" rowSpan={span}>
                              <a
                                className="accuracy-chart-link"
                                href={chartHref(row.pdfId, rowPage(row.pageNumber))}
                                onClick={(event) => {
                                  // Cmd / Ctrl / Shift / middle click keep the browser's new-tab behaviour.
                                  if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                                  event.preventDefault();
                                  saveChartOrder("manual-review", chartOrder);
                                  onOpenChart(row.pdfId, rowPage(row.pageNumber));
                                }}
                              >
                                {row.pdfId}
                              </a>
                              <button
                                type="button"
                                className="accuracy-chart-newtab"
                                title="Open in new tab"
                                aria-label={`Open ${row.pdfId} page ${row.pageNumber} in a new tab`}
                                // window.open (not a target=_blank link) so the new tab keeps this tab's sign-in.
                                onClick={() => {
                                  saveChartOrder("manual-review", chartOrder);
                                  window.open(chartHref(row.pdfId, rowPage(row.pageNumber)), "_blank");
                                }}
                              >
                                <ExternalLink size={12} aria-hidden="true" />
                              </button>
                            </th>
                            <td rowSpan={span} className="landing-col-updated accuracy-col-date">
                              <DateLines iso={bundle?.lastRun[row.pdfId]} />
                            </td>
                            <td rowSpan={span}>{row.pageNumber}</td>
                          </>
                        ) : null}
                        <td className="excel-compare-source">
                          {span ? shortSides[side] : shortSides.join(" = ")}
                        </td>
                        {result.fields.map((field) => {
                          const mismatched = row.mismatched.includes(field);
                          // A value both sides agree on is shown once, across both rows.
                          const agreed =
                            !mismatched &&
                            span !== undefined &&
                            (row.status === "ERROR" || row.status === "CORRECT") &&
                            !splitDisplay(row.values[field] ?? "", tokens);
                          if (agreed && side > 0) return null;
                          return (
                            <td
                              key={field}
                              rowSpan={agreed ? span : undefined}
                              className={mismatched ? "excel-compare-mismatch" : undefined}
                            >
                              {sideValue(row, field, side, tokens) || "—"}
                            </td>
                          );
                        })}
                        {side === 0 ? (
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
