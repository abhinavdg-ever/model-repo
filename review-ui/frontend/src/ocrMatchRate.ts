/**
 * OCR overlap / match rate between available engines.
 *
 * - 3 texts → average of the 3 pairwise Jaccard scores
 * - 2 texts → single pairwise Jaccard
 * - 0–1 texts → null (UI shows NA)
 */

import type { OcrKind } from "./api";
import { OCR_TAB_LABELS } from "./api";
import { ocrTextForFilename } from "./ocrPages";

export type OcrMatchRate = {
  /** 0–1, or null → display NA */
  rate: number | null;
  /** How many usable OCR texts were compared */
  count: number;
  /** Short labels of engines included, e.g. ["Tess", "OSS"] */
  engines: string[];
  /** Pairwise rates for tooltips */
  pairs: { left: string; right: string; rate: number }[];
};

const SHORT_LABEL: Record<OcrKind, string> = {
  preliminary: "Tess",
  final1: "OSS",
  final2: "AzDocInt",
};

export function tokenize(text: string): Set<string> {
  const tokens = text
    .toLowerCase()
    .replace(/[^\p{L}\p{N}]+/gu, " ")
    .split(/\s+/)
    .filter((t) => t.length > 1);
  return new Set(tokens);
}

export function jaccard(a: Set<string>, b: Set<string>): number {
  if (a.size === 0 && b.size === 0) return 1;
  if (a.size === 0 || b.size === 0) return 0;
  let inter = 0;
  for (const t of a) {
    if (b.has(t)) inter += 1;
  }
  return inter / (a.size + b.size - inter);
}

export function computeMatchRate(
  texts: { kind: OcrKind; text: string }[],
): OcrMatchRate {
  const usable = texts.filter((t) => t.text.trim().length > 0);
  if (usable.length < 2) {
    return {
      rate: null,
      count: usable.length,
      engines: usable.map((t) => SHORT_LABEL[t.kind]),
      pairs: [],
    };
  }

  const sets = usable.map((t) => ({
    kind: t.kind,
    label: SHORT_LABEL[t.kind],
    tokens: tokenize(t.text),
  }));

  const pairs: OcrMatchRate["pairs"] = [];
  let sum = 0;
  for (let i = 0; i < sets.length; i++) {
    for (let j = i + 1; j < sets.length; j++) {
      const rate = jaccard(sets[i].tokens, sets[j].tokens);
      pairs.push({ left: sets[i].label, right: sets[j].label, rate });
      sum += rate;
    }
  }

  return {
    rate: sum / pairs.length,
    count: usable.length,
    engines: sets.map((s) => s.label),
    pairs,
  };
}

export function isUsableOcrPayload(text: string): boolean {
  if (!text.trim()) return false;
  if (text.startsWith("No ")) return false;
  if (text === "OCR unavailable") return false;
  if (text.startsWith("No OCR text found")) return false;
  return true;
}

/** Page-level match rate from full-document OCR blobs. */
export function pageMatchRate(
  byKind: Partial<Record<OcrKind, string>>,
  filename: string,
  kinds: OcrKind[] = ["preliminary", "final1", "final2"],
): OcrMatchRate {
  const texts: { kind: OcrKind; text: string }[] = [];
  for (const kind of kinds) {
    const full = byKind[kind];
    if (!full || !isUsableOcrPayload(full)) continue;
    const chunk = ocrTextForFilename(full, filename);
    if (chunk.trim()) texts.push({ kind, text: chunk });
  }
  return computeMatchRate(texts);
}

/** Document-level match rate (full OCR text per engine). */
export function documentMatchRate(
  byKind: Partial<Record<OcrKind, string>>,
  kinds: OcrKind[] = ["preliminary", "final1", "final2"],
): OcrMatchRate {
  const texts: { kind: OcrKind; text: string }[] = [];
  for (const kind of kinds) {
    const full = byKind[kind];
    if (!full || !isUsableOcrPayload(full)) continue;
    texts.push({ kind, text: full });
  }
  return computeMatchRate(texts);
}

export function formatMatchRatePercent(rate: number | null): string {
  if (rate === null) return "NA";
  return `${Math.round(rate * 100)}%`;
}

/** Traffic-light class for Match rate badge. */
export function matchRateToneClass(rate: number | null): string {
  if (rate === null) return "is-na";
  const pct = rate * 100;
  if (pct > 90) return "is-high";
  if (pct >= 70) return "is-mid";
  return "is-low";
}

export function matchRateTitle(result: OcrMatchRate): string {
  if (result.rate === null) {
    if (result.count <= 1) {
      return result.count === 0
        ? "Match rate: NA (no OCR)"
        : `Match rate: NA (only 1 OCR: ${result.engines[0] ?? OCR_TAB_LABELS.preliminary})`;
    }
    return "Match rate: NA";
  }
  const pairs = result.pairs
    .map((p) => `${p.left}↔${p.right} ${Math.round(p.rate * 100)}%`)
    .join(" · ");
  return `Token overlap (Jaccard). ${pairs}`;
}
