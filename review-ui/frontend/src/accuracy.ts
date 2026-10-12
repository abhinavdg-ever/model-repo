import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import { blankJunkTruth, groundTruthBits, hasGroundTruth, type GtBit } from "./groundTruth";
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
 * ``comingSoon`` = shown greyed as "Coming soon", not in the chart table. */
export const METRICS: {
  id: MetricId;
  label: string;
  rule: string;
  inOverall?: boolean;
  comingSoon?: boolean;
}[] = [
  {
    id: "member",
    label: "Member Verification",
    rule: "Two or more of the labelled Name, DOB, and ID match (the only one, when one is labelled)",
  },
  {
    id: "dos",
    label: "DOS",
    rule: "Each labelled date (From, To) matches the Final DOS: Match. Matches only the Extracted DOS: Partial. Neither: No Match",
  },
  {
    id: "blankJunk",
    label: "Blank / Junk",
    rule: "Blank or junk label matches",
  },
  {
    id: "pageType",
    label: "Page Type",
    rule: "Ground-truth page type matches the Final page type or subtype, or is an accepted label for its model type (else semantic similarity > 0.8): Match. Matches only the Extracted page type, or is a broader label: Partial. Neither: No Match. Not scored on blank or junk pages",
  },
  {
    id: "codeable",
    label: "Codeable / Non-Codeable",
    rule: "Codeable label matches the Final codeability: Match. Matches only the Extracted one: Partial. Neither: No Match",
  },
  {
    id: "credentials",
    label: "Provider Credentials",
    rule: "Credentials match (MD, DO, NP…; punctuation and order ignored); some shared is May be",
  },
  {
    id: "orientation",
    label: "Orientation",
    rule: "Ground-truth rotation equals the stored text orientation (degrees the text sits clockwise: 0 / 90 / 180 / 270). Not part of the overall page match",
    inOverall: false,
    comingSoon: true,
  },
];

/** The metrics with a score on screen (the chart table's columns). */
export const LIVE_METRICS = METRICS.filter((metric) => !metric.comingSoon);

/** The metrics a page's overall match is built from. */
const OVERALL_METRICS = METRICS.filter((metric) => metric.inOverall !== false);

export type Tally = { yes: number; no: number; maybe: number; scored: number };

/** Blank or junk is the positive class. A subtype disagreement still counts as found. */
/** Counts for one "Yes" class: true / false positives and negatives. */
/** ``tpPartial``: the true positives that were only partly right (counted in
 * ``tp`` too). Recall gives each of them 0.5, like Partial in the donut. */
export type ClassRates = { tp: number; tpPartial: number; fp: number; fn: number; tn: number };

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
  blankJunkRates: ClassRates;
  /** Blank / Junk scored on the pages the ground truth calls blank or junk only. */
  blankJunkYes: Tally;
  /** Orientation scored on the pages the ground truth says are rotated (not 0). */
  orientationNonZero: Tally;
  /** "Rotated" = Yes: recall / precision of finding turned pages. */
  orientationRates: ClassRates;
  /** "Yes" class = the member's name / DOB / ID is on the page. */
  memberRates: ClassRates;
  /** Member Verification scored on the pages the ground truth says carry the member only. */
  memberYes: Tally;
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
  return { tp: 0, tpPartial: 0, fp: 0, fn: 0, tn: 0 };
}

function blankOrJunkPositive(label: string): boolean {
  return label.trim().toLowerCase().startsWith("yes");
}

/** A page the ground truth calls blank or junk: Match, Partial (other sub-type)
 * or No Match. Pages the pipeline has not judged yet are left out. */
