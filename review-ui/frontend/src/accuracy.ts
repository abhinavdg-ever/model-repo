import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import {
  blankJunkTruth,
  groundTruthBits,
  gtCredentials,
  hasGroundTruth,
  type GtBit,
} from "./groundTruth";
import { splitPageType } from "./ImagingPanel";

export type Verdict = "yes" | "maybe" | "no";

export type MetricId =
  | "member"
  | "dos"
  | "blankJunk"
  | "pageType"
  | "codeable"
  | "credentials"
  | "orientation";

/** ``inOverall: false`` = scored, but not part of the page's overall match.
 * ``binary`` = Match or No Match only, never Partial.
 * ``comingSoon`` = shown greyed as "Coming soon", not in the chart table. */
export const METRICS: {
  id: MetricId;
  label: string;
  /** Shorter header for the By chart table, where it differs. */
  tableLabel?: string;
  rule: string;
  inOverall?: boolean;
  binary?: boolean;
  comingSoon?: boolean;
}[] = [
  {
    id: "member",
    label: "Member Verification",
    binary: true,
    rule: "Exact match on the Name and DOB presence labels: a page is a Match only when the pipeline found (or did not find) the Name and the DOB exactly as the ground truth says. Scored on pages with both labels. Member ID is not scored (no ground truth)",
  },
  {
    id: "dos",
    label: "DOS",
    rule: "Each labelled date (From, To) matches the Final DOS: Match. Matches only the Extracted DOS: Partial. Neither: No Match",
  },
  {
    id: "blankJunk",
    label: "Blank / Junk",
    binary: true,
    rule: "Blank / Junk (Yes) or not (No) matches the ground truth. Blank vs junk, and the junk sub-type, do not count: Match or No Match, no Partial",
  },
  {
    id: "pageType",
    label: "Page Type",
    rule: "Ground-truth page type matches the Final page type or subtype, or is an accepted label for its model type (else semantic similarity > 0.8): Match. Matches only the Extracted page type, or is a broader label: Partial. Neither: No Match. Not scored on blank or junk pages",
  },
  {
    id: "codeable",
    label: "Codable / Non-Codable",
    tableLabel: "Codability",
    rule: "Codable label matches the Final codability: Match. Matches only the Extracted one: Partial. Neither: No Match. A page the ground truth or the pipeline calls Discharge is not scored",
  },
  {
    id: "credentials",
    label: "Provider Credentials",
    binary: true,
    rule: "Credentials match (MD, DO, NP…; punctuation and order ignored); some shared also counts as a Match. Match or No Match, no Partial",
  },
  {
    id: "orientation",
    label: "Orientation",
    binary: true,
    rule: "Ground-truth rotation equals the stored text orientation (degrees the text sits clockwise: 0 / 90 / 180 / 270), on every labelled page. Under Rotated (not 0°) = Yes, Accuracy is the same score on the rotated pages only. A label that is not a quarter turn (12°) is not scored. Not part of the overall page match",
    inOverall: false,
  },
];

/** The metrics with a score on screen (the chart table's columns). */
export const LIVE_METRICS = METRICS.filter((metric) => !metric.comingSoon);

/** The metrics a page's overall match is built from. */
const OVERALL_METRICS = METRICS.filter((metric) => metric.inOverall !== false);

export type Tally = { yes: number; no: number; maybe: number; scored: number };

/** Counts for Class 1 (the "Yes" class): true / false positives and negatives. */
export type ClassRates = { tp: number; fp: number; fn: number; tn: number };

/** Charts whose last run is before this date are left out of the report. */
export const MIN_LAST_RUN = "2026-10-12";
/** MIN_LAST_RUN as people read it ("12 Oct 2026"), for the page's text. */
export const MIN_LAST_RUN_LABEL = new Intl.DateTimeFormat("en-GB", {
  day: "numeric",
  month: "short",
  year: "numeric",
  timeZone: "UTC",
}).format(new Date(`${MIN_LAST_RUN}T00:00:00Z`));

