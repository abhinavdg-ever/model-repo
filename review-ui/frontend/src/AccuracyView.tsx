import { useEffect, useMemo, useState } from "react";
import { ArrowLeft, ChevronLeft, ChevronRight, Info, Loader2, Search } from "lucide-react";
import { getAccuracyReport } from "./api";
import {
  METRICS,
  MIN_LAST_RUN,
  MIN_LAST_RUN_LABEL,
  accuracyPercent,
  formatClassAccuracy,
  formatDistribution,
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

/** An ⓘ that shows what a number means: on hover, and on click / tap. */
function InfoTip({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  return (
    <span className="accuracy-info">
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

/** Match / Partial / No Match on three lines, so every metric column fits. */
function StackedTally({ tally }: { tally: Tally }) {
  if (tally.scored === 0) return <>—</>;
  return (
    <span className="accuracy-tally">
      <span>Match {tally.yes}</span>
      <span>Partial {tally.maybe}</span>
      <span>No Match {tally.no}</span>
    </span>
  );
}

const SCORE_INFO =
  "Accuracy score = (Match + 0.5 × Partial) / Total, where Total = Match + Partial + No Match.";
const YES_INFO =
  "Pages the ground truth calls blank or junk, scored like the donut: " +
  "Match = the pipeline found it with the same type (blank, or junk), " +
  "Partial = found it as the other type, No Match = kept it as a real page. " +
  SCORE_INFO.replace("Accuracy score", "Score");
const NON_ZERO_INFO =
  "Pages the ground truth says are rotated (90, 180 or 270), scored on the " +
  "stored text orientation: Match = same angle, No Match = any other. " +
  "Most pages are upright, so this is the figure that shows whether turned pages are caught.";
const RATES_INFO =
  "For the Yes class. Recall = TP / (TP + FN): share of the real Yes pages the pipeline caught. " +
  "Precision = TP / (TP + FP): share of the pipeline's Yes calls that were right. " +
  "Accuracy = (TP + TN) / all pages: share of Yes / No calls that were right.";

function Donut({
  label,
  detail,
  tally,
  rates,
  ratesLabel,
  yesTally,
  yesLabel = "Accuracy of Yes",
  yesInfo = YES_INFO,
}: {
  label: string;
  detail: string;
  tally: Tally;
  rates?: ClassRates;
  /** The positive ("Yes") class the precision / recall are for. */
  ratesLabel?: string;
  /** The same score on one slice of the pages (the ground-truth "Yes" pages). */
  yesTally?: Tally;
  yesLabel?: string;
  yesInfo?: string;
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
      <svg className="accuracy-donut" viewBox="0 0 100 100" role="img" aria-label={rates ? `${label} ${percentLabel(tally)}. ${formatDistribution(tally)}. Precision ${formatPrecision(rates)}. Recall ${formatRecall(rates)}` : `${label} ${percentLabel(tally)}. ${formatDistribution(tally)}`}>
        <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
        {arcs}
        <text className="accuracy-donut-pct" x="50" y="54">
          {percentLabel(tally)}
        </text>
      </svg>
      <figcaption>
        {label} <InfoTip text={`${detail} ${SCORE_INFO}`} />
      </figcaption>
      <p>{formatDistribution(tally)}</p>
      {rates ? (
        <p className="accuracy-rates">
          {ratesLabel ? (
            <>
              <span className="accuracy-rates-class">
                {ratesLabel} <InfoTip text={RATES_INFO} />
              </span>
              <br />
            </>
          ) : null}
          Recall {formatRecall(rates)}
          <br />
          Precision {formatPrecision(rates)}
          <br />
          Accuracy {formatClassAccuracy(rates)}
        </p>
      ) : null}
      {yesTally ? (
        <p className="accuracy-rates">
          <span className="accuracy-rates-class">
            {yesLabel} {percentLabel(yesTally)} <InfoTip text={yesInfo} />
          </span>
          <br />
          {formatDistribution(yesTally)}
        </p>
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

const CHARTS_PER_PAGE = 15;

export default function AccuracyView({ onBack, onOpenChart }: Props) {
  const [rows, setRows] = useState<ChartAccuracy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);

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

  // "By chart": search by file / chart name, 15 per page. The donuts stay on all files.
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return needle ? rows.filter((row) => row.chartName.toLowerCase().includes(needle)) : rows;
  }, [rows, query]);
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
  const blankJunkYes = useMemo(() => sumTallies(rows.map((row) => row.blankJunkYes)), [rows]);
  const orientationNonZero = useMemo(
    () => sumTallies(rows.map((row) => row.orientationNonZero)),
    [rows],
  );
  const orientationRates = useMemo(
    () => sumRates(rows.map((row) => row.orientationRates)),
    [rows],
  );
  /** Recall / precision / accuracy of the "Yes" class, across all files. */
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
      ? "Member on page = Yes"
      : id === "orientation"
        ? "Rotated (not 0°) = Yes"
        : "Blank / Junk = Yes";

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
                detail="A page matches when every metric that applies matches (the 6 other than Orientation, or 5 on a blank / junk page, where page type is not scored), is partial at 3 or more, and misses at 0 to 2. Orientation is scored on its own card only"
                tally={overall}
              />
              {METRICS.map((metric) => (
                <Donut
                  key={metric.id}
                  label={metric.label}
                  detail={metric.rule}
                  tally={overallByMetric[metric.id]}
                  rates={ratesFor(metric.id)}
                  ratesLabel={ratesLabelFor(metric.id)}
                  yesTally={
                    metric.id === "blankJunk"
                      ? blankJunkYes
                      : metric.id === "orientation"
                        ? orientationNonZero
                        : undefined
                  }
                  yesLabel={metric.id === "orientation" ? "Accuracy of non-zero" : undefined}
                  yesInfo={metric.id === "orientation" ? NON_ZERO_INFO : undefined}
                />
              ))}
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
                  <th scope="col">
                    Chart <InfoTip text="The file (chart). Click it to open the file in the viewer." />
                  </th>
                  <th scope="col">
                    Last Run At <InfoTip text={`When the pipeline last ran this file. Only runs on or after ${MIN_LAST_RUN_LABEL} are shown.`} />
                  </th>
                  <th scope="col">
                    Last Verified At <InfoTip text="When ground truth was last saved for this file." />
                  </th>
                  <th scope="col">
                    Pages <InfoTip text="Pages of this file compared (pages that have ground truth)." />
                  </th>
                  {METRICS.map((metric) => (
                    <th key={metric.id} scope="col">
                      {metric.label} <InfoTip text={metric.rule} />
                    </th>
                  ))}
                  <th scope="col">
                    Accuracy <InfoTip text={`Overall page score for this file. ${SCORE_INFO}`} />
                  </th>
                </tr>
              </thead>
              <tbody>
                {pageRows.length === 0 ? (
                  <tr>
                    <td colSpan={METRICS.length + 5} className="accuracy-empty">
                      No file matches “{query.trim()}”.
                    </td>
                  </tr>
                ) : null}
                {pageRows.map((row) => (
                  <tr key={row.chartId}>
                    <th scope="row">
                      <button
                        type="button"
                        className="accuracy-chart-link"
                        onClick={() => onOpenChart(row.chartId)}
                      >
                        {row.chartName}
                      </button>
                    </th>
                    <td className="landing-col-updated">{fmtUpdated(row.lastUpdatedAt)}</td>
                    <td className="landing-col-updated">{fmtUpdated(row.lastVerifiedAt)}</td>
                    <td>{row.overall.scored}</td>
                    {METRICS.map((metric) => (
                      <td key={metric.id}>
                        <StackedTally tally={row.metrics[metric.id]} />
                      </td>
                    ))}
                    <td>
                      <strong>{percentLabel(row.overall)}</strong>
                      <span className="accuracy-chart-ratio">
                        <StackedTally tally={row.overall} />
                      </span>
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
