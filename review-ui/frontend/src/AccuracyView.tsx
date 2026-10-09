import { useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { getAccuracyReport } from "./api";
import {
  METRICS,
  accuracyPercent,
  formatRatio,
  scoreChart,
  sumTallies,
  type ChartAccuracy,
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

function Donut({ label, detail, tally }: { label: string; detail: string; tally: Tally }) {
  const pct = accuracyPercent(tally);
  const shown = pct ?? 0;
  const radius = 36;
  const circumference = 2 * Math.PI * radius;
  const filled = tally.scored === 0 ? 0 : (shown / 100) * circumference;
  return (
    <figure className="accuracy-donut-card" title={detail}>
      <svg className="accuracy-donut" viewBox="0 0 100 100" role="img" aria-label={`${label} ${percentLabel(tally)}`}>
        <circle className="accuracy-donut-track" cx="50" cy="50" r={radius} />
        {filled > 0 ? (
          <circle
            className="accuracy-donut-value"
            cx="50"
            cy="50"
            r={radius}
            strokeDasharray={`${filled} ${circumference - filled}`}
          />
        ) : null}
        <text className="accuracy-donut-pct" x="50" y="54">
          {percentLabel(tally)}
        </text>
      </svg>
      <figcaption>{label}</figcaption>
      <p>{formatRatio(tally)}</p>
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

  const overall = useMemo(
    () => sumTallies(METRICS.map((metric) => overallByMetric[metric.id])),
    [overallByMetric],
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
            <div className="accuracy-donuts">
              <Donut label="Overall" detail="Every labelled check" tally={overall} />
              {METRICS.map((metric) => (
                <Donut
                  key={metric.id}
                  label={metric.label}
                  detail={metric.rule}
                  tally={overallByMetric[metric.id]}
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
                      <td key={metric.id}>{formatRatio(row.metrics[metric.id])}</td>
                    ))}
                    <td>
                      {percentLabel(row.overall)}
                      <span className="accuracy-chart-ratio"> {formatRatio(row.overall)}</span>
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
