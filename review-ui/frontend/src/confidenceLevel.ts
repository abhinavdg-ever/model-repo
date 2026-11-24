/** Page and chart confidence levels (2026-11-19, user): High pages can skip review, Low pages
 * are looked at carefully.
 *
 * Only the features of the final accuracy list count (2026-11-19, user): Member Verification,
 * DOS, Blank / Junk, Page Type, Codability, Provider Signature, Orientation. Each has its Final
 * confidence (Member Verification: the average of Member Name and Member DOB), banded as
 * everywhere: High >= 0.80, Medium 0.50-0.80, Low < 0.50. A feature with no confidence
 * (nothing read, a default value) is not counted.
 *
 * Page (2026-11-22, user: like an NPS, as the chart): score = (High fields - Low fields) / fields
 * counted, -1..+1 (Medium counts 0); its % = (1 + score) / 2, at least the 20% floor, banded like
 * any confidence. A Blank / Junk or duplicate page counts only what matters for it: Blank / Junk
 * and Codability.
 *
 * Chart (2026-11-23, user): score = the pages' levels averaged with weights High 1.0, Medium 0.6,
 * Low 0, a critical page (CRITICAL_PAGE_TYPES, its Final page type) counting twice; the level is
 * that score banded (High 80%+, Medium 50-80%, Low under 50%; no guardrails since 2026-11-23).
 */
import type { ImagingPageResult } from "./api";

export type Level = "high" | "medium" | "low";

export const HIGH_AT = 0.8;
export const LOW_BELOW = 0.5;
/** No confidence is shown below this (2026-11-19, user). */
export const FLOOR = 0.2;

/** NPS-like score from High / Medium / Low counts: a page over its fields (2026-11-22, user). */
export function npsScore(counts: Record<Level, number>): { nps: number; value: number; level: Level } | null {
  const n = counts.high + counts.medium + counts.low;
  if (!n) return null;
  const nps = (counts.high - counts.low) / n;
  const value = round4(Math.max(FLOOR, (1 + nps) / 2));
  return { nps, value, level: band(value) };
}
/** The % never contradicts the level (2026-11-19, user): a Medium page / chart shows at most
 * 79%, a Low one at most 49%, whatever the plain average. */
export const LEVEL_CAP: Record<Level, number> = { high: 1, medium: 0.79, low: 0.49 };

export function capToLevel(level: Level, value: number): number {
  return Math.min(value, LEVEL_CAP[level]);
}

/** A member Name / DOB / ID with no confidence -- not found on the page (2026-11-23, user). */
export const MEMBER_NOT_FOUND = 0.8;

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

/** 2026-11-24 (user: the download's % must agree with the level): every confidence is rounded to
 * the database's 4 decimals before it is banded or shown, so a float average a hair under a cut
 * (0.79999...) is 80% and High, never "80.0%" and Medium. */
export function round4(value: number): number {
  return Math.round(value * 10000) / 10000;
}

export function band(value: number): Level {
  return value >= HIGH_AT ? "high" : value >= LOW_BELOW ? "medium" : "low";
}

/** 2026-11-23 (user): per-feature cuts [High at, Low below]: Blank / Junk, Provider Signature and
 * Orientation High from 95%, Low under 75%; DOS High from 75%. Others: 80% / 50%. */
export const FEATURE_CUTS: Record<string, [number, number]> = {
  blank_junk: [0.95, 0.75], provider: [0.95, 0.75], orientation: [0.95, 0.75], dos: [0.75, 0.5],
};
/** The features with 1-point bands from 95% in the Confidence Analysis. */
export const STRICT_FEATURES = new Set(["blank_junk", "provider", "orientation"]);