export type ChartAccuracy = {
  chartId: string;
  chartName: string;
  lastUpdatedAt?: string | null;
  lastVerifiedAt?: string | null;
  metrics: Record<MetricId, Tally>;
  overall: Tally;
  /** Class 1 = a blank or junk page. */
  blankJunkRates: ClassRates;
  /** Orientation scored on the pages the ground truth says are rotated (not 0°). */
  orientationNonZero: Tally;
  /** Class 1 = a rotated page (not 0°). */
  orientationRates: ClassRates;
  /** Class 1 = both the member's Name and DOB are on the page. */
  memberRates: ClassRates;
};

const EMPTY_SECTIONS: ImagingSectionsProcessed = {
  member: false,
  dos: false,
  hw: false,
  quality: false,
  rotation: false,
  junk: false,
  codeable: false,
  encounter: false,
  sequencing: false,
  verification: false,
};

function emptyTally(): Tally {
  return { yes: 0, no: 0, maybe: 0, scored: 0 };
}

function emptyRates(): ClassRates {
  return { tp: 0, fp: 0, fn: 0, tn: 0 };
}

function addRates(rates: ClassRates, expected: boolean, predicted: boolean): void {
  if (expected && predicted) rates.tp += 1;
  else if (expected) rates.fn += 1;
  else if (predicted) rates.fp += 1;
  else rates.tn += 1;
}

/** 0..359 from "270", "270°" or "-90"; null when it is not a number. */
function quarterTurn(value: string | number | null | undefined): number | null {
  const n = typeof value === "number" ? value : Number(String(value ?? "").trim().replace(/°$/, ""));
  if (!Number.isFinite(n)) return null;
  return ((Math.round(n) % 360) + 360) % 360;
}

/** A page the ground truth says is rotated (90 / 180 / 270): Match or No Match. */
function addOrientationNonZero(tally: Tally, bits: GtBit[]): void {
  if (!labeled(bits) || bits[0].mark === "unknown") return;
  const expected = quarterTurn(bits[0].label);
  if (expected === null || expected === 0) return;
  add(tally, grade(bits));
}

/** "Rotated" (not 0) as a Yes / No class. A rotated page reported at another
 * non-zero angle still counts as caught; the exact angle is the Orientation score. */
function addOrientationRates(
  rates: ClassRates,
  bits: GtBit[],
  angle: number | null | undefined,
): void {
  if (!labeled(bits) || bits[0].mark === "unknown") return;
  const expectedTurn = quarterTurn(bits[0].label);
  const actualTurn = quarterTurn(angle);
  if (expectedTurn === null || actualTurn === null) return;
  addRates(rates, expectedTurn !== 0, actualTurn !== 0);
}

/** Blank / junk as Yes / No: blank vs junk and the junk sub-type do not count.
 * A page the pipeline has not judged yet is left out. */
function blankJunkCall(bits: GtBit[]): { expected: boolean; predicted: boolean } | null {
  if (!labeled(bits) || bits[0].mark === "unknown") return null;
  const expected = bits[0].label.trim().toLowerCase().startsWith("yes");
  // "partial" = both sides say Yes, as the other of blank / junk.
  const right = bits[0].mark === "match" || bits[0].mark === "partial";
  return { expected, predicted: right ? expected : !expected };
}

/** Name and DOB presence: what the ground truth says and what the pipeline
 * found, for each. Null unless both are labelled Yes / No and the member stage ran. */
function memberCall(
  name: [string | null | undefined, GtBit[]],
  dob: [string | null | undefined, GtBit[]],
): { expected: [boolean, boolean]; predicted: [boolean, boolean] } | null {
  const sides = [name, dob].map(([label, bits]) => {
    const expected = yesNo(label);
    if (expected === null || !labeled(bits) || bits[0].mark === "unknown") return null;
    // "match" = the pipeline found it exactly when the ground truth says Yes.
    const found = (expected === "yes") === (bits[0].mark === "match");
    return { expected: expected === "yes", found };
  });
  if (sides[0] === null || sides[1] === null) return null;
  return {
    expected: [sides[0].expected, sides[1].expected],
    predicted: [sides[0].found, sides[1].found],
  };
}

