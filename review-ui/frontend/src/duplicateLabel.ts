/** Duplicate UI label + display confidence from the pipeline's duplicate verdict.
 *
 * 2026-11-11 (user: Duplicate_Logic.md): a duplicate — ``isDuplicate`` — ("No" under Blank /
 * Junk: not junk); a 100% text match a hard block kept from being a duplicate —
 * ``isDuplicateContent`` — stays a normal page. N is always the earliest copy.
 * 2026-11-13 (user): they read "Yes (to page N)" and "Partial (to page N)" (were "Duplicate of
 * N" / "Copy of N"); both match a ground truth of Yes. */

export const DUPLICATE_PARTIAL_MIN = 0.95;
export const DUPLICATE_YES_MIN = 0.98;

/**
 * Duplicate confidence (2026-11-14, user; docs/specials/CONFIDENCE_LOGIC.md 12.2.1, the same
 * numbers as core-pipeline/resources/canon/confidence_canon.json ``duplicate``).
 */
export const DUPLICATE_CURVE = {
  matchSlope: 10,
  floor: 0.3,
  clearBelow: 0.8,
  goodUntil: 0.9,
  goodSlope: 2,
  nearSlope: 5,
} as const;

/** Below this similarity a page is surely not a duplicate (100% confidence). */
export const DUPLICATE_SURE_BELOW = DUPLICATE_CURVE.clearBelow;

/** Confidence in Yes / Partial from the match score s to the original:
 * max(0.30, 1 - 10 x (1 - s)). 100% → 100%, 98% → 80%, 95% → 50%, floor 30%. */
export function duplicateMatchConfidence(similarity: number): number {
  const s = Math.min(1, Math.max(0, similarity));
  return Math.max(DUPLICATE_CURVE.floor, 1 - DUPLICATE_CURVE.matchSlope * (1 - s));
}

/** Confidence in No from the best match score s to any compared page: under 80% → 100%;
 * 80–90% → 1 - 2 x (s - 0.80) (90% → 80%); above → 0.80 - 5 x (s - 0.90), floor 30%
 * (95% → 55%, 98% → 40%, 100% → 30%). */
export function duplicateNoConfidence(similarity: number): number {
  const s = Math.min(1, Math.max(0, similarity));
  const { clearBelow, goodUntil, goodSlope, nearSlope, floor } = DUPLICATE_CURVE;
  if (s < clearBelow) return 1;
  if (s <= goodUntil) return 1 - goodSlope * (s - clearBelow);
  return Math.max(floor, 1 - goodSlope * (goodUntil - clearBelow) - nearSlope * (s - goodUntil));
}

/** The confidence a similarity gives the call it makes: Yes at or above 95%, No below. */
export function similarityConfidence(similarity: number): number {
  return similarity >= DUPLICATE_PARTIAL_MIN ? duplicateMatchConfidence(similarity) : duplicateNoConfidence(similarity);
}

/**
 * The Duplicate column's confidence. ``bestSimilarity`` is the page's highest duplicate
 * similarity (``duplicateConfidence``); without it a duplicate row's own ``similarity``
 * stands in. Yes: the match curve. Partial (``isContent``: a blocked 100% match): the match
 * curve at 100%. No: the No curve; a page never compared reads 100%.
 */
export function duplicateDisplayConfidence(
  isDuplicate: boolean | null | undefined,
  similarity: number | null | undefined,
  isPartial?: boolean | null,
  bestSimilarity?: number | null,
  isContent?: boolean | null,
): number | null {
  if (isDuplicate == null) return null;
  if (isContent && !isDuplicate) return duplicateMatchConfidence(1);
  const flagged = isDuplicate || Boolean(isPartial);
  let score = bestSimilarity ?? null;
  if (score == null && flagged) score = similarity ?? null;
  if (score == null) return flagged ? null : 1;
  const sim = Number(score);
  if (!Number.isFinite(sim)) return null;
  return flagged ? duplicateMatchConfidence(sim) : duplicateNoConfidence(sim);
}

/**
 * ``Yes (to page 4)`` for a duplicate, ``Partial (to page 4)`` for a blocked 100% match, ``No``.
 * ``labels.duplicateOf`` is the original page's file name. ``isPartial`` is ignored (no
 * Partials since 2026-11-11).
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
  const target = duplicatePageNumber(labels.duplicateOf);
  const withTarget = (state: string) => (target ? `${state} (to page ${target})` : state);
  if (isDuplicate) return withTarget("Yes");
  if (labels.isContent) return withTarget("Partial");
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

/** Download cell: the same as on screen — ``Yes (to page 4)``, ``Partial (to page 4)``, ``No``. */
export function formatDuplicateDownload(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  duplicateOf?: string | null,
  isPartial?: boolean | null,
  isContent?: boolean | null,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, true, { duplicateOf, isPartial, isContent });
}

/** "4.png" → "4". Empty when the original page was not recorded. */
export function duplicatePageNumber(fileName: string | null | undefined): string {
  return (fileName ?? "").replace(/\.[^.]+$/, "").trim();
}

/** The table row id of a page, so "Yes (to page 4)" can link to page 4's row. */
export function pageRowId(fileName: string | null | undefined): string {
  return `imaging-page-${duplicatePageNumber(fileName)}`;
}

/** "4.png" → "Page 4". Empty when the original page was not recorded. */
export function duplicatePageRef(fileName: string | null | undefined): string {
  const stem = (fileName ?? "").replace(/\.[^.]+$/, "").trim();
  return stem ? `Page ${stem}` : "";
}
