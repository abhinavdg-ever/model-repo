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

/** Per-feature cuts [High at, Low below]: Orientation High from 90%, Low under 60% (2026-11-25,
 * user; was 95% / 75%); DOS High from 75%. Blank / Junk and Provider Signature High from 99% (2026-11-24, user; was
 * 95%), Low under 75%. Others: 80% / 50%. */
export const FEATURE_CUTS: Record<string, [number, number]> = {
  blank_junk: [0.99, 0.75], provider: [0.99, 0.75], orientation: [0.9, 0.6], dos: [0.75, 0.5],
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
  /** Page Score (2026-11-25, user): the plain average of the same fields' confidences. A number
   * beside the level only: colours and levels stay the level's. */
  score: number | null;
  counts: Record<Level, number>;
  fields: FieldConfidence[];
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** Page Type / Codability confidence curves (2026-11-24, user: a higher band must be more accurate;
 * fewer pages High is fine, High must be right). feature -> knots [[raw, measured accuracy], ...],
 * fitted on the ground truth by the Confidence Analysis and stored by the backend
 * (confidence_calibration); a raw confidence is read through its curve. None: the raw value. */
export const CALIBRATED_FEATURES = ["page_type", "codability"] as const;
export type Calibration = Record<string, number[][]>;
let calibration: Calibration = {};

export function setCalibration(table: Calibration | null | undefined): void {
  calibration = table ?? {};
}

export function currentCalibration(): Calibration {
  return calibration;
}

/** ``value`` through ``knots`` (linear between them, flat beyond the ends), 4 decimals. */
export function applyKnots(knots: number[][] | undefined, value: number): number {
  if (!knots || knots.length < 2) return value;
  const pts = [...knots].sort((a, b) => a[0] - b[0]);
  if (value <= pts[0][0]) return round4(pts[0][1]);
  if (value >= pts[pts.length - 1][0]) return round4(pts[pts.length - 1][1]);
  for (let i = 1; i < pts.length; i += 1) {
    const [x0, y0] = pts[i - 1];
    const [x1, y1] = pts[i];
    if (value <= x1) return round4(x1 <= x0 ? y0 : y0 + ((y1 - y0) * (value - x0)) / (x1 - x0));
  }
  return value;
}

/** One curve from (raw confidence, right 0 / 0.5 / 1) points: bins of about 25 pages by raw value,
 * pooled until accuracy never falls as the raw value rises (pool adjacent violators); one knot
 * per pool, (mean raw, smoothed accuracy). Fewer than 30 points: null. */
export function fitKnots(points: [number, number][], binPages = 25, minPages = 30): number[][] | null {
  if (points.length < minPages) return null;
  const sorted = [...points].sort((a, b) => a[0] - b[0]);
  const bins: [number, number, number][] = []; // [sum raw, pages, right]
  for (let start = 0; start < sorted.length; start += binPages) {
    const chunk = sorted.slice(start, start + binPages);
    const sumRaw = chunk.reduce((s, [r]) => s + r, 0);
    const right = chunk.reduce((s, [, ok]) => s + ok, 0);
    if (bins.length && chunk.length < Math.floor(binPages / 2)) {
      const last = bins[bins.length - 1];
      bins[bins.length - 1] = [last[0] + sumRaw, last[1] + chunk.length, last[2] + right];
    } else {
      bins.push([sumRaw, chunk.length, right]);
    }
  }
  const pools: [number, number, number][] = [];
  for (const b of bins) {
    pools.push([...b]);
    while (pools.length > 1) {
      const a = pools[pools.length - 2];
      const c = pools[pools.length - 1];
      if (a[2] / a[1] < c[2] / c[1]) break;
      pools.splice(pools.length - 2, 2, [a[0] + c[0], a[1] + c[1], a[2] + c[2]]);
    }
  }
  const knots = pools.map(([sumRaw, n, right]) => [round4(sumRaw / n), round4((right + 1) / (n + 2))]);
  // One pool: a flat line at the measured accuracy (two knots, so it is a curve).
  if (knots.length === 1) return [[0, knots[0][1]], [1, knots[0][1]]];
  return knots;
}

/** A Discharge Summary page's Codability is Low at most (2026-11-25, user): it always gets a look. */
export const DISCHARGE_CODABILITY_CAP = 0.49;
export function isDischargeSummary(pageType: string | null | undefined): boolean {
  return (pageType ?? "").split(" (")[0].trim().toLowerCase() === "discharge summary";
}

function fieldValue(page: ImagingPageResult, key: string): number | null {
  const raw = rawFieldValue(page, key);
  const value = raw == null || !calibration[key] ? raw : applyKnots(calibration[key], raw);
  if (value != null && key === "codability" && isDischargeSummary(page.finalPageType)) {
    return Math.min(value, DISCHARGE_CODABILITY_CAP);
  }
  return value;
}

/** A feature's confidence as the pipeline stored it (no calibration): what a refit measures. */
export function rawFieldValue(page: ImagingPageResult, key: string): number | null {
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

/** The calibrated features' raw confidences (before their curve), for a refit. */
export function rawFeatureConfidences(page: ImagingPageResult): Record<string, number> {
  const out: Record<string, number> = {};
  for (const key of CALIBRATED_FEATURES) {
    const value = rawFieldValue(page, key);
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
  if (!fields.length) return { level: null, value: null, nps: null, score: null, counts, fields };
  // 2026-11-22 (user): like an NPS over the page's fields, as the chart over its pages.
  const nps = npsScore(counts)!;
  const score = round4(fields.reduce((sum, f) => sum + f.value, 0) / fields.length);
  return { level: nps.level, value: nps.value, nps: nps.nps, score, counts, fields };
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
