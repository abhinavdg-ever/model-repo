import { useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { getAccuracyReport } from "./api";
import {
  METRICS,
  accuracyPercent,
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

function Donut({
  label,
  detail,
  tally,
  rates,
}: {
  label: string;
  detail: string;
  tally: Tally;
  rates?: ClassRates;
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
    <figure className="accuracy-donut-card" title={detail}>
      <svg className="accuracy-donut" viewBox="0 0 100 100" role="img" aria-label={rates ? `${label} ${percentLabel(tally)}. ${formatDistribution(tally)}. Precision ${formatPrecision(rates)}. Recall ${formatRecall(rates)}` : `${label} ${percentLabel(tally)}. ${formatDistribution(tally)}`}>
        <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
        {arcs}
        <text className="accuracy-donut-pct" x="50" y="54">
          {percentLabel(tally)}
        </text>
      </svg>
      <figcaption>{label}</figcaption>
      <p>{formatDistribution(tally)}</p>
      {rates ? (
        <p className="accuracy-rates">
          Precision {formatPrecision(rates)}
          <br />
          Recall {formatRecall(rates)}
        </p>
      ) : null}
    </figure>
  );
}

export default function AccuracyView({ onBack, onOpenChart }: Props) {
  const [rows, setRows] = useState<ChartAccuracy[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
          if (row.overall.scored > 0) scored.push(row);
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

  const blankJunkRates = useMemo(
    () => sumRates(rows.map((row) => row.blankJunkRates)),
    [rows],
  );

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
        <p className="accuracy-status">Loading accuracy…</p>
      ) : rows.length === 0 ? (
        <p className="accuracy-status">No pages with ground truth to score yet.</p>
      ) : (
        <>
          <section className="accuracy-section" aria-label="Accuracy by metric">
            <h2>By metric</h2>
            <p className="accuracy-note">Yes counts as a match, May be as half, and No as a miss.</p>
            <div className="accuracy-donuts">
              <Donut
                label="Overall"
                detail="A page matches when all 6 metrics match, is partial at 3 to 5, and misses at 0 to 2"
                tally={overall}
              />
              {METRICS.map((metric) => (
                <Donut
                  key={metric.id}
                  label={metric.label}
                  detail={metric.rule}
                  tally={overallByMetric[metric.id]}
                  rates={metric.id === "blankJunk" ? blankJunkRates : undefined}
                />
              ))}
            </div>
          </section>

          <section className="accuracy-section" aria-label="Accuracy by chart">
            <h2>By chart</h2>
            <div className="accuracy-table-scroll">
            <table className="landing-history-table accuracy-table">
              <thead>
                <tr>
                  <th scope="col">Chart</th>
                  <th scope="col">Last Run At</th>
                  <th scope="col">Last Verified At</th>
                  {METRICS.map((metric) => (
                    <th key={metric.id} scope="col">
                      {metric.label}
                    </th>
                  ))}
                  <th scope="col">Accuracy</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
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
                    {METRICS.map((metric) => (
                      <td key={metric.id}>{formatDistribution(row.metrics[metric.id])}</td>
                    ))}
                    <td>
                      {percentLabel(row.overall)}
                      <span className="accuracy-chart-ratio"> {formatDistribution(row.overall)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