function add(tally: Tally, verdict: Verdict | null) {
  if (!verdict) return;
  tally[verdict] += 1;
  tally.scored += 1;
}

/** Accuracy score = (Match + 0.5 × Partial) / (Match + Partial + No Match). */
export function accuracyPercent(tally: Tally): number | null {
  if (tally.scored === 0) return null;
  return Math.round((100 * (tally.yes + 0.5 * tally.maybe)) / tally.scored);
}

function matched(bits: GtBit[]): boolean {
  return bits.some((bit) => bit.mark === "match");
}

function partial(bits: GtBit[]): boolean {
  return bits.some((bit) => bit.mark === "partial");
}

function labeled(bits: GtBit[]): boolean {
  return bits.length > 0;
}

function grade(bits: GtBit[]): Verdict {
  if (matched(bits)) return "yes";
  if (partial(bits)) return "maybe";
  return "no";
}

function isDischarge(value: string | null | undefined): boolean {
  return (value ?? "").trim().toLowerCase().startsWith("discharge");
}

function text(value: string | null | undefined): string {
  return (value ?? "").trim();
}

/** Each labelled date (From, To) must match exactly. A date that only shares
 * its year or month is wrong: the 0.5 is kept for "matches the Extracted DOS". */
function dosVerdict(bits: { dosFrom: GtBit[]; dosTo: GtBit[] }): Verdict | null {
  const grades = [bits.dosFrom, bits.dosTo].filter(labeled).map(grade);
  if (grades.length === 0) return null;
  return grades.every((item) => item === "yes") ? "yes" : "no";
}

/** Matching the Final value is a full match (1). Matching only the Extracted
 * value, or a partial on either, is Partial (0.5). Neither is No Match (0). */
function finalOrExtracted(final: Verdict | null, extracted: Verdict | null): Verdict | null {
  if (final === null && extracted === null) return null;
  if (final === "yes") return "yes";
  if (extracted === "yes" || final === "maybe" || extracted === "maybe") return "maybe";
  return "no";
}

