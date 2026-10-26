import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import {
  blankJunkTruth,
  groundTruthBits,
  gtCredentials,
  gtSignature,
  hasGroundTruth,
  type GtBit,
} from "./groundTruth";
import { DEFAULT_DOS, splitPageType } from "./ImagingPanel";

export type Verdict = "yes" | "maybe" | "no";

export type MetricId =
  | "member"
  | "dos"
  | "blankJunk"
  | "pageType"
  | "codeable"
  | "signature"
  | "orientation";

/** ``inOverall: false`` = scored, but not part of the page's overall match.
 * ``binary`` = Match or No Match only, never Partial (only Page Type has a Partial).
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
    rule: "Name and DOB presence labels against what the pipeline found (or did not find): both right = Match, else No Match. Not Available = a page whose Name or DOB label is NA / Not Available (or missing), or one the member stage did not judge (blank / junk); it is counted, not scored. Member ID is not scored (no ground truth). Under Name & DOB = Yes (cases = pages the ground truth has both on): Precision, Recall and F1",
  },
  {
    id: "dos",
    label: "DOS",
    binary: true,
    rule: "Every page is compared, except a blank / junk page (by the ground truth or the pipeline) and one where the ground truth has no date and the pipeline shows the 2222-02-02 placeholder (neither is scored). Labelled dates (From, To) both match the Final DOS or both match the Extracted DOS: Match; else No Match. A page whose DOS label is blank / NA / Not Available expects no date: the Extracted or the Final DOS being empty is a Match (either side matching counts), both dated is a No Match. Under Date = Yes (cases = pages the ground truth dates): Precision = of the pages the pipeline dated, the share right; Recall = of the cases, the share found",
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
    rule: "Scored on the pages the ground truth says are not blank / junk. The client's label is mapped to our L1 > L2 > L3 (gtPageTypeCanon.json; the table is docs/gt_page_type_mapping.csv). Match (1): a full target is the start of our Final or Extracted path. Partial (0.5): a listed neighbour is, or our path has the same L1 (stops above the label's level, a sibling L3, another L2). Else No Match (0). Cover / index / fax / blank, NA and Misc labels are not scored. A label not in the mapping falls back to the accepted-label lists. A page the pipeline called blank / junk has the type Blank/Junk: No Match",
  },
  {
    id: "codeable",
    label: "Codable / Non-Codable",
    tableLabel: "Codability",
    binary: true,
    rule: "Every labelled page. Codable label matches the Final or the Extracted codability: Match. Neither: No Match. Discharge counts as Codable on both sides; a page the pipeline called blank / junk is Non-Codable",
  },
  {
    id: "signature",
    label: "Provider Signature",
    binary: true,
    rule: "Every labelled page, Match or No Match: the ground truth's Provider Signature (Yes / No; credentials in the label mean Yes; NA / blank is No) equals whether the pipeline found an electronic signature. Under Signature = Yes (cases = pages the ground truth or the pipeline calls signed): Precision = of the pages the pipeline called signed, the share signed; Recall = of the pages the ground truth calls signed, the share found; Cred. M (credentials match) = of the pages the ground truth gives credentials, the share whose credentials match the ground truth's (any one credential shared: MD, DO, NP…; punctuation, order and separators ignored)",
  },
  {
    id: "orientation",
    label: "Orientation",
    binary: true,
    rule: "Every page with ground truth, Match or No Match: the stored text orientation equals the ground truth (0 / 90 / 180 / 270, degrees the text sits counter-clockwise). A label of NA / blank is Not Found, read as upright (0°), and so is a page with no stored angle. Under Rotated (not 0°) = Yes (pages the ground truth or the pipeline calls rotated): Precision = of the pages the pipeline turned, the share at the right angle; Recall = of the rotated pages, the share found at the right angle",
  },
];

/** The metrics with a score on screen (the chart table's columns). */
export const LIVE_METRICS = METRICS.filter((metric) => !metric.comingSoon);

/** The metrics a page's overall match is built from: the seven streams. */
const OVERALL_METRICS = METRICS.filter((metric) => metric.inOverall !== false);
export const OVERALL_STREAMS = OVERALL_METRICS.length;

/** yes = Match, maybe = Partial, no = No Match; na = Not Available (counted, not scored).
 * Overall only: ``matched[k]`` = pages with k of the seven streams matching (0..7), and
 * ``fullOnly`` = the accuracy counts full matches only (7 of 7; a Partial scores 0). */