function addBlankJunkYes(tally: Tally, bits: GtBit[]): void {
  if (!labeled(bits) || !blankOrJunkPositive(bits[0].label) || bits[0].mark === "unknown") return;
  add(tally, grade(bits));
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
 * non-zero angle still counts as caught; the exact angle is "Accuracy of non-zero". */
function addOrientationRates(
  rates: ClassRates,
  bits: GtBit[],
  angle: number | null | undefined,
): void {
  if (!labeled(bits) || bits[0].mark === "unknown") return;
  const expectedTurn = quarterTurn(bits[0].label);
  const actualTurn = quarterTurn(angle);
  if (expectedTurn === null || actualTurn === null) return;
  const expected = expectedTurn !== 0;
  const predicted = actualTurn !== 0;
  if (expected && predicted) rates.tp += 1;
  else if (expected) rates.fn += 1;
  else if (predicted) rates.fp += 1;
  else rates.tn += 1;
}

function addBlankJunk(rates: ClassRates, bits: GtBit[]): void {
  if (!labeled(bits)) return;
  const bit = bits[0];
  const expected = blankOrJunkPositive(bit.label);
  if (bit.mark === "match" || bit.mark === "partial") {
    if (expected) {
      rates.tp += 1;
      // Found, but as the other of blank / junk.
      if (bit.mark === "partial") rates.tpPartial += 1;
    } else rates.tn += 1;
    return;
  }
  if (expected) rates.fn += 1;
  else if (bit.mark === "mismatch") rates.fp += 1;
}

/** A page the ground truth says carries the member (any labelled name, DOB or
 * ID is Yes): the page's Member Verification verdict. Fields the member stage
 * did not run on are left out, as in ``addMember``. */
function addMemberYes(
  tally: Tally,
  fields: Array<[string | null | undefined, GtBit[]]>,
  verdict: Verdict | null,
): void {
  const present = fields.some(
    ([label, bits]) => yesNo(label) === "yes" && bits.length > 0 && bits[0].mark !== "unknown",
  );
  if (present) add(tally, verdict);
}

/** Member presence as a Yes / No class. A page is positive in the ground truth
 * when any labelled member field (name, DOB, ID) is Yes, and predicted positive
 * when the pipeline found any of those labelled fields. Unknown fields (the
 * member stage did not run) are left out. A found page whose Member
 * Verification is not a Match is a partial true positive (0.5 in recall). */
function addMember(
  rates: ClassRates,
  fields: Array<[string | null | undefined, GtBit[]]>,
  verdict: Verdict | null,
): void {
  const scored = fields.filter(
    ([label, bits]) => yesNo(label) != null && bits.length > 0 && bits[0].mark !== "unknown",
  );
  if (scored.length === 0) return;
  const expected = scored.some(([label]) => yesNo(label) === "yes");
  const predicted = scored.some(
    ([label, bits]) => (yesNo(label) === "yes") === (bits[0].mark === "match"),
  );
  if (expected && predicted) {
    rates.tp += 1;
    if (verdict !== "yes") rates.tpPartial += 1;
  } else if (expected) rates.fn += 1;
  else if (predicted) rates.fp += 1;
  else rates.tn += 1;
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

function idKey(value: string | null | undefined): string {
  return (value ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function pageScores(
  page: ImagingPageResult,
  sections: ImagingSectionsProcessed,
  manifestMemberId: string | null | undefined,
): {
  scores: Record<MetricId, Verdict | null>;
  blankJunk: GtBit[];
  rotation: GtBit[];
  /** The metrics a page is held to: all six, or five on a blank / junk page. */
  applicable: typeof METRICS;
  member: Array<[string | null | undefined, GtBit[]]>;
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

  // A labelled Member ID is checked against the page; otherwise the extracted
  // id is compared with the manifest, but only alongside a labelled name or DOB.
  const extractedId = idKey(page.memberId);
  const expectedId = idKey(manifestMemberId);
  const checks: Verdict[] = [];
  if (labeled(bits.memberName)) checks.push(grade(bits.memberName));
  if (labeled(bits.memberDob)) checks.push(grade(bits.memberDob));
  if (labeled(bits.memberId)) {
    checks.push(grade(bits.memberId));
  } else if (checks.length > 0 && extractedId && expectedId) {
    checks.push(extractedId === expectedId ? "yes" : "no");
  }
  const hits = checks.filter((item) => item === "yes").length;
  const member: Verdict | null =
    checks.length === 0
      ? null
      : hits >= Math.min(2, checks.length)
        ? "yes"
        : checks.some((item) => item === "maybe") && checks.every((item) => item !== "no")
          ? "maybe"
          : "no";

  const dos = finalOrExtracted(dosVerdict(finalBits), dosVerdict(bits));

  const single = (field: GtBit[]): Verdict | null => (labeled(field) ? grade(field) : null);

  // A blank or junk page has no page type to score: the ground truth says so,
  // or, when it has no blank / junk label, the pipeline does.
  const truth = page.groundTruth ? blankJunkTruth(page.groundTruth) : null;
  const blankOrJunk = truth
    ? truth.toLowerCase().startsWith("yes")
    : (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes");

  // The ground-truth Provider Credentials label is stored in providerSignature.
  const credentials = credentialsVerdict(page.groundTruth?.providerSignature, page.providerCredentials);

  return {
    scores: {
      member,
      dos,
      blankJunk: single(bits.blankJunk),
      pageType: blankOrJunk
        ? null
        : finalOrExtracted(single(finalBits.pageType), single(bits.pageType)),
      codeable: finalOrExtracted(single(finalBits.codeable), single(bits.codeable)),
      credentials,
      orientation: single(bits.rotation),
    },
    blankJunk: bits.blankJunk,
    rotation: bits.rotation,
    applicable: blankOrJunk
      ? OVERALL_METRICS.filter((metric) => metric.id !== "pageType")
      : OVERALL_METRICS,
    member: [
      [page.groundTruth?.memberName, bits.memberName],
      [page.groundTruth?.memberDob, bits.memberDob],
      [page.groundTruth?.memberId, bits.memberId],
    ] as Array<[string | null | undefined, GtBit[]]>,
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
  const blankJunkYes = emptyTally();
  const orientationNonZero = emptyTally();
  const orientationRates = emptyRates();
  const memberRates = emptyRates();
  const memberYes = emptyTally();
  const overall = emptyTally();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections, doc.manifest?.memberId);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    add(overall, overallVerdict(scored.scores, scored.applicable));
    addBlankJunk(blankJunkRates, scored.blankJunk);
    addBlankJunkYes(blankJunkYes, scored.blankJunk);
    addOrientationNonZero(orientationNonZero, scored.rotation);
    addOrientationRates(orientationRates, scored.rotation, page.orientationAngle);
    addMember(memberRates, scored.member, scored.scores.member);
    addMemberYes(memberYes, scored.member, scored.scores.member);
  }
  return {
    chartId,
    chartName,
    metrics,
    overall,
    blankJunkRates,
    blankJunkYes,
    orientationNonZero,
    orientationRates,
    memberRates,
    memberYes,
  };
}

export function sumRates(rows: ClassRates[]): ClassRates {
  const total = emptyRates();
  for (const row of rows) {
    total.tp += row.tp;
    total.tpPartial += row.tpPartial;
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
  // A partly right catch counts 0.5, as Partial does in the donut.
  return ratePercent(rates.tp - 0.5 * rates.tpPartial, rates.tp + rates.fn);
}

/** Share of pages where the Yes / No call was right: (TP + TN) / all. */
export function formatClassAccuracy(rates: ClassRates): string {
  return ratePercent(rates.tp + rates.tn, rates.tp + rates.tn + rates.fp + rates.fn);
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

export function formatDistribution(tally: Tally): string {
  if (tally.scored === 0) return "—";
  return `Match ${tally.yes} · Partial ${tally.maybe} · No Match ${tally.no}`;
}
