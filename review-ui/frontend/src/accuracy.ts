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
    rule: "Name and DOB presence labels against what the pipeline found (or did not find): both right = Match, one right = Partial, neither = No Match. Not Available = a page whose Name or DOB label is NA / Not Available (or missing), or one the member stage did not judge (blank / junk); it is counted, not scored. Member ID is not scored (no ground truth). Under Member & DOB = Yes (cases = pages the ground truth has both on): Precision, Recall and F1",
  },
  {
    id: "dos",
    label: "DOS",
    rule: "Every page is compared. Labelled dates (From, To) match the Final DOS: Match; only the Extracted DOS: Partial; neither: No Match. A page whose DOS label is blank / NA / Not Available expects no date: the pipeline finding none is a Match, finding one is a No Match. Under Date = Yes (cases = pages the ground truth dates): Precision = of the pages the pipeline dated, the share right; Recall = of the cases, the share found",
  },
  {
    id: "blankJunk",
    label: "Blank / Junk",
    binary: true,
    rule: "Blank / Junk (Yes) or not (No) matches the ground truth, on every labelled page. Blank vs junk, and the junk sub-type, do not count: Match or No Match, no Partial. Under Blank/Junk = Yes (cases = pages the ground truth calls blank / junk): Precision, Recall and F1",
  },
  {
    id: "pageType",
    label: "Page Type",
    rule: "Scored on the pages the ground truth says are not blank / junk. Ground-truth page type matches the Final page type or subtype, or is an accepted label for its model type (else semantic similarity > 0.8): Match. Matches only the Extracted page type, or is a broader label: Partial. Neither: No Match. A page the pipeline called blank / junk has the type Blank/Junk: No Match",
  },
  {
    id: "codeable",
    label: "Codable / Non-Codable",
    tableLabel: "Codability",
    rule: "Every labelled page. Codable label matches the Final codability: Match. Matches only the Extracted one: Partial. Neither: No Match. Discharge counts as Codable on both sides; a page the pipeline called blank / junk is Non-Codable",
  },
  {
    id: "credentials",
    label: "Provider Credentials",
    binary: true,
    rule: "Only pages where the ground truth or the pipeline gives credentials are compared. Any one credential shared is a Match (MD, DO, NP…; punctuation, order and separators ignored: 'MD | NP' against 'MD' matches). A page whose label is blank / NA / Not Available / None but where the pipeline extracted some is a No Match. Match or No Match, no Partial. Under Credentials = Yes (cases = pages the ground truth gives credentials): Precision = of the pages the pipeline gave credentials, the share right; Recall = of the cases, the share found",
  },
  {
    id: "orientation",
    label: "Orientation",
    binary: true,
    rule: "Accuracy = of every page with an orientation label, the share where the stored text orientation equals the ground truth (0 / 90 / 180 / 270, degrees the text sits counter-clockwise); an upright page left upright is a match. Under Rotated (not 0°) = Yes (cases = pages the ground truth calls rotated): Precision = of the pages the pipeline turned, the share at the right angle (an upright page turned by mistake counts against it); Recall = of the rotated pages, the share found at the right angle. A label that is not a quarter turn (12°) is not scored. Not part of the overall page match",
    inOverall: false,
  },
];

/** The metrics with a score on screen (the chart table's columns). */
export const LIVE_METRICS = METRICS.filter((metric) => !metric.comingSoon);

/** The metrics a page's overall match is built from. */
const OVERALL_METRICS = METRICS.filter((metric) => metric.inOverall !== false);

/** yes = Match, maybe = Partial, no = No Match; na = Not Available (counted, not scored). */
export type Tally = { yes: number; no: number; maybe: number; scored: number; na: number };

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
  /** Class 1 = a rotated page (not 0°). */
  orientationRates: ClassRates;
  /** Class 1 = both the member's Name and DOB are on the page. */
  memberRates: ClassRates;
  /** Class 1 = the ground truth gives the provider's credentials. */
  credentialsRates: ClassRates;
  /** Class 1 = the ground truth dates the page. */
  dosRates: ClassRates;
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
  return { yes: 0, no: 0, maybe: 0, scored: 0, na: 0 };
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

