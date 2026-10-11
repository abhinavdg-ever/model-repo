/** Duplicate UI label + display confidence from junk flag + stored similarity. */

export const DUPLICATE_MAYBE_MIN = 0.95;
export const DUPLICATE_YES_MIN = 1.0;

/**
 * Map raw SequenceMatcher similarity → UI confidence for Partial rows.
 * ``1 + (sim − 1) × 10`` (98% → 80%, 99% → 90%, 95% → 50%).
 */
export function duplicateDisplayConfidence(
  isDuplicate: boolean | null | undefined,
  similarity: number | null | undefined,
): number | null {
  if (isDuplicate == null) return null;
  if (!isDuplicate) return 1.0;
  if (similarity == null) return null;
  const sim = Number(similarity);
  if (!Number.isFinite(sim)) return null;
  if (sim >= DUPLICATE_YES_MIN) return 1.0;
  return 1 + (sim - 1) * 10;
}

/**
 * ``Yes (To Page 4)`` = exact match (100%); ``Partial (To Page 4)`` = [95%, 100%);
 * ``No`` otherwise. ``confidence`` is the SequenceMatcher ratio written for
 * duplicate rows; ``labels.duplicateOf`` is the reference page's file name.
 */
export function formatDuplicateLabel(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  processed = true,
  labels: { yetToProcess?: string; notFound?: string; duplicateOf?: string | null } = {},
): string {
  const yetToProcess = labels.yetToProcess ?? "Yet to Process";
  const notFound = labels.notFound ?? "Not Found";
  if (!processed) return yetToProcess;
  if (isDuplicate == null) return notFound;
  if (!isDuplicate) return "No";
  const target = duplicatePageRef(labels.duplicateOf);
  const withTarget = (state: string) => (target ? `${state} (To ${target})` : state);
  if (confidence == null) return withTarget("Partial");
  const sim = Number(confidence);
  if (!Number.isFinite(sim)) return withTarget("Partial");
  if (sim >= DUPLICATE_YES_MIN) return withTarget("Yes");
  if (sim >= DUPLICATE_MAYBE_MIN) return withTarget("Partial");
  return "No";
}

/** Right-hand note for a duplicate row. Empty when this page is not one. */
export function duplicateTargetNote(fileName: string | null | undefined): string {
  const ref = duplicatePageRef(fileName);
  return ref ? `Duplicate to ${ref}` : "";
}

/** Download cell: the same as on screen — ``Yes (To Page 4)``, ``Partial (To Page 4)``, ``No``. */
export function formatDuplicateDownload(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  duplicateOf?: string | null,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, true, { duplicateOf });
}

/** "4.png" → "Page 4". Empty when the original page was not recorded. */
export function duplicatePageRef(fileName: string | null | undefined): string {
  const stem = (fileName ?? "").replace(/\.[^.]+$/, "").trim();
  return stem ? `Page ${stem}` : "";
}
