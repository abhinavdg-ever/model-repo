import { useContext, useEffect, useState } from "react";
import { Lock } from "lucide-react";
import {
  GROUND_TRUTH_SAVED,
  getGroundTruthUpdates,
  setChartReview,
  type ChartGroundTruthUpdates,
  type GroundTruthUpdate,
  type PageGroundTruth,
} from "./api";
import { ChartReviewContext, SetChartReviewContext } from "./chartFrozen";
import { ClientViewContext } from "./clientView";
import { formatIst, serverTime } from "./istTime";
import { readSession } from "./session";

/** The ground-truth tabs that carry the banner. */
export type GroundTruthTab = keyof ChartGroundTruthUpdates;

/** "Last Updated By Bulk Upload on X" / "Last Updated By User: Y on X". */
export function lastUpdatedText(update: GroundTruthUpdate | undefined): string {
  if (!update?.at) return "Last Updated: no ground truth yet";
  if (update.bulk) return `Last Updated By Bulk Upload on ${formatIst(update.at)}`;
  return `Last Updated By User: ${update.by || "unknown"} on ${formatIst(update.at)}`;
}

/** One page's Last Updated: its last manual save, or its last spreadsheet load when that
 * came later (or there is no save). */
export function pageUpdate(gt: PageGroundTruth | null | undefined): GroundTruthUpdate {
  const manual = serverTime(gt?.reviewedAt)?.getTime() ?? null;
  const bulk = serverTime(gt?.bulkUploadedAt)?.getTime() ?? null;
  if (manual != null && (bulk == null || manual >= bulk)) {
    return { bulk: false, by: gt?.reviewedBy, at: gt?.reviewedAt };
  }
  if (bulk != null) return { bulk: true, at: gt?.bulkUploadedAt };
  return { bulk: false };
}

/** The constant banner at the top of Page Details, Encounter, Sequencing and Rejection
 * Rules (2026-10-28, user): who last updated the tab's ground truth and when (Page Details:
 * the page shown, ``update``; the other tabs: the chart); for a chart
 * marked Reviewed, who completed the review. An admin, or whoever marked it, can enable
 * review again from here. */
export default function GroundTruthBanner({
  folderId,
  tab,
  update,
}: {
  folderId: string;
  tab: GroundTruthTab;
  /** Shown instead of the chart's: Page Details passes the page's own. */
  update?: GroundTruthUpdate;
}) {
  const review = useContext(ChartReviewContext);
  const onReviewChange = useContext(SetChartReviewContext);
  // Include Extracted: No (and the client's copy): as wide as the narrower Page Details.
  const finalOnly = useContext(ClientViewContext);
  const [updates, setUpdates] = useState<ChartGroundTruthUpdates | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () =>
      getGroundTruthUpdates(folderId)
        .then((next) => !cancelled && setUpdates(next))
        .catch(() => !cancelled && setUpdates(null));
    const onSaved = (event: Event) => {
      if ((event as CustomEvent<string>).detail === folderId) void load();
    };
    void load();
    window.addEventListener(GROUND_TRUTH_SAVED, onSaved);
    return () => {
      cancelled = true;
      window.removeEventListener(GROUND_TRUTH_SAVED, onSaved);
    };
  }, [folderId]);

  useEffect(() => {
    setConfirming(false);
    setError(null);
  }, [folderId, review?.reviewed]);

  const user = readSession()?.user;
  const reviewed = review?.reviewed ? review : null;
  const canEnable =
    Boolean(reviewed) &&
    (user?.role === "admin" || (user?.id != null && reviewed?.reviewedById === user.id));

  async function enableReview() {
    setBusy(true);
    setError(null);
    try {
      onReviewChange?.(await setChartReview(folderId, false));
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setBusy(false);
      setConfirming(false);
    }
  }

  return (
    <div className={`gt-banner${reviewed ? " is-reviewed" : ""}${finalOnly ? " is-final-only" : ""}`} role="status">
      <div className="gt-banner-line">
        {reviewed ? (
          <span className="gt-banner-locked" title="Marked Reviewed: the ground truth cannot be edited">
            <Lock size={12} aria-hidden="true" /> Locked
          </span>
        ) : null}
        <span>{lastUpdatedText(update ?? updates?.[tab])}</span>
      </div>
      {reviewed ? (
        <div className="gt-banner-line gt-banner-review">
          <span>
            Chart Review Completed by User: {reviewed.reviewedBy || "a reviewer"} on{" "}
            {formatIst(reviewed.reviewedAt)}
          </span>
          {canEnable ? (
            confirming ? (
              <span className="gt-banner-actions">
                Enable review? Its ground truth can be edited again.
                <button type="button" disabled={busy} onClick={() => void enableReview()}>
                  {busy ? "Saving…" : "Enable"}
                </button>
                <button type="button" disabled={busy} onClick={() => setConfirming(false)}>
                  Cancel
                </button>
              </span>
            ) : (
              <button type="button" onClick={() => setConfirming(true)}>
                Enable review
              </button>
            )
          ) : (
            <span className="gt-banner-hint">Reach out to an Admin to enable review.</span>
          )}
        </div>
      ) : null}
      {error ? <div className="gt-banner-line gt-banner-error">{error}</div> : null}
    </div>
  );
}