function pageScores(
  page: ImagingPageResult,
  sections: ImagingSectionsProcessed,
): {
  scores: Record<MetricId, Verdict | null>;
  blankJunk: ReturnType<typeof blankJunkCall>;
  rotation: GtBit[];
  /** The metrics a page is held to: all six, or five on a blank / junk page. */
  applicable: typeof METRICS;
  member: ReturnType<typeof memberCall>;
} {
  const parts = splitPageType(page.pageType);
  const input = {
    gt: page.groundTruth,
    memberName: page.memberName,
    memberDob: page.memberDob,
    memberId: page.memberId,
    memberKnown: sections.member,
    orientationAngle: page.orientationAngle,
    rotationKnown: sections.rotation,
    dosFrom: page.dosFrom,
    dosTo: page.dosTo,
    dosKnown: sections.dos,
    blankOrJunk: page.blankOrJunk,
    junkKnown: sections.junk,
    pageType: parts.family,
    pageSubtype: parts.subtype,
    pageTypeKnown: Boolean(sections.junk || sections.codeable),
    pageTypeSemantic: page.pageTypeSemantic,
    isCodeable: page.isCodeable,
    codeableKnown:
      Boolean(sections.codeable) ||
      (page.isCodeable != null && String(page.isCodeable).trim() !== ""),
    actualSequence: page.actualSequence,
    sequenceKnown: Boolean(sections.sequencing),
  };
  const bits = groundTruthBits(input);
  // Final (imaging_final, after continuity) where the run has one; a field the
  // Final stage left empty is the Extracted value, so it scores in full.
  const finalType = text(page.finalPageType) || text(page.pageType);
  const finalParts = splitPageType(finalType);
  const finalFrom = text(page.finalDosFrom) || text(page.dosFrom);
  const finalTo = text(page.finalDosTo) || (text(page.finalDosFrom) ? text(page.finalDosFrom) : text(page.dosTo));
  const finalBits = groundTruthBits({
    ...input,
    pageType: finalParts.family,
    pageSubtype: finalParts.subtype,
    pageTypeSemantic: finalType === text(page.pageType) ? page.pageTypeSemantic : null,
    isCodeable: text(page.finalCodeable) || page.isCodeable,
    dosFrom: finalFrom || null,
    dosTo: finalTo || null,
  });

  // Exact match on Name and DOB presence; Member ID has no ground truth.
  const memberCalls = memberCall(
    [page.groundTruth?.memberName, bits.memberName],
    [page.groundTruth?.memberDob, bits.memberDob],
  );
  const member: Verdict | null = memberCalls
    ? memberCalls.expected.every((value, index) => value === memberCalls.predicted[index])
      ? "yes"
      : "no"
    : null;
  const blankJunkCalls = blankJunkCall(bits.blankJunk);

  const dos = finalOrExtracted(dosVerdict(finalBits), dosVerdict(bits));

  const single = (field: GtBit[]): Verdict | null => (labeled(field) ? grade(field) : null);

  // A blank or junk page has no page type to score: the ground truth says so,
  // or, when it has no blank / junk label, the pipeline does.
  const truth = page.groundTruth ? blankJunkTruth(page.groundTruth) : null;
  const blankOrJunk = truth
    ? truth.toLowerCase().startsWith("yes")
    : (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes");

  const credentials = credentialsVerdict(gtCredentials(page.groundTruth), page.providerCredentials);

  return {
    scores: {
      member,
      dos,
      blankJunk: blankJunkCalls
        ? blankJunkCalls.expected === blankJunkCalls.predicted ? "yes" : "no"
        : null,
      pageType: blankOrJunk
        ? null
        : finalOrExtracted(single(finalBits.pageType), single(bits.pageType)),
      // Discharge is a third type, not Codable or Non-Codable: left out.
      codeable: [page.groundTruth?.codeable, page.finalCodeable, page.isCodeable].some(isDischarge)
        ? null
        : finalOrExtracted(single(finalBits.codeable), single(bits.codeable)),
      credentials: credentials === "maybe" ? "yes" : credentials,
      // An angle that is not a quarter turn, or a page the rotation stage did not run on, is
      // not scored (it is not a miss).
      orientation: bits.rotation[0]?.mark === "unknown" ? null : single(bits.rotation),
    },
    blankJunk: blankJunkCalls,
    rotation: bits.rotation,
    applicable: blankOrJunk
      ? OVERALL_METRICS.filter((metric) => metric.id !== "pageType")
      : OVERALL_METRICS,
    member: memberCalls,
  };
}

/** A page's overall match, over the metrics it has ground truth for (page type
 * is not one on a blank / junk page, and Orientation never is): all of them
 * matching is a match, at least half is partial, fewer is a miss. */
function overallVerdict(
  scores: Record<MetricId, Verdict | null>,
  applicable: typeof METRICS = OVERALL_METRICS,
): Verdict | null {
  // Only the metrics this page has ground truth for: an unlabelled metric is
  // not a miss. (Before, a page could only fully match with all six labelled.)
  const scored = applicable.filter((metric) => scores[metric.id] != null);
  if (scored.length === 0) return null;
  const matches = scored.filter((metric) => scores[metric.id] === "yes").length;
  if (matches === scored.length) return "yes";
  if (2 * matches >= scored.length) return "maybe";
  return "no";
}

/** "M.D., Ph.D." -> {"MD", "PHD"}. */
function credentialTokens(value: string | null | undefined): Set<string> {
  return new Set(
    (value ?? "")
      .toUpperCase()
      .replace(/\./g, "")
      .split(/[^A-Z0-9]+/)
      .filter((token) => token.length > 0),
  );
}

/** Same credentials = Yes; some shared = May be; none shared or none extracted = No.
 * A label that is only Yes / No (an old signature label) or NA is not scored. */
function credentialsVerdict(
  expected: string | null | undefined,
  actual: string | null | undefined,
): Verdict | null {
  const folded = (expected ?? "").trim().toLowerCase();
  if (!folded || yesNo(folded) != null || ["na", "n/a", "not found", "none"].includes(folded)) {
    return null;
  }
  const want = credentialTokens(expected);
  if (want.size === 0) return null;
  const got = credentialTokens(actual);
  if (got.size === 0) return "no";
  const shared = [...want].filter((token) => got.has(token)).length;
  if (shared === want.size && shared === got.size) return "yes";
  return shared > 0 ? "maybe" : "no";
}

function yesNo(value: string | null | undefined): "yes" | "no" | null {
  const folded = (value ?? "").trim().toLowerCase();
  if (folded === "yes" || folded === "y" || folded === "true") return "yes";
  if (folded === "no" || folded === "n" || folded === "false") return "no";
  return null;
}

export function scoreChart(
  chartId: string,
  chartName: string,
  doc: ImagingDocumentResponse,
): ChartAccuracy {
  const sections = doc.sectionsProcessed ?? EMPTY_SECTIONS;
  const metrics = Object.fromEntries(METRICS.map((metric) => [metric.id, emptyTally()])) as Record<
    MetricId,
    Tally
  >;
  const blankJunkRates = emptyRates();
  const orientationNonZero = emptyTally();
  const orientationRates = emptyRates();
  const memberRates = emptyRates();
  const overall = emptyTally();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    add(overall, overallVerdict(scored.scores, scored.applicable));
    if (scored.blankJunk) {
      addRates(blankJunkRates, scored.blankJunk.expected, scored.blankJunk.predicted);
    }
    addOrientationNonZero(orientationNonZero, scored.rotation);
    addOrientationRates(orientationRates, scored.rotation, page.orientationAngle);
    if (scored.member) {
      // Class 1: both Name and DOB on the page.
      addRates(
        memberRates,
        scored.member.expected.every(Boolean),
        scored.member.predicted.every(Boolean),
      );
    }
  }
  return {
    chartId,
    chartName,
    metrics,
    overall,
    blankJunkRates,
    orientationNonZero,
    orientationRates,
    memberRates,
  };
}

