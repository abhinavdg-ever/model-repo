import { useEffect, useMemo, useState } from "react";
import { ArrowLeft } from "lucide-react";
import { getFolderImaging, listFolders } from "./api";
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

export default function AccuracyView({ onBack, onOpenChart }: Props) {
  const [rows, setRows] = useState<ChartAccuracy[]>([]);
  const [loading, setLoading] = useState(true);
  const [progress, setProgress] = useState({ done: 0, total: 0 });
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const list = await listFolders({ sort: "filename", sort_dir: "asc" });
        if (cancelled) return;
        const labelled = list.items.filter((folder) => folder.ground_truth_available);
        setProgress({ done: 0, total: labelled.length });
        const scored: ChartAccuracy[] = [];
        for (const folder of labelled) {
          if (cancelled) return;
          try {
            const doc = await getFolderImaging(folder.id);
            const chart = scoreChart(folder.id, folder.name, doc);
            if (chart.overall.scored > 0) scored.push(chart);
          } catch {
            /* a chart that fails to load is left out of the totals */
          }
          if (!cancelled) {
            setProgress((current) => ({ ...current, done: current.done + 1 }));
          }
        }
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
            <h1>Accuracy</h1>
            <p>
              Page-level match against ground truth, across charts with ground truth. Only the
              fields that were labelled are scored.
            </p>
          </div>
        </div>
      </div>

      {error ? <div className="error-banner">{error}</div> : null}

      {loading ? (
        <p className="accuracy-status">
          Scoring charts{progress.total > 0 ? ` ${progress.done}/${progress.total}` : "…"}
        </p>
      ) : rows.length === 0 ? (
        <p className="accuracy-status">No pages with ground truth to score yet.</p>
      ) : (
        <>
          <div className="accuracy-overall" aria-label="Overall accuracy">
            <span className="accuracy-overall-value">{percentLabel(overall)}</span>
            <span className="accuracy-overall-detail">
              {formatRatio(overall)} page checks correct
              <span> · {overall.correct} correct · {overall.wrong} wrong</span>
            </span>
          </div>

          <section className="accuracy-section" aria-label="Accuracy by metric">
            <h2>By metric</h2>
            <table className="imaging-summary-table accuracy-table">
              <thead>
                <tr>
                  <th scope="col">Metric</th>
                  <th scope="col">Rule</th>
                  <th scope="col">Correct</th>
                  <th scope="col">Wrong</th>
                  <th scope="col">Pages</th>
                  <th scope="col">Accuracy</th>
                </tr>
              </thead>
              <tbody>
                {METRICS.map((metric) => {
                  const tally = overallByMetric[metric.id];
                  return (
                    <tr key={metric.id}>
                      <th scope="row">{metric.label}</th>
                      <td>{metric.rule}</td>
                      <td>{tally.scored === 0 ? "—" : tally.correct}</td>
                      <td>{tally.scored === 0 ? "—" : tally.wrong}</td>
                      <td>{formatRatio(tally)}</td>
                      <td>{percentLabel(tally)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </section>

          <section className="accuracy-section" aria-label="Accuracy by chart">
            <h2>By chart</h2>
            <table className="imaging-summary-table accuracy-table">
              <thead>
                <tr>
                  <th scope="col">Chart</th>
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
          </section>
        </>
      )}
    </div>
  );
}