export type Tally = {
  yes: number;
  no: number;
  maybe: number;
  scored: number;
  na: number;
  matched?: number[];
  fullOnly?: boolean;
};

/** Counts for Class 1 (the "Yes" class): true / false positives and negatives. */
export type ClassRates = {
  tp: number;
  fp: number;
  fn: number;
  tn: number;
  /** Provider Signature only: pages the ground truth gives credentials for, and of
   * them, those whose extracted credentials match. */
  credCases?: number;
  credMatch?: number;
};

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
  /** Run / Batch and chart metadata, for the filters above the By chart table. */
  runId?: string | null;
  batchId?: string | null;
  projectScope?: string | null;
  sourceName?: string | null;
  measure?: string | null;
  rrMethod?: string | null;
  metrics: Record<MetricId, Tally>;
  overall: Tally;
  /** Class 1 = a blank or junk page. */
  blankJunkRates: ClassRates;
  /** Class 1 = a rotated page (not 0°). */
  orientationRates: ClassRates;
  /** Class 1 = both the member's Name and DOB are on the page. */
  memberRates: ClassRates;
  /** Class 1 = the ground truth says the page is signed; credMatch counts its credentials. */
  signatureRates: ClassRates;
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

/** The ground truth's and the pipeline's text orientation, in degrees. A ground truth of
 * NA / blank (Not Found) is upright, 0°; so is a page with no stored angle. */
function orientationTurns(page: ImagingPageResult): { expected: number; actual: number } {
  const label = page.groundTruth?.rotation;
  return {
    expected: noValue(label) ? 0 : quarterTurn(label) ?? 0,
    actual: quarterTurn(page.orientationAngle) ?? 0,
  };
}

/** Class 1 = a rotated page (not 0°). Rotated in the ground truth and found at
 * the same angle = TP; reported upright = FN; another angle = FP + FN (a wrong
 * call that also misses the page). Upright in the ground truth but turned = FP.
 * Upright on both sides = TN. */
function addOrientationRates(rates: ClassRates, page: ImagingPageResult): void {
  const { expected: expectedTurn, actual: actualTurn } = orientationTurns(page);
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
  // Blank against Junk is a match (both Yes).
  const right = bits[0].mark === "match";
  return { expected, predicted: right ? expected : !expected };
}

/** A pipeline value that says "nothing found". */
const NOT_FOUND_VALUES = new Set(["", "na", "n/a", "not found", "not available", "none"]);

function found(value: string | null | undefined): boolean {
  return !NOT_FOUND_VALUES.has(text(value).toLowerCase());
}

/** Name and DOB on every page: [Name, DOB] as the ground truth has them (Yes is Yes; No,
 * NA, blank or anything else is No) and as the pipeline found them (extracted or not). */
function memberCall(page: ImagingPageResult): { expected: [boolean, boolean]; predicted: [boolean, boolean] } {
  const gt = page.groundTruth;
  return {
    expected: [yesNo(gt?.memberName) === "yes", yesNo(gt?.memberDob) === "yes"],
    predicted: [found(page.memberName), found(page.memberDob)],
  };
}

function add(tally: Tally, verdict: Verdict | null) {
  if (!verdict) return;
  tally[verdict] += 1;
  tally.scored += 1;
}

/** Accuracy score = (Match + 0.5 × Partial) / (Match + Partial + No Match); Overall
 * counts full matches only: Match / (Match + Partial + No Match). */
