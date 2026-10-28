import { createContext, useContext } from "react";
import type { ChartReview } from "./api";
import { formatIst } from "./istTime";

/** The open chart's Reviewed mark (History, 2026-10-28, user). While marked, every
 * ground-truth editor (Page Details, Page Sequencing, encounters, the member decision) is
 * read only; the server refuses a save too. */
export const ChartReviewContext = createContext<ChartReview | null>(null);

/** Replaces the open chart's Reviewed mark after the banner clears it. */
export const SetChartReviewContext = createContext<((review: ChartReview) => void) | null>(null);

export function useChartFrozen(): ChartReview | null {
  const review = useContext(ChartReviewContext);
  return review?.reviewed ? review : null;
}

/** "Chart reviewed by X on 28 Oct 2026, 02:45 pm IST: its ground truth is frozen. Reach out to
 * an Admin to enable review." */
export function frozenNote(review: ChartReview): string {
  return `Chart reviewed by ${review.reviewedBy || "a reviewer"} on ${formatIst(review.reviewedAt)}: its ground truth is frozen. Reach out to an Admin to enable review.`;
}
