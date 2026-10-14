import { Fragment, useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, Filter, Info, Loader2, Search } from "lucide-react";
import { getAccuracyReport } from "./api";
import { saveAccuracyOrder } from "./chartSequence";
import { useViewState } from "./viewState";
import {
  LIVE_METRICS,
  METRICS,
  MIN_LAST_RUN,
  MIN_LAST_RUN_LABEL,
  accuracyPercent,
  f1Percent,
  formatDistribution,
  formatF1,
  formatPrecision,
  formatRecall,
  scoreChart,
  sumRates,
  sumTallies,
  type ChartAccuracy,
  type ClassRates,
  type MetricId,
  type Tally,
} from "./accuracy";

type Props = {
  onBack: () => void;
  onOpenChart: (folderId: string) => void;
  /** The chart's URL, so it can open in a new tab. */
  chartHref: (folderId: string) => string;
};

function percentLabel(tally: Tally): string {
  const pct = accuracyPercent(tally);
  return pct == null ? "—" : `${pct}%`;
}

function fmtUpdated(iso: string | null | undefined): string {
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

/** Date on one line, time under it, so the column stays narrow. */
export function DateLines({ iso }: { iso: string | null | undefined }) {
  const text = fmtUpdated(iso);
  const cut = text.lastIndexOf(", ");
  if (cut < 0) return <>{text}</>;
  return (
    <>
      <span>{text.slice(0, cut)}</span>
      <span>{text.slice(cut + 2)}</span>
    </>
  );
}

/** An ⓘ that shows what a number means: on hover, and on click / tap. */
function InfoTip({ text, className }: { text: string; className?: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className={className ? `accuracy-info ${className}` : "accuracy-info"}>
      <button
        type="button"
        className="accuracy-info-btn"
        aria-label={text}
        aria-expanded={open}
        title={text}
        onClick={() => setOpen((v) => !v)}
        onBlur={() => setOpen(false)}
      >
        <Info size={13} aria-hidden="true" />
      </button>
      {open ? (
        <span className="accuracy-info-pop" role="tooltip">
          {text}
        </span>
      ) : null}
    </span>
  );
}

/** "Match n · Partial n · No Match n", wrapping only between the parts. */
function Distribution({ tally, binary = false }: { tally: Tally; binary?: boolean }) {
  return (
    <>
      {formatDistribution(tally, binary)
        .split(" · ")
        .map((part, index) => (
          <Fragment key={part}>
            {index > 0 ? " · " : null}
            <span>{part}</span>
          </Fragment>
        ))}
    </>
  );
}

/** Class 1 Precision / Recall / F1, label above value. */
function RatesGrid({ rates }: { rates: ClassRates }) {
  return (
    <dl className="accuracy-class-grid">
      {[
        ["Precision", formatPrecision(rates)],
        ["Recall", formatRecall(rates)],
        ["F1", formatF1(rates)],
      ].map(([name, value]) => (
        <div key={name}>
          <dt>{name}</dt>
          <dd>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One table cell: the donut card's numbers, compact. */
function CompactScore({
  tally,
  binary = false,
  rates,
}: {
  tally: Tally;
  binary?: boolean;
  rates?: ClassRates;
}) {
  if (tally.scored === 0) return <>—</>;
  return (
    <div className="accuracy-cell">
      <strong>{percentLabel(tally)}</strong>
      <p className="accuracy-distribution">
        <Distribution tally={tally} binary={binary} />
      </p>
      {rates ? <RatesGrid rates={rates} /> : null}
    </div>
  );
}

const SCORE_INFO =
  "Accuracy score = (Match + 0.5 × Partial) / Total, where Total = Match + Partial + No Match.";
const RATES_INFO =
  "Class 1 (the Yes pages) only. Precision = TP / (TP + FP): share of the pipeline's Class 1 calls that were right. " +
  "Recall = TP / (TP + FN): share of the real Class 1 pages the pipeline caught. " +
  "F1 = 2 × Precision × Recall / (Precision + Recall). " +
  "These matter because Class 1 pages are rare: the overall accuracy can be high while they are missed.";

function Donut({
  label,
  detail,
  tally,
  rates,
  ratesLabel,
  binary = false,
}: {
  label: string;
  detail: string;
  tally: Tally;
  /** Match or No Match only. */
  binary?: boolean;
  /** Class 1 counts: Precision, Recall and F1 are shown for them. */
  rates?: ClassRates;
  /** What Class 1 is ("Blank / Junk = Yes"). */
  ratesLabel?: string;
}) {
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const segments = [
    { key: "yes", count: tally.yes, tone: "accuracy-donut-yes" },
    { key: "maybe", count: tally.maybe, tone: "accuracy-donut-maybe" },
    { key: "no", count: tally.no, tone: "accuracy-donut-no" },
  ];
  let offset = 0;
  const arcs = segments.flatMap((segment) => {
    if (tally.scored === 0 || segment.count === 0) return [];
    const length = (segment.count / tally.scored) * circumference;
    const arc = (
      <circle
        key={segment.key}
        className={`accuracy-donut-seg ${segment.tone}`}
        cx="50"
        cy="50"
        r={radius}
        strokeDasharray={length >= circumference - 0.01 ? undefined : `${length} ${circumference - length}`}
        strokeDashoffset={-offset}
      />
    );
    offset += length;
    return [arc];
  });
  return (
    <figure className="accuracy-donut-card">
      <svg className="accuracy-donut" viewBox="0 0 100 100" role="img" aria-label={rates ? `${label} ${percentLabel(tally)}. ${formatDistribution(tally, binary)}. Precision ${formatPrecision(rates)}. Recall ${formatRecall(rates)}. F1 ${formatF1(rates)}` : `${label} ${percentLabel(tally)}. ${formatDistribution(tally, binary)}`}>
        <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
        {arcs}
        <text className="accuracy-donut-pct" x="50" y={tally.scored > 0 ? 51 : 54}>
          {percentLabel(tally)}
        </text>
        {tally.scored > 0 ? (
          <text className="accuracy-donut-caption" x="50" y="64">
            Accuracy
          </text>
        ) : null}
      </svg>
      <InfoTip
        className="accuracy-info-corner"
        text={[detail, SCORE_INFO, rates ? RATES_INFO : ""]
          .filter(Boolean)
          .join(" ")}
      />
      <figcaption>{label}</figcaption>
      <p className="accuracy-distribution">
        <Distribution tally={tally} binary={binary} />
      </p>
      {rates ? (
        <div className="accuracy-class">
          {ratesLabel ? <div className="accuracy-class-head">{ratesLabel}</div> : null}
          <RatesGrid rates={rates} />
        </div>
      ) : null}
    </figure>
  );
}

/** Shown while the report loads: the scoring reads every chart with ground truth. */
function CalculatingPanel() {
  const [seconds, setSeconds] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setSeconds((s) => s + 1), 1000);
    return () => window.clearInterval(timer);
  }, []);
  return (
    <div className="accuracy-calculating" role="status" aria-live="polite">
      <Loader2 className="accuracy-calculating-spin" size={28} aria-hidden="true" />
      <p className="accuracy-calculating-title">Please wait while we calculate accuracy…</p>
      <p className="accuracy-calculating-detail">
        Comparing every page that has ground truth with the pipeline&apos;s results.
        {seconds >= 3 ? ` ${seconds}s` : ""}
      </p>
    </div>
  );
}

const CHARTS_PER_PAGE = 10;

type SortKey = "name" | "run" | "verified" | "pages" | "overall" | MetricId;
type SortDir = "asc" | "desc";

/** A score column: one metric, or Overall. */
type ScoreColumn = MetricId | "overall";

/** Inclusive percent range; a row with no score is hidden while a range is set. */
type Range = { min: number; max: number };

/** Filters keyed by column, then by what is filtered: Accuracy, or F1 of Class Yes. */
type Filters = Partial<Record<ScoreColumn, { accuracy?: Range; f1?: Range }>>;

function columnTally(row: ChartAccuracy, column: ScoreColumn): Tally {
  return column === "overall" ? row.overall : row.metrics[column];
}

function inRange(value: number | null, range: Range | undefined): boolean {
  if (!range) return true;
  return value !== null && value >= range.min && value <= range.max;
}

function passesFilters(row: ChartAccuracy, filters: Filters): boolean {
  for (const [column, f] of Object.entries(filters) as [ScoreColumn, Filters[ScoreColumn]][]) {
    if (!f) continue;
    if (!inRange(accuracyPercent(columnTally(row, column)), f.accuracy)) return false;
    if (f.f1) {
      const rates = column === "overall" ? undefined : rowRatesFor(row, column);
      if (!inRange(rates ? f1Percent(rates) : null, f.f1)) return false;
    }
  }
  return true;
}

/** One 0–100 bar with a Min and a Max handle: two range inputs stacked on one
 * track, where only the handles take the pointer. */
function RangeSliders({
  label,
  range,
  onChange,
}: {
  label: string;
  range: Range;
  onChange: (range: Range) => void;
}) {
  return (
    <fieldset className="accuracy-filter-range">
      <legend>
        {label}: <strong>{range.min}%–{range.max}%</strong>
      </legend>
      <div className="accuracy-range">
        <div className="accuracy-range-track" aria-hidden="true">
          <div
            className="accuracy-range-fill"
            style={{ left: `${range.min}%`, right: `${100 - range.max}%` }}
          />
        </div>
        <input
          type="range"
          min={0}
          max={100}
          value={range.min}
          aria-label={`${label} minimum`}
          // When both handles meet at the top, the Min handle must stay grabbable.
          className={range.min > 50 ? "on-top" : undefined}
          onChange={(e) => onChange({ min: Math.min(Number(e.target.value), range.max), max: range.max })}
        />
        <input
          type="range"
          min={0}
          max={100}
          value={range.max}
          aria-label={`${label} maximum`}
          onChange={(e) => onChange({ min: range.min, max: Math.max(Number(e.target.value), range.min) })}
        />
      </div>
      <div className="accuracy-range-scale" aria-hidden="true">
        <span>0</span>
        <span>100</span>
      </div>
    </fieldset>
  );
}

const FULL_RANGE: Range = { min: 0, max: 100 };

/** The filter button at the right of a score column's header, and its slider popover.
 * The popover is fixed-position so the table's scroll box does not clip it. */
function ColumnFilter({
  label,
  value,
  f1Label,
  onChange,
}: {
  label: string;
  value: { accuracy?: Range; f1?: Range } | undefined;
  /** Set on a column with a Class Yes block: also filter on its F1. */
  f1Label?: string;
  onChange: (value: { accuracy?: Range; f1?: Range } | undefined) => void;
}) {
  const [anchor, setAnchor] = useState<{ top: number; left: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const active = Boolean(value?.accuracy || value?.f1);

  useEffect(() => {
    if (!anchor) return;
    const close = (event: Event) => {
      const target = event.target as Node;
      if (popoverRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      setAnchor(null);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setAnchor(null);
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    // Scrolling the table or page would leave the popover behind its column.
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", close);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [anchor]);

  function toggle() {
    if (anchor) {
      setAnchor(null);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    const width = 220;
    setAnchor({
      top: rect.bottom + 6,
      left: Math.max(8, Math.min(rect.right - width, window.innerWidth - width - 8)),
    });
  }

  function set(part: "accuracy" | "f1", range: Range) {
    const full = range.min === 0 && range.max === 100;
    const next = { ...value, [part]: full ? undefined : range };
    onChange(next.accuracy || next.f1 ? next : undefined);
  }

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className={`accuracy-filter-btn${active ? " active" : ""}`}
        title={`Filter ${label}`}
        aria-label={`Filter ${label}`}
        aria-expanded={anchor !== null}
        onClick={toggle}
      >
        <Filter size={11} aria-hidden="true" />
      </button>
      {anchor ? (
        <div
          ref={popoverRef}
          className="accuracy-filter-popover"
          style={{ top: anchor.top, left: anchor.left }}
          role="dialog"
          aria-label={`Filter ${label}`}
        >
          <RangeSliders label="Accuracy" range={value?.accuracy ?? FULL_RANGE} onChange={(r) => set("accuracy", r)} />
          {f1Label ? (
            <RangeSliders label={f1Label} range={value?.f1 ?? FULL_RANGE} onChange={(r) => set("f1", r)} />
          ) : null}
          <button type="button" className="accuracy-filter-clear" disabled={!active} onClick={() => onChange(undefined)}>
            Clear
          </button>
        </div>
      ) : null}
    </>
  );
}

/** Milliseconds for sorting; a missing date sorts last whichever way. */
function timeOf(value: string | null | undefined): number | null {
  const t = value ? new Date(value).getTime() : NaN;
  return Number.isNaN(t) ? null : t;
}

/** One file's Class 1 counts for a metric, where the metric has a Class 1. */
function rowRatesFor(row: ChartAccuracy, id: MetricId): ClassRates | undefined {
  if (id === "blankJunk") return row.blankJunkRates;
  if (id === "member") return row.memberRates;
  if (id === "orientation") return row.orientationRates;
  return undefined;
}

/** The number a column sorts on: a time, a page count or an accuracy percent. */
function sortValue(row: ChartAccuracy, key: Exclude<SortKey, "name">): number | null {
  if (key === "run") return timeOf(row.lastUpdatedAt);
  if (key === "verified") return timeOf(row.lastVerifiedAt);
  if (key === "pages") return row.overall.scored;
  return accuracyPercent(columnTally(row, key));
}

/** A missing value sorts last whichever way; ties fall back to the chart name. */
function compareRows(a: ChartAccuracy, b: ChartAccuracy, key: SortKey, dir: SortDir): number {
  const sign = dir === "asc" ? 1 : -1;
  const byName = a.chartName.localeCompare(b.chartName, undefined, { numeric: true });
  if (key === "name") return sign * byName;
  const va = sortValue(a, key);
  const vb = sortValue(b, key);
  if (va === null || vb === null) {
    if (va === vb) return byName;
    return va === null ? 1 : -1;
  }
  return sign * (va - vb) || byName;
}

export default function AccuracyView({ onBack, onOpenChart, chartHref }: Props) {
  const [rows, setRows] = useState<ChartAccuracy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Search, column filters, sort and page survive opening a chart and coming Back.
  // Newest run first, like the home page.
  const [view, setView] = useViewState<{
    query: string;
    page: number;
    sortKey: SortKey;
    sortDir: SortDir;
    filters: Filters;
  }>("accuracy", { query: "", page: 1, sortKey: "run", sortDir: "desc", filters: {} });
  const { query, page, sortKey, sortDir, filters } = view;
  const setQuery = (value: string) => setView({ query: value });
  const setPage = (value: number) => setView({ page: value });

  function setFilter(column: ScoreColumn, value: Filters[ScoreColumn]) {
    const next = { ...filters };
    if (value) next[column] = value;
    else delete next[column];
    setView({ filters: next, page: 1 });
  }

  // Same behaviour as the home page: click again to flip; name starts A→Z, numbers highest first.
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setView({ sortDir: sortDir === "asc" ? "desc" : "asc", page: 1 });
    } else {
      setView({ sortKey: key, sortDir: key === "name" ? "asc" : "desc", page: 1 });
    }
  }

  function sortIndicator(key: SortKey): string {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
  }

  function SortHeader({ sort, label, filter }: { sort: SortKey; label: string; filter?: ScoreColumn }) {
    const rates = filter && filter !== "overall" ? ratesFor(filter) : undefined;
    return (
      <th scope="col" aria-sort={sortKey === sort ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
        <span className="accuracy-th">
          <button
            type="button"
            className={`th-sort${sortKey === sort ? " active" : ""}`}
            onClick={() => toggleSort(sort)}
          >
            {label}
            {sortIndicator(sort)}
          </button>
          {filter ? (
            <ColumnFilter
              label={label}
              value={filters[filter]}
              f1Label={rates && filter !== "overall" ? `F1 (${ratesLabelFor(filter)})` : undefined}
              onChange={(value) => setFilter(filter, value)}
            />
          ) : null}
        </span>
      </th>
    );
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const report = await getAccuracyReport();
        if (cancelled) return;
        const scored: ChartAccuracy[] = [];
        for (const chart of report.charts) {
          if (!chart.document) continue;
          const row = scoreChart(chart.chartId, chart.chartName, chart.document);
          row.lastUpdatedAt = chart.lastUpdatedAt ?? null;
          row.lastVerifiedAt = chart.lastVerifiedAt ?? null;
          // Runs before MIN_LAST_RUN used older rules; leave them out.
          const ranAt = row.lastUpdatedAt ? new Date(row.lastUpdatedAt) : null;
          const recent = ranAt != null && !Number.isNaN(ranAt.getTime()) && ranAt >= new Date(`${MIN_LAST_RUN}T00:00:00+05:30`);
          if (row.overall.scored > 0 && recent) scored.push(row);
        }
        scored.sort((a, b) => a.chartName.localeCompare(b.chartName));
        if (!cancelled) setRows(scored);
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : "Could not load accuracy");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const overallByMetric = useMemo(() => {
    const out = {} as Record<MetricId, Tally>;
    for (const metric of METRICS) {
      out[metric.id] = sumTallies(rows.map((row) => row.metrics[metric.id]));
    }
    return out;
  }, [rows]);

  const overall = useMemo(() => sumTallies(rows.map((row) => row.overall)), [rows]);

  // "By chart": search by file / chart name and the column filters, 10 per page. The
  // donuts stay on all files.
  const filterCount = Object.keys(filters).length;
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = rows.filter(
      (row) => (!needle || row.chartName.toLowerCase().includes(needle)) && passesFilters(row, filters),
    );
    return matched.sort((a, b) => compareRows(a, b, sortKey, sortDir));
  }, [rows, query, filters, sortKey, sortDir]);
  const totalPages = Math.max(1, Math.ceil(filtered.length / CHARTS_PER_PAGE));
  const currentPage = Math.min(page, totalPages);
  const pageRows = filtered.slice(
    (currentPage - 1) * CHARTS_PER_PAGE,
    currentPage * CHARTS_PER_PAGE,
  );
  const rangeStart = filtered.length === 0 ? 0 : (currentPage - 1) * CHARTS_PER_PAGE + 1;
  const rangeEnd = Math.min(currentPage * CHARTS_PER_PAGE, filtered.length);
  const pageNumbers = useMemo(() => {
    const maxButtons = 7;
    if (totalPages <= maxButtons) return Array.from({ length: totalPages }, (_, i) => i + 1);
    const pages = new Set<number>([1, totalPages, currentPage]);
    for (let d = 1; pages.size < maxButtons - 1; d++) {
      if (currentPage - d >= 1) pages.add(currentPage - d);
      if (currentPage + d <= totalPages) pages.add(currentPage + d);
    }
    return [...pages].sort((a, b) => a - b);
  }, [currentPage, totalPages]);

  const blankJunkRates = useMemo(
    () => sumRates(rows.map((row) => row.blankJunkRates)),
    [rows],
  );
  const memberRates = useMemo(() => sumRates(rows.map((row) => row.memberRates)), [rows]);
  const orientationRates = useMemo(
    () => sumRates(rows.map((row) => row.orientationRates)),
    [rows],
  );
  /** Class 1 counts across all files. */
  const ratesFor = (id: MetricId) =>
    id === "blankJunk"
      ? blankJunkRates
      : id === "member"
        ? memberRates
        : id === "orientation"
          ? orientationRates
          : undefined;
  const ratesLabelFor = (id: MetricId) =>
    id === "member"
      ? "Member & DOB = Yes"
      : id === "orientation"
        ? "Rotated (not 0°) = Yes"
        : "Blank/Junk = Yes";

  return (
    <div className="workspace accuracy-view">
      <div className="workspace-header">
        <div className="workspace-header-start">
          <button type="button" className="back-btn" onClick={onBack}>
            <ArrowLeft size={15} aria-hidden="true" />
            Back
          </button>
          <div className="workspace-title">
            <h1>Accuracy View</h1>
          </div>
        </div>
      </div>

      {error ? <div className="error-banner">{error}</div> : null}

      {loading ? (
        <CalculatingPanel />
      ) : rows.length === 0 ? (
        <p className="accuracy-status">
          No pages with ground truth to score from runs on or after {MIN_LAST_RUN_LABEL}.
        </p>
      ) : (
        <>
          <section className="accuracy-section" aria-label="Accuracy by metric">
            <h2>By metric</h2>
            <p className="accuracy-compared">
              <strong>{rows.length}</strong> {rows.length === 1 ? "file" : "files"} compared ·{" "}
              <strong>{overall.scored}</strong> {overall.scored === 1 ? "page" : "pages"} compared{" "}
              <InfoTip text={`Files last run on or after ${MIN_LAST_RUN_LABEL} with at least one page that has ground truth. A page is compared when it has any ground-truth label.`} />
            </p>
            <p className="accuracy-note">
              Match counts as 1, Partial as 0.5, and No Match as 0. <InfoTip text={SCORE_INFO} />
            </p>
            <div className="accuracy-donuts">
              <Donut
                label="Overall"
                detail="Each page is judged on the metrics it has ground truth for (Orientation, and page type on a blank / junk page, are left out): all of them matching is a match, at least half is partial, fewer is a miss. A metric with no ground-truth label does not count against the page"
                tally={overall}
              />
              {METRICS.map((metric) =>
                metric.comingSoon ? (
                  <figure key={metric.id} className="accuracy-donut-card accuracy-coming-soon" aria-label={`${metric.label}: coming soon`}>
                    <svg className="accuracy-donut" viewBox="0 0 100 100" aria-hidden="true">
                      <circle className="accuracy-donut-track" cx="50" cy="50" r="36" />
                    </svg>
                    <figcaption>{metric.label}</figcaption>
                    <p>Coming soon</p>
                  </figure>
                ) : (
                <Donut
                  key={metric.id}
                  label={metric.label}
                  detail={metric.rule}
                  tally={overallByMetric[metric.id]}
                  rates={ratesFor(metric.id)}
                  ratesLabel={ratesLabelFor(metric.id)}
                  binary={metric.binary}
                />
                ),
              )}
            </div>
          </section>

          <section className="accuracy-section" aria-label="Accuracy by chart">
            <div className="accuracy-chart-head">
              <h2>By chart</h2>
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
                  aria-label="Search files"
                />
              </label>
            </div>
            <div className="accuracy-table-scroll">
            <table className="landing-history-table accuracy-table">
              <thead>
                <tr>
                  {SortHeader({ sort: "name", label: "Chart" })}
                  {SortHeader({ sort: "run", label: "Last Run At" })}
                  {SortHeader({ sort: "verified", label: "Last Verified At" })}
                  {SortHeader({ sort: "pages", label: "Pages" })}
                  {LIVE_METRICS.map((metric) => (
                    <Fragment key={metric.id}>
                      {SortHeader({ sort: metric.id, label: metric.tableLabel ?? metric.label, filter: metric.id })}
                    </Fragment>
                  ))}
                  {SortHeader({ sort: "overall", label: "Overall", filter: "overall" })}
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 ? (
                  <tr>
                    <td colSpan={LIVE_METRICS.length + 5} className="accuracy-empty">
                      {query.trim()
                        ? `No file matches “${query.trim()}”${filterCount ? " and the column filters" : ""}.`
                        : "No file matches the column filters."}
                    </td>
                  </tr>
                ) : null}
                {pageRows.map((row) => (
                  <tr key={row.chartId}>
                    <th scope="row">
                      <a
                        className="accuracy-chart-link"
                        href={chartHref(row.chartId)}
                        onClick={(event) => {
                          // Cmd / Ctrl / Shift / middle click keep the browser's new-tab behaviour.
                          if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
                          event.preventDefault();
                          saveAccuracyOrder(filtered.map((r) => ({ id: r.chartId, name: r.chartName })));
                          onOpenChart(row.chartId);
                        }}
                      >
                        {row.chartName}
                      </a>
                      <button
                        type="button"
                        className="accuracy-chart-newtab"
                        title="Open in new tab"
                        aria-label={`Open ${row.chartName} in a new tab`}
                        // window.open (not a target=_blank link) so the new tab keeps this tab's sign-in.
                        onClick={() => {
                          saveAccuracyOrder(filtered.map((r) => ({ id: r.chartId, name: r.chartName })));
                          window.open(chartHref(row.chartId), "_blank");
                        }}
                      >
                        <ExternalLink size={12} aria-hidden="true" />
                      </button>
                    </th>
                    <td className="landing-col-updated accuracy-col-date">
                      <DateLines iso={row.lastUpdatedAt} />
                    </td>
                    <td className="landing-col-updated accuracy-col-date">
                      <DateLines iso={row.lastVerifiedAt} />
                    </td>
                    <td>{row.overall.scored}</td>
                    {LIVE_METRICS.map((metric) => (
                      <td key={metric.id}>
                        <CompactScore
                          tally={row.metrics[metric.id]}
                          binary={metric.binary}
                          rates={rowRatesFor(row, metric.id)}
                        />
                      </td>
                    ))}
                    <td>
                      <CompactScore tally={row.overall} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
            {filtered.length > 0 ? (
              <div className="landing-pagination" aria-label="Files pagination">
                <span className="landing-pagination-meta">
                  {rangeStart}–{rangeEnd} of {filtered.length}
                  {query.trim() || filterCount ? ` (of ${rows.length})` : ""}
                </span>
                <div className="landing-pagination-controls">
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage <= 1}
                    onClick={() => setPage(Math.max(1, currentPage - 1))}
                    aria-label="Previous page"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  {pageNumbers.map((n, idx) => {
                    const prev = pageNumbers[idx - 1];
                    return (
                      <span key={n} className="landing-page-num-wrap">
                        {prev != null && n - prev > 1 ? (
                          <span className="landing-page-ellipsis">…</span>
                        ) : null}
                        <button
                          type="button"
                          className={`landing-page-num${n === currentPage ? " active" : ""}`}
                          onClick={() => setPage(n)}
                          aria-label={`Page ${n}`}
                          aria-current={n === currentPage ? "page" : undefined}
                        >
                          {n}
                        </button>
                      </span>
                    );
                  })}
                  <button
                    type="button"
                    className="landing-page-btn"
                    disabled={currentPage >= totalPages}
                    onClick={() => setPage(Math.min(totalPages, currentPage + 1))}
                    aria-label="Next page"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </div>
            ) : null}
          </section>
        </>
      )}
    </div>
  );
}
