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
  labels: { yetToProcess?: string; notFound?: string; duplicateOf?: string | null } = {},
): string {
  const yetToProcess = labels.yetToProcess ?? "Yet to Process";
  const notFound = labels.notFound ?? "Not Found";
  const duplicateOf = labels.duplicateOf;
  if (!processed) return yetToProcess;
  if (isDuplicate == null) return notFound;
  if (!isDuplicate) return "No";
  if (confidence == null) return withDuplicateOf("May Be", duplicateOf);
  const sim = Number(confidence);
  if (!Number.isFinite(sim)) return withDuplicateOf("May Be", duplicateOf);
  if (sim >= DUPLICATE_YES_MIN) return withDuplicateOf("Yes", duplicateOf);
  if (sim >= DUPLICATE_MAYBE_MIN) return withDuplicateOf("May Be", duplicateOf);
  return "No";
}

/** "4.png" → "Page 4". Empty when the original page was not recorded. */
export function duplicatePageRef(fileName: string | null | undefined): string {
  const stem = (fileName ?? "").replace(/\.[^.]+$/, "").trim();
  return stem ? `Page ${stem}` : "";
}

function withDuplicateOf(label: string, duplicateOf: string | null | undefined): string {
  const ref = duplicatePageRef(duplicateOf);
  return ref ? `${label} (with ${ref})` : label;
}