/** Class 1 = a rotated page (not 0°). Rotated in the ground truth and found at
 * the same angle = TP; reported upright = FN; another angle = FP + FN (a wrong
 * call that also misses the page). Upright in the ground truth but turned = FP.
 * Upright on both sides = TN. */
function addOrientationRates(
  rates: ClassRates,
  bits: GtBit[],
  angle: number | null | undefined,
): void {
  if (!labeled(bits) || bits[0].mark === "unknown") return;
  const expectedTurn = quarterTurn(bits[0].label);
  const actualTurn = quarterTurn(angle);
  if (expectedTurn === null || actualTurn === null) return;
  if (expectedTurn === 0) {
    if (actualTurn === 0) rates.tn += 1;
    else rates.fp += 1;
  } else if (actualTurn === expectedTurn) {
    rates.tp += 1;
  } else if (actualTurn === 0) {
    rates.fn += 1;
  } else {
    rates.fp += 1;
    rates.fn += 1;
  }
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
/** Ground-truth labels that mean "not available" rather than Yes / No. */
const NOT_AVAILABLE = new Set(["", "na", "n/a", "not available"]);

function memberCall(
  name: [string | null | undefined, GtBit[]],
  dob: [string | null | undefined, GtBit[]],
): { expected: [boolean, boolean]; predicted: [boolean, boolean] } | null {
  const sides = [name, dob].map(([label, bits]) => {
    // NA / Not Available (or no label) is not a Yes / No: the page is Not Available.
    if (NOT_AVAILABLE.has((label ?? "").trim().toLowerCase())) return null;
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
  dos: PresenceCall | null;
  credentials: PresenceCall | null;
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

  // Name and DOB presence: both right = Match, one = Partial, neither = No
  // Match. Member ID has no ground truth.
  const memberCalls = memberCall(
    [page.groundTruth?.memberName, bits.memberName],
    [page.groundTruth?.memberDob, bits.memberDob],
  );
  const memberRight = memberCalls
    ? memberCalls.expected.filter((value, index) => value === memberCalls.predicted[index]).length
    : null;
  const member: Verdict | null =
    memberRight === null ? null : memberRight === 2 ? "yes" : memberRight === 1 ? "maybe" : "no";
  const blankJunkCalls = blankJunkCall(bits.blankJunk);

  // DOS on every page: a ground truth with no date expects none.
  const pipelineDated = Boolean(finalFrom || finalTo);
  const gtDated = !noValue(page.groundTruth?.dosFrom) || !noValue(page.groundTruth?.dosTo);
  const dosCall: PresenceCall | null = gtDated
    ? (() => {
        const verdict = finalOrExtracted(dosVerdict(finalBits), dosVerdict(bits));
        return verdict ? { expected: true, predicted: pipelineDated, verdict } : null;
      })()
    : { expected: false, predicted: pipelineDated, verdict: pipelineDated ? "no" : "yes" };
  const dos = dosCall?.verdict ?? null;

  const single = (field: GtBit[]): Verdict | null => (labeled(field) ? grade(field) : null);

  // Page type is scored on the pages the ground truth says are not blank /
  // junk. One the pipeline called blank / junk has the type Blank/Junk: a miss.
  const truth = page.groundTruth ? blankJunkTruth(page.groundTruth) : null;
  const blankOrJunk = (truth ?? "").toLowerCase().startsWith("yes");
  const pipelineBlankJunk = (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes");
  const pageTypeLabelled = labeled(bits.pageType) || labeled(finalBits.pageType);

  // Codability on every labelled page; Discharge is Codable. A page the
  // pipeline called blank / junk carries no codability: it is Non-Codable.
  const codeableBits = (value: string | null | undefined) =>
    groundTruthBits({
      ...input,
      isCodeable: pipelineBlankJunk && !text(value) ? "Non-Codable" : value,
      codeableKnown: input.codeableKnown || pipelineBlankJunk,
    }).codeable;
  const codeable = finalOrExtracted(
    single(codeableBits(text(page.finalCodeable) || page.isCodeable)),
    single(codeableBits(page.isCodeable)),
  );

  const credentialsCalled = credentialsCall(page);

  return {
    scores: {
      member,
      dos,
      blankJunk: blankJunkCalls
        ? blankJunkCalls.expected === blankJunkCalls.predicted ? "yes" : "no"
        : null,
      pageType: blankOrJunk || !pageTypeLabelled
        ? null
        : pipelineBlankJunk
          ? "no"
          : finalOrExtracted(single(finalBits.pageType), single(bits.pageType)),
      codeable,
      credentials: credentialsCalled?.verdict ?? null,
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
    dos: dosCall,
    credentials: credentialsCalled,
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
/** A ground-truth value that says "nothing here": blank, NA, Not Available, None. */
const NO_VALUE = new Set(["", "na", "n/a", "not available", "not applicable", "not found", "none"]);

function noValue(value: string | null | undefined): boolean {
  return NO_VALUE.has((value ?? "").trim().toLowerCase());
}

/** A field compared on every page: whether the ground truth and the pipeline
 * have a value, and the verdict. */
type PresenceCall = { expected: boolean; predicted: boolean; verdict: Verdict };

/** Class 1 = the ground truth has a value. Right = TP; none found = FN; a wrong
 * value = FP + FN (as a wrong angle is for Orientation); one where the ground
 * truth has none = FP; neither = TN. */
function addPresenceRates(rates: ClassRates, call: PresenceCall | null): void {
  if (!call) return;
  if (call.expected && call.verdict === "yes") rates.tp += 1;
  else if (call.expected && !call.predicted) rates.fn += 1;
  else if (call.expected) {
    rates.fp += 1;
    rates.fn += 1;
  } else if (call.predicted) rates.fp += 1;
  else rates.tn += 1;
}

/** Credentials where either side gives some. Old rows with only a Yes / No signature label
 * say nothing about credentials and are left out. */
function credentialsCall(page: ImagingPageResult): PresenceCall | null {
  const gt = page.groundTruth;
  const label = gtCredentials(gt);
  if (!label && yesNo(gt?.providerSignature) === "yes") return null;
  const predicted = credentialTokens(page.providerCredentials).size > 0;
  // Only pages where the ground truth or the pipeline gives credentials are scored.
  if (noValue(label)) return predicted ? { expected: false, predicted, verdict: "no" } : null;
  const verdict = credentialsVerdict(label, page.providerCredentials);
  if (!verdict) return null;
  return { expected: true, predicted, verdict: verdict === "maybe" ? "yes" : verdict };
}

export function credentialsVerdict(
  expected: string | null | undefined,
  actual: string | null | undefined,
): Verdict | null {
  const folded = (expected ?? "").trim().toLowerCase();
  if (!folded || yesNo(folded) != null || noValue(folded)) {
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
  const orientationRates = emptyRates();
  const memberRates = emptyRates();
  const credentialsRates = emptyRates();
  const dosRates = emptyRates();
  const overall = emptyTally();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    // A page with ground truth that Member cannot score is Not Available.
    if (!scored.scores.member) metrics.member.na += 1;
    addPresenceRates(credentialsRates, scored.credentials);
    addPresenceRates(dosRates, scored.dos);

    add(overall, overallVerdict(scored.scores, scored.applicable));
    if (scored.blankJunk) {
      addRates(blankJunkRates, scored.blankJunk.expected, scored.blankJunk.predicted);
    }
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
    orientationRates,
    memberRates,
    credentialsRates,
    dosRates,
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

/** F1 as a whole percent; null when there is nothing to score. */
export function f1Percent(rates: ClassRates): number | null {
  const total = 2 * rates.tp + rates.fp + rates.fn;
  return total === 0 ? null : Math.round((200 * rates.tp) / total);
}

export function sumTallies(rows: Tally[]): Tally {
  const total = emptyTally();
  for (const row of rows) {
    total.yes += row.yes;
    total.no += row.no;
    total.maybe += row.maybe;
    total.scored += row.scored;
    total.na += row.na;
  }
  return total;
}

export function formatDistribution(tally: Tally, binary = false): string {
  const na = tally.na > 0 ? ` · Not Available ${tally.na}` : "";
  if (tally.scored === 0) return na ? na.slice(3) : "—";
  if (binary) return `Match ${tally.yes} · No Match ${tally.no}${na}`;
  return `Match ${tally.yes} · Partial ${tally.maybe} · No Match ${tally.no}${na}`;
}
