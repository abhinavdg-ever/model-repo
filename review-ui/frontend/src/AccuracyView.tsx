import { Fragment, useEffect, useMemo, useState } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, ExternalLink, Info, Loader2, Search } from "lucide-react";
import { getAccuracyReport } from "./api";
import {
  LIVE_METRICS,
  METRICS,
  MIN_LAST_RUN,
  MIN_LAST_RUN_LABEL,
  accuracyPercent,
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
function DateLines({ iso }: { iso: string | null | undefined }) {
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

type SortKey = "name" | "run" | "verified";
type SortDir = "asc" | "desc";

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

function compareRows(a: ChartAccuracy, b: ChartAccuracy, key: SortKey, dir: SortDir): number {
  const sign = dir === "asc" ? 1 : -1;
  if (key === "name") return sign * a.chartName.localeCompare(b.chartName, undefined, { numeric: true });
  const ta = timeOf(key === "run" ? a.lastUpdatedAt : a.lastVerifiedAt);
  const tb = timeOf(key === "run" ? b.lastUpdatedAt : b.lastVerifiedAt);
  if (ta === null || tb === null) {
    if (ta === tb) return a.chartName.localeCompare(b.chartName, undefined, { numeric: true });
    return ta === null ? 1 : -1;
  }
  return sign * (ta - tb) || a.chartName.localeCompare(b.chartName, undefined, { numeric: true });
}

export default function AccuracyView({ onBack, onOpenChart, chartHref }: Props) {
  const [rows, setRows] = useState<ChartAccuracy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  // Newest run first, like the home page.
  const [sortKey, setSortKey] = useState<SortKey>("run");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  // Same behaviour as the home page: click again to flip; name starts A→Z, dates newest first.
  function toggleSort(key: SortKey) {
    if (sortKey === key) {
      setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    } else {
      setSortKey(key);
      setSortDir(key === "name" ? "asc" : "desc");
    }
    setPage(1);
  }

  function sortIndicator(key: SortKey): string {
    if (sortKey !== key) return "";
    return sortDir === "asc" ? " ↑" : " ↓";
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

  // "By chart": search by file / chart name, 10 per page. The donuts stay on all files.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    const matched = needle ? rows.filter((row) => row.chartName.toLowerCase().includes(needle)) : rows;
    return [...matched].sort((a, b) => compareRows(a, b, sortKey, sortDir));
  }, [rows, query, sortKey, sortDir]);
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
                  <th scope="col" aria-sort={sortKey === "name" ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "name" ? " active" : ""}`}
                      onClick={() => toggleSort("name")}
                    >
                      Chart{sortIndicator("name")}
                    </button>
                  </th>
                  <th scope="col" aria-sort={sortKey === "run" ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "run" ? " active" : ""}`}
                      onClick={() => toggleSort("run")}
                    >
                      Last Run At{sortIndicator("run")}
                    </button>
                  </th>
                  <th scope="col" aria-sort={sortKey === "verified" ? (sortDir === "asc" ? "ascending" : "descending") : "none"}>
                    <button
                      type="button"
                      className={`th-sort${sortKey === "verified" ? " active" : ""}`}
                      onClick={() => toggleSort("verified")}
                    >
                      Last Verified At{sortIndicator("verified")}
                    </button>
                  </th>
                  <th scope="col">
                    Pages
                  </th>
                  {LIVE_METRICS.map((metric) => (
                    <th key={metric.id} scope="col">
                      {metric.tableLabel ?? metric.label}
                    </th>
                  ))}
                  <th scope="col">
                    Overall
                  </th>
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 ? (
                  <tr>
                    <td colSpan={LIVE_METRICS.length + 5} className="accuracy-empty">
                      No file matches “{query.trim()}”.
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
                        onClick={() => window.open(chartHref(row.chartId), "_blank")}
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
                  {query.trim() ? ` (of ${rows.length})` : ""}
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
