/** Page and chart confidence levels (2026-11-19, user): High pages can skip review, Low pages
 * are looked at carefully.
 *
 * Only the features of the final accuracy list count (2026-11-19, user): Member Verification,
 * DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation. Each has its Final
 * confidence (Member Verification: the lower of Member Name and Member DOB), banded as
 * everywhere: High >= 0.80, Medium 0.50-0.80, Low < 0.50. A feature with no confidence
 * (nothing read, a default value) is not counted.
 *
 * Page (2026-11-22, user: like an NPS, as the chart): score = (High fields - Low fields) / fields
 * counted, -1..+1 (Medium counts 0); its % = (1 + score) / 2, at least the 20% floor, banded like
 * any confidence. A Blank / Junk or duplicate page counts only what matters for it: Blank / Junk
 * and Codability.
 *
 * Chart (2026-11-21, user: like an NPS): score = (High pages - Low pages) / pages scored, -1..+1
 * (Medium counts 0). Its % = (1 + score) / 2 -- all High 100%, all Medium 50%, all Low 0% -- at
 * least the 20% floor, and its level is that % banded like any confidence (High >= 80%, i.e.
 * score >= +0.6; Low < 50%, i.e. more Low pages than High).
 */
import type { ImagingPageResult } from "./api";

export type Level = "high" | "medium" | "low";

export const HIGH_AT = 0.8;
export const LOW_BELOW = 0.5;
/** No confidence is shown below this (2026-11-19, user). */
export const FLOOR = 0.2;

/** A chart's confidence from its pages' levels (2026-11-21, user: NPS-like). */
export function chartScore(counts: Record<Level, number>): { nps: number; value: number; level: Level } | null {
  const n = counts.high + counts.medium + counts.low;
  if (!n) return null;
  const nps = (counts.high - counts.low) / n;
  const value = Math.max(FLOOR, (1 + nps) / 2);
  return { nps, value, level: band(value) };
}
/** The % never contradicts the level (2026-11-19, user): a Medium page / chart shows at most
 * 79%, a Low one at most 49%, whatever the plain average. */
export const LEVEL_CAP: Record<Level, number> = { high: 1, medium: 0.79, low: 0.49 };

export function capToLevel(level: Level, value: number): number {
  return Math.min(value, LEVEL_CAP[level]);
}

/** The features counted, in the accuracy list's order: [key, label]. */
export const CONFIDENCE_FIELDS: [string, string][] = [
  ["member", "Member Verification"],
  ["dos", "DOS"],
  ["blank_junk", "Blank / Junk"],
  ["page_type", "Page Type"],
  ["codability", "Codability"],
  ["provider", "Provider Signature"],
  ["orientation", "Orientation"],
];
const BLANK_JUNK_FIELDS = new Set(["blank_junk", "codability"]);

export function band(value: number): Level {
  return value >= HIGH_AT ? "high" : value >= LOW_BELOW ? "medium" : "low";
}

export type FieldConfidence = { key: string; label: string; value: number; level: Level };
export type PageConfidence = {
  level: Level | null;
  value: number | null;
  /** (High fields - Low fields) / fields, -1..+1 (2026-11-22). */
  nps?: number | null;
  counts: Record<Level, number>;
  fields: FieldConfidence[];
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fieldValue(page: ImagingPageResult, key: string): number | null {
  if (key === "member") {
    const parts = [rawValue(page, "member_name"), rawValue(page, "member_dob")].filter((v): v is number => v != null);
    return parts.length ? Math.min(...parts) : null;
  }
  return rawValue(page, key);
}

function rawValue(page: ImagingPageResult, key: string): number | null {
  const stored = num(page.finalConfidences?.[key]);
  if (stored != null) return stored;
  // Rows written before the Final confidences: the stage's own number.
  switch (key) {
    case "dos": return num(page.dosConfidence);
    case "page_type": return num(page.pageTypeConfidence);
    case "blank_junk": return num(page.blankJunkConfidence);
    case "orientation": return num(page.measuredConfidences?.orientation);
    default: return null;
  }
}

/** Every feature's confidence on the page, Blank / Junk pages included (the Confidence Analysis
 * scores each metric on every page the accuracy scores it, 2026-11-19, user). */
export function featureConfidences(page: ImagingPageResult): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [key] of CONFIDENCE_FIELDS) {
    const value = fieldValue(page, key);
    if (value != null) out[key] = value;
  }
  return out;
}

export function isBlankJunkOrDuplicate(page: ImagingPageResult): boolean {
  return page.isDuplicate === true || (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes");
}

export function pageConfidence(page: ImagingPageResult): PageConfidence {
  const only = isBlankJunkOrDuplicate(page) ? BLANK_JUNK_FIELDS : null;
  const fields: FieldConfidence[] = [];
  for (const [key, label] of CONFIDENCE_FIELDS) {
    if (only && !only.has(key)) continue;
    const value = fieldValue(page, key);
    if (value == null) continue;
    fields.push({ key, label, value, level: band(value) });
  }
  const counts: Record<Level, number> = { high: 0, medium: 0, low: 0 };
  for (const f of fields) counts[f.level] += 1;
  if (!fields.length) return { level: null, value: null, nps: null, counts, fields };
  // 2026-11-22 (user): like an NPS over the page's fields, as the chart over its pages.
  const score = chartScore(counts)!;
  return { level: score.level, value: score.value, nps: score.nps, counts, fields };
}

export type ChartConfidence = {
  level: Level | null;
  value: number | null;
  /** (High - Low) / pages, -1..+1 (2026-11-21). */
  nps: number | null;
  /** Pages per level; pages with nothing to score are not counted. */
  counts: Record<Level, number>;
  /** Page numbers per level, for "Low: 3, 8, 12". */
  pages: Record<Level, number[]>;
};

export function chartConfidence(pages: ImagingPageResult[]): ChartConfidence {
  const counts: Record<Level, number> = { high: 0, medium: 0, low: 0 };
  const byLevel: Record<Level, number[]> = { high: [], medium: [], low: [] };
  for (const page of pages) {
    const pc = pageConfidence(page);
    if (!pc.level || pc.value == null) continue;
    counts[pc.level] += 1;
    byLevel[pc.level].push(page.pageNumber);
  }
  const score = chartScore(counts);
  if (!score) return { level: null, value: null, nps: null, counts, pages: byLevel };
  return { level: score.level, value: score.value, nps: score.nps, counts, pages: byLevel };
}

export function levelLabel(level: Level | null): string {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : "Not scored";
}
