/** Page and chart confidence levels (2026-11-19, user): High pages can skip review, Low pages
 * are looked at carefully.
 *
 * Each field the accuracy scores has its own Final confidence, banded as everywhere:
 * High >= 0.80, Medium 0.50-0.80, Low < 0.50. A field with no confidence (nothing read,
 * a default value) is not counted.
 *
 * Page: **High** when every counted field is High; **Low** when a key field (Member Name,
 * Member DOB, DOS, Page Type, Blank / Junk) is Low or two or more fields are Low; else
 * **Medium**. A Blank / Junk or duplicate page counts only what matters for it: Blank / Junk
 * and Codability. Its number is the mean of the counted fields.
 *
 * Chart: **High** when every page is High; **Low** when Low pages are LOW_SHARE (10%) of the
 * pages or more; else **Medium**. Its number is the mean of its pages' numbers.
 */
import type { ImagingPageResult } from "./api";

export type Level = "high" | "medium" | "low";

export const HIGH_AT = 0.8;
export const LOW_BELOW = 0.5;
export const LOW_SHARE = 0.1;

/** The fields counted, in display order: [key in finalConfidences, label]. */
export const CONFIDENCE_FIELDS: [string, string][] = [
  ["member_name", "Member Name"],
  ["member_dob", "Member DOB"],
  ["dos", "DOS"],
  ["blank_junk", "Blank / Junk"],
  ["page_type", "Page Type"],
  ["codability", "Codability"],
  ["provider", "Provider Signature"],
  ["orientation", "Text Orientation"],
];
const KEY_FIELDS = new Set(["member_name", "member_dob", "dos", "page_type", "blank_junk"]);
const BLANK_JUNK_FIELDS = new Set(["blank_junk", "codability"]);

export function band(value: number): Level {
  return value >= HIGH_AT ? "high" : value >= LOW_BELOW ? "medium" : "low";
}

export type FieldConfidence = { key: string; label: string; value: number; level: Level };
export type PageConfidence = {
  level: Level | null;
  value: number | null;
  counts: Record<Level, number>;
  fields: FieldConfidence[];
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function fieldValue(page: ImagingPageResult, key: string): number | null {
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
  if (!fields.length) return { level: null, value: null, counts, fields };
  const keyLow = fields.some((f) => f.level === "low" && KEY_FIELDS.has(f.key));
  const level: Level = counts.low === 0 && counts.medium === 0
    ? "high"
    : keyLow || counts.low >= 2 ? "low" : "medium";
  const value = fields.reduce((sum, f) => sum + f.value, 0) / fields.length;
  return { level, value, counts, fields };
}

export type ChartConfidence = {
  level: Level | null;
  value: number | null;
  /** Pages per level; pages with nothing to score are not counted. */
  counts: Record<Level, number>;
  /** Page numbers per level, for "Low: 3, 8, 12". */
  pages: Record<Level, number[]>;
};

export function chartConfidence(pages: ImagingPageResult[]): ChartConfidence {
  const counts: Record<Level, number> = { high: 0, medium: 0, low: 0 };
  const byLevel: Record<Level, number[]> = { high: [], medium: [], low: [] };
  let sum = 0;
  let n = 0;
  for (const page of pages) {
    const pc = pageConfidence(page);
    if (!pc.level || pc.value == null) continue;
    counts[pc.level] += 1;
    byLevel[pc.level].push(page.pageNumber);
    sum += pc.value;
    n += 1;
  }
  if (!n) return { level: null, value: null, counts, pages: byLevel };
  const level: Level = counts.high === n ? "high" : counts.low / n >= LOW_SHARE ? "low" : "medium";
  return { level, value: sum / n, counts, pages: byLevel };
}

export function levelLabel(level: Level | null): string {
  return level ? level.charAt(0).toUpperCase() + level.slice(1) : "Not scored";
}
