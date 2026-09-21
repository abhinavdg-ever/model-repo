/** Duplicate UI label + display confidence from junk flag + stored similarity. */

export const DUPLICATE_MAYBE_MIN = 0.95;
export const DUPLICATE_YES_MIN = 1.0;

/**
 * Map raw SequenceMatcher similarity → UI confidence for May Be rows.
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
 * Yes = exact match (100%); May Be = [95%, 100%); No otherwise.
 * ``confidence`` is the SequenceMatcher ratio written for duplicate rows.
 */
export function formatDuplicateLabel(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  processed = true,
  labels: { yetToProcess?: string; notFound?: string } = {},
): string {
  const yetToProcess = labels.yetToProcess ?? "Yet to Process";
  const notFound = labels.notFound ?? "Not Found";
  if (!processed) return yetToProcess;
  if (isDuplicate == null) return notFound;
  if (!isDuplicate) return "No";
  if (confidence == null) return "May Be";
  const sim = Number(confidence);
  if (!Number.isFinite(sim)) return "May Be";
  if (sim >= DUPLICATE_YES_MIN) return "Yes";
  if (sim >= DUPLICATE_MAYBE_MIN) return "May Be";
  return "No";
}
