/** The chart's Details (2026-11-19, user): one "Details" button styled like OCR / Imaging, left
 * of them, opening a card with four sections:
 *   Chart       -- cluster, run / batch, scope, measure, source, retrieval;
 *   Accuracy    -- overall and per metric;
 *   Confidence  -- pages High / Medium / Low per metric, and for the page as a whole;
 *   Confidence x accuracy -- a cross tab of each page's confidence level against its accuracy. */
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Info, X } from "lucide-react";

import { LIVE_METRICS, accuracyPercent, scoreChart } from "./accuracy";
import type { FolderDetail, ImagingDocumentResponse } from "./api";
import {
  CONFIDENCE_FIELDS,
  chartConfidence,
  levelLabel,
  pageConfidence,
  type Level,
} from "./confidenceLevel";

const LEVELS: Level[] = ["high", "medium", "low"];

function pct(value: number | null | undefined): string {
  return value == null ? "-" : `${Math.round(value)}%`;
}

function text(value: string | null | undefined): string {
  return value && value.trim() ? value : "-";
}

type Counts = Record<Level, number>;
const zero = (): Counts => ({ high: 0, medium: 0, low: 0 });

export default function ChartDetails({
  doc,
  folder,
}: {
  doc: ImagingDocumentResponse | null;
  folder: FolderDetail | null;
}) {
  const [open, setOpen] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  const data = useMemo(() => {
    if (!open || !doc || doc.pages.length === 0) return null;
    const scored = scoreChart(doc.folder_id, folder?.name ?? doc.folder_id, doc);
    const cc = chartConfidence(doc.pages);
    // Confidence per metric: how many pages are High / Medium / Low on each field.
    const perField = new Map<string, Counts>(CONFIDENCE_FIELDS.map(([key]) => [key, zero()]));
    for (const page of doc.pages) {
      for (const field of pageConfidence(page).fields) perField.get(field.key)![field.level] += 1;
    }
    const fieldRows = CONFIDENCE_FIELDS.map(([key, label]) => ({ label, counts: perField.get(key)! }))
      .filter((row) => row.counts.high + row.counts.medium + row.counts.low > 0);
    // Cross tab: page confidence level x the page's overall accuracy verdict.
    const cross = LEVELS.map((level) => {
      const pages = scored.pages.filter((p) => p.confidence?.level === level && p.overall != null);
      const match = pages.filter((p) => p.overall === "yes").length;
      const partial = pages.filter((p) => p.overall === "maybe").length;
      const noMatch = pages.length - match - partial;
      return { level, match, partial, noMatch, total: pages.length,
               rate: pages.length ? (100 * match) / pages.length : null };
    });
    return { scored, cc, fieldRows, cross };
  }, [open, doc, folder?.name]);

  if (!doc || doc.pages.length === 0) return null;

  const button = (
    <div className="mode-icon-group chart-details-group">
      <button type="button" className="mode-icon-btn" onClick={() => setOpen(true)}
              title="Chart analysis" aria-label="Chart analysis">
        <Info size={18} aria-hidden="true" />
        <span>Analysis</span>
      </button>
    </div>
  );
  if (!open || !data) return button;

  const { scored, cc, fieldRows, cross } = data;
  const metrics = LIVE_METRICS.map((metric) => ({
    label: metric.label,
    pct: accuracyPercent(scored.metrics[metric.id]),
    pages: scored.metrics[metric.id].scored,
  })).filter((row) => row.pct != null);
  const overall = accuracyPercent(scored.overall);
  const crossTotal = cross.reduce(
    (t, r) => ({ match: t.match + r.match, partial: t.partial + r.partial, noMatch: t.noMatch + r.noMatch, total: t.total + r.total }),
    { match: 0, partial: 0, noMatch: 0, total: 0 },
  );
  const showPartial = crossTotal.partial > 0;

  return (
    <>
      {button}
      {createPortal(
        <div className="modal-backdrop" role="presentation" onClick={() => setOpen(false)}>
          <div className="modal-card chart-details-card" role="dialog" aria-modal="true"
               aria-labelledby="chart-details-title" onClick={(event) => event.stopPropagation()}>
            <div className="modal-card-header">
              <div>
                <h2 id="chart-details-title">{folder?.name ?? doc.folder_id}</h2>
                <p className="chart-details-sub">
                  {doc.pages.length} page{doc.pages.length === 1 ? "" : "s"} · {scored.pages.length} with ground truth
                </p>
              </div>
              <button type="button" className="modal-close" onClick={() => setOpen(false)} aria-label="Close">
                <X size={16} />
              </button>
            </div>

            <section>
              <h3>Chart</h3>
              <dl className="chart-details-facts">
                <dt>Cluster</dt><dd>{folder?.chart_cluster || "Not clustered"}</dd>
                <dt>Run / Batch</dt><dd>{text(folder?.run_id)} / {text(folder?.batch_id)}</dd>
                <dt>Scope</dt><dd>{text(folder?.project_scope)}</dd>
                <dt>Measure</dt><dd>{text(folder?.measure)}</dd>
                <dt>Source</dt><dd>{text(folder?.source_name)}</dd>
                <dt>Retrieval</dt><dd>{text(folder?.rr_method)}</dd>
              </dl>
            </section>

            <section>
              <h3>Accuracy</h3>
              {metrics.length ? (
                <table className="chart-details-table">
                  <thead><tr><th>Metric</th><th className="num">Accuracy</th><th className="num">Pages</th></tr></thead>
                  <tbody>
                    <tr className="chart-details-total"><td>Overall (every metric right)</td><td className="num">{pct(overall)}</td><td className="num">{scored.overall.scored}</td></tr>
                    {metrics.map((row) => (
                      <tr key={row.label}><td>{row.label}</td><td className="num">{pct(row.pct)}</td><td className="num">{row.pages}</td></tr>
                    ))}
                  </tbody>
                </table>
              ) : <p className="chart-details-note">No ground truth saved for this chart yet.</p>}
            </section>

            <section>
              <h3>Confidence</h3>
              <p className="chart-details-note">Pages per level. High = 80% and above, Medium = 50-80%, Low = under 50%.</p>
              <table className="chart-details-table">
                <thead>
                  <tr><th>Metric</th>{LEVELS.map((l) => <th key={l} className={`num level-text-${l}`}>{levelLabel(l)}</th>)}</tr>
                </thead>
                <tbody>
                  <tr className="chart-details-total">
                    <td>Overall (page level) · chart {levelLabel(cc.level)}{cc.reason ? ` (${cc.reason})` : ""}</td>
                    {LEVELS.map((l) => <td key={l} className="num">{cc.counts[l]}</td>)}
                  </tr>
                  {fieldRows.map((row) => (
                    <tr key={row.label}>
                      <td>{row.label}</td>
                      {LEVELS.map((l) => <td key={l} className="num">{row.counts[l]}</td>)}
                    </tr>
                  ))}
                </tbody>
              </table>
            </section>

            <section>
              <h3>Confidence × Accuracy (pages)</h3>
              {crossTotal.total ? (
                <>
                  <p className="chart-details-note">Pages with ground truth: page confidence against whether every metric matched.</p>
                  <table className="chart-details-table">
                    <thead>
                      <tr>
                        <th>Confidence</th><th className="num">Match</th>
                        {showPartial ? <th className="num">Partial</th> : null}
                        <th className="num">No Match</th><th className="num">Total</th><th className="num">Accuracy</th>
                      </tr>
                    </thead>
                    <tbody>
                      {cross.map((row) => (
                        <tr key={row.level}>
                          <td className={`level-text-${row.level}`}>{levelLabel(row.level)}</td>
                          <td className="num">{row.match}</td>
                          {showPartial ? <td className="num">{row.partial}</td> : null}
                          <td className="num">{row.noMatch}</td><td className="num">{row.total}</td>
                          <td className="num">{pct(row.rate)}</td>
                        </tr>
                      ))}
                      <tr className="chart-details-total">
                        <td>Total</td><td className="num">{crossTotal.match}</td>
                        {showPartial ? <td className="num">{crossTotal.partial}</td> : null}
                        <td className="num">{crossTotal.noMatch}</td><td className="num">{crossTotal.total}</td>
                        <td className="num">{pct((100 * crossTotal.match) / crossTotal.total)}</td>
                      </tr>
                    </tbody>
                  </table>
                </>
              ) : <p className="chart-details-note">Shown once pages have ground truth.</p>}
            </section>
          </div>
        </div>,
        document.body,
      )}
    </>
  );
}