/** A feature's level by its own cuts (FEATURE_CUTS), else the usual 80% / 50%. */
export function bandFor(key: string, value: number): Level {
  const [highAt, lowBelow] = FEATURE_CUTS[key] ?? [HIGH_AT, LOW_BELOW];
  return value >= highAt ? "high" : value >= lowBelow ? "medium" : "low";
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
    // 2026-11-23 (user): the average of Member Name and Member DOB (was the lower of the two); a field
    // with no confidence (not found) reads MEMBER_NOT_FOUND (80%).
    // Only on a page the Final stage scored (any Final confidence).
    const scored = Object.values(page.finalConfidences ?? {}).some((v) => typeof v === "number");
    const parts = [rawValue(page, "member_name"), rawValue(page, "member_dob")];
    if (!scored && parts.every((v) => v == null)) return null;
    return round4(parts.map((v) => v ?? MEMBER_NOT_FOUND).reduce((sum, v) => sum + v, 0) / parts.length);
  }
  const value = rawValue(page, key);
  return value == null ? null : round4(value);
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
    fields.push({ key, label, value, level: bandFor(key, value) });
  }
  const counts: Record<Level, number> = { high: 0, medium: 0, low: 0 };
  for (const f of fields) counts[f.level] += 1;
  if (!fields.length) return { level: null, value: null, nps: null, counts, fields };
  // 2026-11-22 (user): like an NPS over the page's fields, as the chart over its pages.
  const score = npsScore(counts)!;
  return { level: score.level, value: score.value, nps: score.nps, counts, fields };
}

/** Final page types (their family) whose Low page makes the whole chart Low and which count twice in
 * its score (2026-11-23, user: "any critical clinical page, e.g. Discharge Summary"). */
export const CRITICAL_PAGE_TYPES = ["Discharge Summary", "Operative Note", "Procedure Note"];
export const CHART_WEIGHTS: Record<Level, number> = { high: 1.0, medium: 0.6, low: 0 };
export const CRITICAL_WEIGHT = 2;
export function isCriticalPageType(pageType: string | null | undefined): boolean {
  const family = (pageType ?? "").split(" (")[0].trim().toLowerCase();
  return CRITICAL_PAGE_TYPES.some((t) => t.toLowerCase() === family);
}

/** A chart's (or any set of pages') confidence from its pages' levels (2026-11-23, user): the
 * weighted score (High 1.0, Medium 0.6, Low 0; a critical page counts twice), and the level is that
 * score banded like any confidence (High 80%+, Medium 50-80%, Low under 50%). No guardrails. */
export function chartScore(pages: { level: Level; critical: boolean }[]):
    { value: number; level: Level; reason: string; criticalLow: number } | null {
  if (!pages.length) return null;
  let sum = 0, weight = 0, criticalLow = 0;
  for (const p of pages) {
    const w = p.critical ? CRITICAL_WEIGHT : 1;
    sum += CHART_WEIGHTS[p.level] * w;
    weight += w;
    if (p.level === "low" && p.critical) criticalLow += 1;
  }
  // Rounded first, so a score of exactly 80% is High (not 79.99999...).
  const value = Math.round(Math.max(FLOOR, sum / weight) * 10000) / 10000;
  return { value, level: band(value), reason: "", criticalLow };
}

export type ChartConfidence = {
  level: Level | null;
  value: number | null;
  /** Kept for callers; empty since the level is the score's band (2026-11-23). */
  reason: string | null;
  /** Pages per level; pages with nothing to score are not counted. */
  counts: Record<Level, number>;
  /** Page numbers per level, for "Low: 3, 8, 12". */
  pages: Record<Level, number[]>;
};

export function chartConfidence(pages: ImagingPageResult[]): ChartConfidence {
  const counts: Record<Level, number> = { high: 0, medium: 0, low: 0 };
  const byLevel: Record<Level, number[]> = { high: [], medium: [], low: [] };
  const scored: { level: Level; critical: boolean }[] = [];
  for (const page of pages) {
    const pc = pageConfidence(page);
    if (!pc.level || pc.value == null) continue;
    counts[pc.level] += 1;
    byLevel[pc.level].push(page.pageNumber);
    scored.push({ level: pc.level, critical: isCriticalPageType(page.finalPageType) });
  }
  const score = chartScore(scored);
  if (!score) return { level: null, value: null, reason: null, counts, pages: byLevel };
  return { level: score.level, value: score.value, reason: score.reason, counts, pages: byLevel };
}

export function levelLabel(level: Level | null): string {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : "Not scored";
}
