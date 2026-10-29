/** Duplicate UI label + display confidence from the pipeline's duplicate verdict.
 *
 * 2026-10-22 (user): a duplicate — ``isDuplicate`` — reads "Duplicate (To Page N)" here
 * and, since 2026-10-29, "No" under Blank / Junk (not junk); a partial duplicate —
 * ``isPartialDuplicate`` — reads "Partial (To Page N)" and stays a Main page. A copied page
 * whose encounter is not a copy — ``isDuplicateContent`` — reads "No (Duplicate Content,
 * Page N)". */

export const DUPLICATE_PARTIAL_MIN = 0.95;
export const DUPLICATE_YES_MIN = 0.98;

/** Below this similarity a page is surely not a duplicate (100% confidence). */
export const DUPLICATE_SURE_BELOW = 0.8;

/**
 * Confidence in the duplicate call a similarity makes (2026-10-22, user): from 100% at
 * 100% alike it falls exponentially to 50% at 95%, the cut between a duplicate and
 * not; below 95% the confidence that the page is NOT a duplicate rises back to 100% at
 * 80% alike. 99% → 87%, 98% → 76%, 96% → 57%, 90% → 63%.
 */
export function similarityConfidence(similarity: number): number {
  if (similarity >= DUPLICATE_PARTIAL_MIN) {
    return 0.5 ** ((1 - similarity) / (1 - DUPLICATE_PARTIAL_MIN));
  }
  if (similarity <= DUPLICATE_SURE_BELOW) return 1;
  return 0.5 ** ((similarity - DUPLICATE_SURE_BELOW) / (DUPLICATE_PARTIAL_MIN - DUPLICATE_SURE_BELOW));
}

/**
 * The Duplicate column's confidence. ``bestSimilarity`` is the page's highest duplicate
 * similarity (``duplicateConfidence``); without it a duplicate or partial row's own
 * ``similarity`` stands in, and a page never scored reads 100%, as before.
 */
export function duplicateDisplayConfidence(
  isDuplicate: boolean | null | undefined,
  similarity: number | null | undefined,
  isPartial?: boolean | null,
  bestSimilarity?: number | null,
): number | null {
  if (isDuplicate == null) return null;
  const flagged = isDuplicate || Boolean(isPartial);
  let score = bestSimilarity ?? null;
  if (score == null && flagged) score = similarity ?? null;
  if (score == null) return flagged ? null : 1;
  const sim = Number(score);
  if (!Number.isFinite(sim)) return null;
  // Flagged on its few differing words below the 95% cut: as unsure as the cut.
  if (flagged && sim < DUPLICATE_PARTIAL_MIN) return 0.5;
  return similarityConfidence(sim);
}

/**
 * ``Duplicate (To Page 4)`` for a duplicate, ``Partial (To Page 4)`` for a partial
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
    isContent?: boolean | null;
  } = {},
): string {
  void confidence; // the pipeline's verdict decides; kept for callers' signature
  const yetToProcess = labels.yetToProcess ?? "Yet to Process";
  const notFound = labels.notFound ?? "Not Found";
  if (!processed) return yetToProcess;
  if (isDuplicate == null) return notFound;
  const target = duplicatePageRef(labels.duplicateOf);
  const withTarget = (state: string) => (target ? `${state} (To ${target})` : state);
  if (isDuplicate) return withTarget("Duplicate");
  if (labels.isPartial) return withTarget("Partial");
  if (labels.isContent) return target ? `No (Duplicate Content, ${target})` : "No (Duplicate Content)";
  return "No";
}

/** A duplicate: it has no sequence. A partial duplicate keeps its own verdict. */
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

/** Download cell: the same as on screen — ``Duplicate (To Page 4)``, ``Partial (To Page 4)``, ``No``. */
export function formatDuplicateDownload(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  duplicateOf?: string | null,
  isPartial?: boolean | null,
  isContent?: boolean | null,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, true, { duplicateOf, isPartial, isContent });
}

/** "4.png" → "Page 4". Empty when the original page was not recorded. */
export function duplicatePageRef(fileName: string | null | undefined): string {
  const stem = (fileName ?? "").replace(/\.[^.]+$/, "").trim();
  return stem ? `Page ${stem}` : "";
}
