/** Which ground truth the screens show, remembered per browser.
 * "latest" = the newest value, whoever wrote it; "bulk_upload" = each page's
 * latest spreadsheet load, with manual edits hidden. */
export type GroundTruthSource = "latest" | "bulk_upload";

const KEY = "advantmed_ground_truth_source";

export function readGroundTruthSource(): GroundTruthSource {
  try {
    return localStorage.getItem(KEY) === "bulk_upload" ? "bulk_upload" : "latest";
  } catch {
    return "latest";
  }
}

export function writeGroundTruthSource(source: GroundTruthSource): void {
  try {
    if (source === "latest") localStorage.removeItem(KEY);
    else localStorage.setItem(KEY, source);
  } catch {
    // Private window or blocked storage: the choice lasts until reload.
  }
}

/** "?ground_truth=bulk_upload" while manual edits are hidden, else "". */
export function groundTruthQuery(): string {
  return readGroundTruthSource() === "bulk_upload" ? "?ground_truth=bulk_upload" : "";
}