export function sumRates(rows: ClassRates[]): ClassRates {
  const total = emptyRates();
  for (const row of rows) {
    total.tp += row.tp;
    total.fp += row.fp;
    total.fn += row.fn;
    total.tn += row.tn;
  }
  return total;
}

function ratePercent(hits: number, total: number): string {
  if (total === 0) return "—";
  return `${Math.round((100 * hits) / total)}%`;
}

export function formatPrecision(rates: ClassRates): string {
  return ratePercent(rates.tp, rates.tp + rates.fp);
}

export function formatRecall(rates: ClassRates): string {
  return ratePercent(rates.tp, rates.tp + rates.fn);
}

/** F1 = 2 × Precision × Recall / (Precision + Recall) = 2TP / (2TP + FP + FN). */
export function formatF1(rates: ClassRates): string {
  return ratePercent(2 * rates.tp, 2 * rates.tp + rates.fp + rates.fn);
}

export function sumTallies(rows: Tally[]): Tally {
  const total = emptyTally();
  for (const row of rows) {
    total.yes += row.yes;
    total.no += row.no;
    total.maybe += row.maybe;
    total.scored += row.scored;
  }
  return total;
}

export function formatDistribution(tally: Tally, binary = false): string {
  if (tally.scored === 0) return "—";
  if (binary) return `Match ${tally.yes} · No Match ${tally.no}`;
  return `Match ${tally.yes} · Partial ${tally.maybe} · No Match ${tally.no}`;
}
