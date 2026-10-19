/** Which ground truth the screens show, remembered per browser.
 * "latest" = the newest value, whoever wrote it; "bulk_upload" = each page's
 * latest spreadsheet load, with manual edits hidden. */
export type GroundTruthSource = "latest" | "bulk_upload";

const KEY = "advantmed_ground_truth_source";

/** Always "latest" since the "Hide manual ground truth" toggle was removed (2026-10-19); a
 * browser that had turned it on is reset. */
export function readGroundTruthSource(): GroundTruthSource {
  try {
    localStorage.removeItem(KEY);
  } catch {
    // Private window or blocked storage: nothing stored to clear.
  }
  return "latest";
}

/** "?ground_truth=bulk_upload" while manual edits are hidden, else "". */
export function groundTruthQuery(): string {
  return readGroundTruthSource() === "bulk_upload" ? "?ground_truth=bulk_upload" : "";
}
