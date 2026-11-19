/** The chart at a glance (2026-11-19, user): a small confidence pill and a Details button in the
 * top bar, left of OCR. Details opens one card: the chart (cluster, run, batch, pages), its
 * accuracy (overall and per metric), how its pages' confidence is spread, and how accurate each
 * confidence level is -- so High pages can be trusted and Low pages looked at first. */
import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { Info, X } from "lucide-react";

import { LIVE_METRICS, accuracyPercent, scoreChart } from "./accuracy";
import type { FolderDetail, ImagingDocumentResponse } from "./api";
import { chartConfidence, levelLabel, type Level } from "./confidenceLevel";

const LEVELS: Level[] = ["high", "medium", "low"];
const LEVEL_HINT: Record<Level, string> = {
  high: "every field 80%+: can skip review",
  medium: "some fields 50-80%: a quick look",
  low: "a key field under 50%: check carefully",
};

function pct(value: number | null | undefined): string {
  return value == null ? "-" : `${Math.round(value)}%`;
}

export default function ChartDetails({
  doc,
  folder,
}: {
  doc: ImagingDocumentResponse | null;
  folder: FolderDetail | null;
}) {
  const [open, setOpen] = useState(false);
  const summary = useMemo(() => {
    if (!doc || doc.pages.length === 0) return null;
    const scored = scoreChart(doc.folder_id, folder?.name ?? doc.folder_id, doc);
    return { cc: chartConfidence(doc.pages), scored };
  }, [doc, folder?.name]);

  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open]);

  if (!summary) return null;
  const { cc, scored } = summary;
  const total = cc.counts.high + cc.counts.medium + cc.counts.low;
  const overall = accuracyPercent(scored.overall);
  const metrics = LIVE_METRICS.map((metric) => ({
    label: metric.label,
    pct: accuracyPercent(scored.metrics[metric.id]),
    pages: scored.metrics[metric.id].scored,
  })).filter((row) => row.pct != null);
  // Confidence x accuracy: of the pages with ground truth at each level, how many had every
  // scored metric right (the Overall verdict).
  const byLevel = LEVELS.map((level) => {
    const pages = scored.pages.filter((p) => p.confidence?.level === level && p.overall != null);
    const right = pages.filter((p) => p.overall === "yes").length;
    return { level, pages: pages.length, right, pct: pages.length ? (100 * right) / pages.length : null };
  });

  return (
    <>
      <div className="chart-glance" title={`Chart confidence: ${levelLabel(cc.level)}`}>
        <span className={`chart-glance-pill level-${cc.level ?? "none"}`}>
          {levelLabel(cc.level)}
          {cc.value != null ? ` ${Math.round(cc.value * 100)}%` : ""}
        </span>
        <button type="button" className="chart-glance-btn" onClick={() => setOpen(true)}>
          <Info size={14} aria-hidden="true" /> Details
        </button>
      </div>
      {open
        ? createPortal(
            <div className="modal-backdrop" role="presentation" onClick={() => setOpen(false)}>
              <div className="modal-card chart-details-card" role="dialog" aria-modal="true"
                   aria-labelledby="chart-details-title" onClick={(event) => event.stopPropagation()}>
                <div className="modal-card-header">
                  <h2 id="chart-details-title">{folder?.name ?? doc?.folder_id}</h2>
                  <button type="button" className="modal-close" onClick={() => setOpen(false)} aria-label="Close">
                    <X size={16} />
                  </button>
                </div>

                <section>
                  <h3>Chart</h3>
                  <dl className="chart-details-facts">
                    <dt>Cluster</dt><dd>{folder?.chart_cluster || "Not clustered"}</dd>
                    <dt>Run / Batch</dt><dd>{folder?.run_id || "-"} / {folder?.batch_id || "-"}</dd>
                    <dt>Pages</dt><dd>{doc?.pages.length ?? 0} ({scored.pages.length} with ground truth)</dd>
                  </dl>
                </section>

                <section>
                  <h3>Accuracy {overall != null ? <span className="chart-details-big">{pct(overall)}</span> : null}</h3>
                  {metrics.length ? (
                    <>
                      <p className="chart-details-note">Overall = pages where every metric matched the ground truth.</p>
                      <table className="chart-details-table">
                        <thead><tr><th>Metric</th><th>Accuracy</th><th>Pages</th></tr></thead>
                        <tbody>
                          {metrics.map((row) => (
                            <tr key={row.label}><td>{row.label}</td><td>{pct(row.pct)}</td><td>{row.pages}</td></tr>
                          ))}
                        </tbody>
                      </table>
                    </>
                  ) : <p className="chart-details-note">No ground truth saved for this chart yet.</p>}
                </section>

                <section>
                  <h3>Confidence</h3>
                  <p className="chart-details-note">How sure the pipeline is, page by page.</p>
                  {LEVELS.map((level) => {
                    const n = cc.counts[level];
                    const share = total ? (100 * n) / total : 0;
                    return (
                      <div key={level} className="chart-details-bar-row">
                        <span className="chart-details-bar-label">{levelLabel(level)}</span>
                        <span className="chart-details-bar"><span className={`level-${level}`} style={{ width: `${share}%` }} /></span>
                        <span className="chart-details-bar-count">{n} page{n === 1 ? "" : "s"}</span>
                        <span className="chart-details-hint">{LEVEL_HINT[level]}</span>
                      </div>
                    );
                  })}
                  {cc.pages.low.length ? (
                    <p className="chart-details-note">Low pages: {cc.pages.low.slice(0, 20).join(", ")}{cc.pages.low.length > 20 ? " …" : ""}</p>
                  ) : null}
                </section>

                <section>
                  <h3>Confidence vs accuracy</h3>
                  {byLevel.some((row) => row.pages) ? (
                    <>
                      <p className="chart-details-note">Of the pages with ground truth: how many were fully right at each confidence level.</p>
                      <table className="chart-details-table">
                        <thead><tr><th>Confidence</th><th>Pages</th><th>Fully right</th></tr></thead>
                        <tbody>
                          {byLevel.map((row) => (
                            <tr key={row.level}>
                              <td>{levelLabel(row.level)}</td><td>{row.pages}</td>
                              <td>{row.pages ? `${row.right} (${pct(row.pct)})` : "-"}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </>
                  ) : <p className="chart-details-note">Shown once pages have ground truth.</p>}
                </section>
              </div>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