export function accuracyPercent(tally: Tally): number | null {
  if (tally.scored === 0) return null;
  const partialWeight = tally.fullOnly ? 0 : 0.5;
  return Math.round((100 * (tally.yes + partialWeight * tally.maybe)) / tally.scored);
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

/** A pipeline DOS, "" for none. The continuity stage writes DOS_DEFAULT_DATE
 * (2222-02-02) as the Final DOS of a page with no date: that is no date. */
function pipelineDate(value: string | null | undefined): string {
  const date = text(value);
  return date.startsWith(DEFAULT_DOS) ? "" : date;
}

/** Each labelled date (From, To) must match exactly. A date that only shares
 * its year or month is wrong: the 0.5 is kept for "matches the Extracted DOS". */
function dosVerdict(bits: { dosFrom: GtBit[]; dosTo: GtBit[] }): Verdict | null {
  const grades = [bits.dosFrom, bits.dosTo].filter(labeled).map(grade);
  if (grades.length === 0) return null;
  return grades.every((item) => item === "yes") ? "yes" : "no";
}

/** Page type: the Final or the Extracted value matching is a full match (1), a
 * partial on either is Partial (0.5), neither is No Match (0). */
function finalOrExtracted(final: Verdict | null, extracted: Verdict | null): Verdict | null {
  if (final === null && extracted === null) return null;
  if (final === "yes" || extracted === "yes") return "yes";
  if (final === "maybe" || extracted === "maybe") return "maybe";
  return "no";
}

/** Match when the Final or the Extracted value matches, else No Match: no Partial. */
function finalOrExtractedMatch(final: Verdict | null, extracted: Verdict | null): Verdict | null {
  if (final === null && extracted === null) return null;
  return final === "yes" || extracted === "yes" ? "yes" : "no";
}

function pageScores(
  page: ImagingPageResult,
  sections: ImagingSectionsProcessed,
): {
  scores: Record<MetricId, Verdict | null>;
  blankJunk: ReturnType<typeof blankJunkCall>;
  rotation: GtBit[];
  /** The metrics a page is held to. */
  applicable: typeof METRICS;
  member: ReturnType<typeof memberCall>;
  dos: PresenceCall;
  signature: SignatureCall;
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
    dosFrom: pipelineDate(page.dosFrom) || null,
    dosTo: pipelineDate(page.dosTo) || null,
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
  const finalFrom = pipelineDate(page.finalDosFrom) || pipelineDate(page.dosFrom);
  const finalTo =
    pipelineDate(page.finalDosTo) ||
    (pipelineDate(page.finalDosFrom) ? pipelineDate(page.finalDosFrom) : pipelineDate(page.dosTo));
  const finalBits = groundTruthBits({
    ...input,
    pageType: finalParts.family,
    pageSubtype: finalParts.subtype,
    pageTypeSemantic: finalType === text(page.pageType) ? page.pageTypeSemantic : null,
    isCodeable: text(page.finalCodeable) || page.isCodeable,
    dosFrom: finalFrom || null,
    dosTo: finalTo || null,
  });

  // Name and DOB on every page: the ground truth's Yes is Yes, anything else (No, NA,
  // blank) is No; the pipeline's is whether it extracted one. Both right = Match, else
  // No Match. Member ID has no ground truth.
  const memberCalls = memberCall(page);
  const memberRight = memberCalls.expected.filter((value, index) => value === memberCalls.predicted[index]).length;
  const member: Verdict = memberRight === 2 ? "yes" : "no";
  const blankJunkCalls = blankJunkCall(bits.blankJunk);

  const truth = page.groundTruth ? blankJunkTruth(page.groundTruth) : null;
  const blankOrJunk = (truth ?? "").toLowerCase().startsWith("yes");
  // A duplicate carries its original's values (withDuplicateValues) and is scored on
  // them: only true blank / junk pages have no content (2026-10-26, user).
  const pipelineBlankJunk =
    page.isDuplicate !== true && (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes");

  // DOS on every page, Match or No Match. A page the ground truth calls blank / junk has no
  // date (Not Found). A labelled date matches when the Final or the Extracted DOS equals it
  // (From and To). No date in the ground truth matches when the Extracted or the Final DOS is
  // empty (either side matching is a Match).
  // The 2222-02-02 placeholder is no date.
  const pipelineDated = Boolean(finalFrom || finalTo);
  const extractedDated = Boolean(pipelineDate(page.dosFrom) || pipelineDate(page.dosTo));
  const gtDated = !blankOrJunk && (!noValue(page.groundTruth?.dosFrom) || !noValue(page.groundTruth?.dosTo));
  const dosCall: PresenceCall = gtDated
    ? {
        expected: true,
        predicted: pipelineDated || extractedDated,
        verdict: dosVerdict(finalBits) === "yes" || dosVerdict(bits) === "yes" ? "yes" : "no",
      }
    : // Either undated (Final or Extracted) is a Match.
      { expected: false, predicted: extractedDated && pipelineDated, verdict: extractedDated && pipelineDated ? "no" : "yes" };
  const dos = dosCall.verdict;

  const single = (field: GtBit[]): Verdict | null => (labeled(field) ? grade(field) : null);

  // Page type on every labelled page. Blank / junk is a type of its own (Blank/Junk) on
  // both sides.
  const pageTypeLabelled = labeled(bits.pageType) || labeled(finalBits.pageType);

  // Codability on every labelled page; Discharge is Codable. A page the
  // pipeline called blank / junk carries no codability: it is Non-Codable.
  const codeableBits = (value: string | null | undefined) =>
    groundTruthBits({
      ...input,
      isCodeable: pipelineBlankJunk && !text(value) ? "Non-Codable" : value,
      codeableKnown: input.codeableKnown || pipelineBlankJunk,
    }).codeable;
  // A ground-truth Discharge page type is a Match for now.
  const gtDischarge = /discharge/i.test(text(page.groundTruth?.pageType)) || /discharge/i.test(text(page.groundTruth?.codeable));
  const codeable = gtDischarge
    ? "yes"
    : finalOrExtractedMatch(
        single(codeableBits(text(page.finalCodeable) || page.isCodeable)),
        single(codeableBits(page.isCodeable)),
      );

  const signatureCalled = signatureCall(page);

  return {
    scores: {
      member,
      dos,
      blankJunk: blankJunkCalls
        ? blankJunkCalls.expected === blankJunkCalls.predicted ? "yes" : "no"
        : null,
      // A page the ground truth calls blank / junk has the type Blank/Junk: the pipeline
      // calling it blank / junk too is a Match, anything else a No Match.
      pageType: blankOrJunk
        ? pipelineBlankJunk ? "yes" : "no"
        : !pageTypeLabelled
          ? null
          : pipelineBlankJunk
            ? "no"
            : finalOrExtracted(single(finalBits.pageType), single(bits.pageType)),
      codeable,
      signature: signatureCalled.expected === signatureCalled.predicted ? "yes" : "no",
      // Every page: a ground truth of NA (or none) is upright (0°), as is a page the rotation
      // stage left alone. Equal angles = Match, else No Match.
      orientation: orientationTurns(page).expected === orientationTurns(page).actual ? "yes" : "no",
    },
    blankJunk: blankJunkCalls,
    rotation: bits.rotation,
    applicable: OVERALL_METRICS,
    member: memberCalls,
    dos: dosCall,
    signature: signatureCalled,
  };
}

/** A page's overall match over the seven streams: ``matches`` = how many match (0..7).
 * A Page Type Partial is not a match; a stream with no ground truth is not a miss.
 * All seven = Match, none = No Match, anything between = Partial. */
function overallVerdict(
  scores: Record<MetricId, Verdict | null>,
  applicable: typeof METRICS = OVERALL_METRICS,
): { verdict: Verdict; matches: number } | null {
  if (!applicable.some((metric) => scores[metric.id] != null)) return null;
  const misses = applicable.filter((metric) => scores[metric.id] === "no" || scores[metric.id] === "maybe").length;
  const matches = applicable.length - misses;
  const verdict: Verdict = misses === 0 ? "yes" : matches === 0 ? "no" : "maybe";
  return { verdict, matches };
}

function emptyOverall(): Tally {
  return { ...emptyTally(), matched: [], fullOnly: true };
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

export function noValue(value: string | null | undefined): boolean {
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

/** Provider Signature on every labelled page: the ground truth's Yes is Yes (credentials
 * in the label mean Yes), anything else (No, NA, blank) is No; the pipeline's is whether
 * it found a signature. ``credCase`` = the ground truth gives credentials; ``credMatch`` =
 * the credentials extracted share one with them. */
type SignatureCall = { expected: boolean; predicted: boolean; credCase: boolean; credMatch: boolean };

function signatureCall(page: ImagingPageResult): SignatureCall {
  const verdict = credentialsVerdict(gtCredentials(page.groundTruth), page.providerCredentials);
  return {
    expected: gtSignature(page.groundTruth) === "Yes",
    predicted: yesNo(page.providerSignature) === "yes",
    credCase: verdict != null,
    credMatch: verdict === "yes" || verdict === "maybe",
  };
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
  const signatureRates: ClassRates = { ...emptyRates(), credCases: 0, credMatch: 0 };
  const dosRates = emptyRates();
  const overall = emptyOverall();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    addRates(signatureRates, scored.signature.expected, scored.signature.predicted);
    if (scored.signature.credCase) signatureRates.credCases = (signatureRates.credCases ?? 0) + 1;
    if (scored.signature.credMatch) signatureRates.credMatch = (signatureRates.credMatch ?? 0) + 1;
    addPresenceRates(dosRates, scored.dos);

    const pageOverall = overallVerdict(scored.scores, scored.applicable);
    if (pageOverall) {
      add(overall, pageOverall.verdict);
      overall.matched![pageOverall.matches] = (overall.matched![pageOverall.matches] ?? 0) + 1;
    }
    if (scored.blankJunk) {
      addRates(blankJunkRates, scored.blankJunk.expected, scored.blankJunk.predicted);
    }
    addOrientationRates(orientationRates, page);
    // Class 1: both Name and DOB on the page (by the ground truth or the pipeline).
    addRates(
      memberRates,
      scored.member.expected.every(Boolean),
      scored.member.predicted.every(Boolean),
    );
  }
  return {
    chartId,
    chartName,
    metrics,
    overall,
    blankJunkRates,
    orientationRates,
    memberRates,
    signatureRates,
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
    if (row.credCases != null) total.credCases = (total.credCases ?? 0) + row.credCases;
    if (row.credMatch != null) total.credMatch = (total.credMatch ?? 0) + row.credMatch;
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

/** Cred. Match = Recall on credentials: of the pages the ground truth gives credentials
 * for, the share whose extracted credentials match. */
export function formatCredMatch(rates: ClassRates): string {
  return ratePercent(rates.credMatch ?? 0, rates.credCases ?? 0);
}

export function credMatchPercent(rates: ClassRates): number | null {
  const total = rates.credCases ?? 0;
  return rates.credMatch == null || total === 0 ? null : Math.round((100 * rates.credMatch) / total);
}

/** Precision as a whole percent; null when the pipeline called no page Yes. */
export function precisionPercent(rates: ClassRates): number | null {
  const total = rates.tp + rates.fp;
  return total === 0 ? null : Math.round((100 * rates.tp) / total);
}

/** Recall as a whole percent; null when the ground truth has no Yes page. */
export function recallPercent(rates: ClassRates): number | null {
  const total = rates.tp + rates.fn;
  return total === 0 ? null : Math.round((100 * rates.tp) / total);
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
    if (row.fullOnly) total.fullOnly = true;
    row.matched?.forEach((count, k) => {
      if (!count) return;
      total.matched ??= [];
      total.matched[k] = (total.matched[k] ?? 0) + count;
    });
  }
  return total;
}

/** Overall: pages by how many of the seven streams match, [[k, pages]] for k = 7..0. */
export function matchCounts(tally: Tally): [number, number][] {
  const matched = tally.matched ?? [];
  return Array.from({ length: OVERALL_STREAMS + 1 }, (_, i) => {
    const k = OVERALL_STREAMS - i;
    return [k, matched[k] ?? 0] as [number, number];
  });
}

/** Good = 5..7 streams matching, Fair = 3..4, Bad = 0..2. */
export const MATCH_BANDS: { id: "good" | "fair" | "bad"; label: string; min: number; max: number }[] = [
  { id: "good", label: "Good", min: 5, max: 7 },
  { id: "fair", label: "Fair", min: 3, max: 4 },
  { id: "bad", label: "Bad", min: 0, max: 2 },
];

export function matchBand(k: number): "good" | "fair" | "bad" {
  return (MATCH_BANDS.find((band) => k >= band.min && k <= band.max) ?? MATCH_BANDS[2]).id;
}

/** Overall: pages per band (Good / Fair / Bad). */
export function bandCounts(tally: Tally): Record<"good" | "fair" | "bad", number> {
  const out = { good: 0, fair: 0, bad: 0 };
  (tally.matched ?? []).forEach((pages, k) => {
    if (pages) out[matchBand(k)] += pages;
  });
  return out;
}

export function formatDistribution(tally: Tally, binary = false): string {
  const na = tally.na > 0 ? ` · Not Available ${tally.na}` : "";
  if (tally.scored === 0) return na ? na.slice(3) : "—";
  if (tally.fullOnly && tally.matched) {
    const bands = bandCounts(tally);
    return MATCH_BANDS.map((band) => `${band.label} ${bands[band.id]}`).join(" · ") + na;
  }
  if (binary) return `Match ${tally.yes} · No Match ${tally.no}${na}`;
  return `Match ${tally.yes} · Partial ${tally.maybe} · No Match ${tally.no}${na}`;
}
