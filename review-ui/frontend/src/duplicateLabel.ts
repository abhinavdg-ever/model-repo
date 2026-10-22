/** Duplicate UI label + display confidence from the pipeline's duplicate verdict.
 *
 * 2026-10-22 (user): ≥ 98% alike (or contained) is a duplicate — ``isDuplicate``,
 * "Yes", and Blank / Junk "Yes (Duplicate)"; 95–98% is a partial duplicate —
 * ``isPartialDuplicate``, "Partial", and the page stays Main. */

export const DUPLICATE_PARTIAL_MIN = 0.95;
export const DUPLICATE_YES_MIN = 0.98;

/**
 * Stored similarity → UI confidence. A duplicate is 100%; a partial duplicate
 * ``1 + (sim − 1) × 10`` (95% → 50%, 97% → 70%).
 */
export function duplicateDisplayConfidence(
  isDuplicate: boolean | null | undefined,
  similarity: number | null | undefined,
  isPartial?: boolean | null,
): number | null {
  if (isDuplicate == null) return null;
  if (isDuplicate || !isPartial) return 1.0;
  if (similarity == null) return null;
  const sim = Number(similarity);
  if (!Number.isFinite(sim)) return null;
  return 1 + (sim - 1) * 10;
}

/**
 * ``Yes (To Page 4)`` for a duplicate, ``Partial (To Page 4)`` for a partial
 * duplicate, ``No`` otherwise. ``labels.duplicateOf`` is the reference page's
 * file name.
 */
export function formatDuplicateLabel(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  processed = true,
  labels: {
    yetToProcess?: string;
    notFound?: string;
    duplicateOf?: string | null;
    isPartial?: boolean | null;
  } = {},
): string {
  void confidence; // the pipeline's verdict decides; kept for callers' signature
  const yetToProcess = labels.yetToProcess ?? "Yet to Process";
  const notFound = labels.notFound ?? "Not Found";
  if (!processed) return yetToProcess;
  if (isDuplicate == null) return notFound;
  const target = duplicatePageRef(labels.duplicateOf);
  const withTarget = (state: string) => (target ? `${state} (To ${target})` : state);
  if (isDuplicate) return withTarget("Yes");
  if (labels.isPartial) return withTarget("Partial");
  return "No";
}

/** A duplicate (≥ 98%): the Blank / Junk column shows it as ``Yes (Duplicate)``,
 * and it has no sequence. A partial duplicate is a Main page. */
export function isCompleteDuplicate(
  isDuplicate: boolean | null | undefined,
  confidence?: number | null,
): boolean {
  void confidence;
  return isDuplicate === true;
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
  isPartial?: boolean | null,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, true, { duplicateOf, isPartial });
}

/** "4.png" → "Page 4". Empty when the original page was not recorded. */
export function duplicatePageRef(fileName: string | null | undefined): string {
  const stem = (fileName ?? "").replace(/\.[^.]+$/, "").trim();
  return stem ? `Page ${stem}` : "";
}
