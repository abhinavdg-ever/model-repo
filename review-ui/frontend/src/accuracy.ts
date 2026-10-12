import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import { blankJunkTruth, groundTruthBits, hasGroundTruth, type GtBit } from "./groundTruth";
import { splitPageType } from "./ImagingPanel";

export type Verdict = "yes" | "maybe" | "no";

export type MetricId = "member" | "dos" | "blankJunk" | "pageType" | "codeable" | "credentials";

export const METRICS: { id: MetricId; label: string; rule: string }[] = [
  {
    id: "member",
    label: "Member Verification",
    rule: "Two or more of the labelled Name, DOB, and ID match (the only one, when one is labelled)",
  },
  {
    id: "dos",
    label: "DOS",
    rule: "Each labelled date (From, To) matches",
  },
  {
    id: "blankJunk",
    label: "Blank / Junk",
    rule: "Blank or junk label matches",
  },
  {
    id: "pageType",
    label: "Page Type",
    rule: "Ground-truth page type matches the page type or subtype, or is an accepted label for the model type; a broader label is May be; else semantic similarity > 0.8. Not scored on blank or junk pages",
  },
  {
    id: "codeable",
    label: "Codeable / Non-Codeable",
    rule: "Codeable label matches",
  },
  {
    id: "credentials",
    label: "Provider Credentials",
    rule: "Credentials match (MD, DO, NP…; punctuation and order ignored); some shared is May be",
  },
];

export type Tally = { yes: number; no: number; maybe: number; scored: number };

/** Blank or junk is the positive class. A subtype disagreement still counts as found. */
/** Counts for one "Yes" class: true / false positives and negatives. */
export type ClassRates = { tp: number; fp: number; fn: number; tn: number };

/** Charts whose last run is before this date are left out of the report. */
export const MIN_LAST_RUN = "2026-10-11";

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
  /** "Yes" class = the member's name / DOB / ID is on the page. */
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

function blankOrJunkPositive(label: string): boolean {
  return label.trim().toLowerCase().startsWith("yes");
}

/** A page the ground truth calls blank or junk: Match, Partial (other sub-type)
 * or No Match. Pages the pipeline has not judged yet are left out. */
function addBlankJunkYes(tally: Tally, bits: GtBit[]): void {
  if (!labeled(bits) || !blankOrJunkPositive(bits[0].label) || bits[0].mark === "unknown") return;
  add(tally, grade(bits));
}

function addBlankJunk(rates: ClassRates, bits: GtBit[]): void {
  if (!labeled(bits)) return;
  const bit = bits[0];
  const expected = blankOrJunkPositive(bit.label);
  if (bit.mark === "match" || bit.mark === "partial") {
    if (expected) rates.tp += 1;
    else rates.tn += 1;
    return;
  }
  if (expected) rates.fn += 1;
  else if (bit.mark === "mismatch") rates.fp += 1;
}

/** Member presence as a Yes / No class. A page is positive in the ground truth
 * when any labelled member field (name, DOB, ID) is Yes, and predicted positive
 * when the pipeline found any of those labelled fields. Unknown fields (the
 * member stage did not run) are left out. */
function addMember(rates: ClassRates, fields: Array<[string | null | undefined, GtBit[]]>): void {
  const scored = fields.filter(
    ([label, bits]) => yesNo(label) != null && bits.length > 0 && bits[0].mark !== "unknown",
  );
  if (scored.length === 0) return;
  const expected = scored.some(([label]) => yesNo(label) === "yes");
  const predicted = scored.some(
    ([label, bits]) => (yesNo(label) === "yes") === (bits[0].mark === "match"),
  );
  if (expected && predicted) rates.tp += 1;
  else if (expected) rates.fn += 1;
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
  /** The metrics a page is held to: all six, or five on a blank / junk page. */
  applicable: typeof METRICS;
  member: Array<[string | null | undefined, GtBit[]]>;
} {
  const parts = splitPageType(page.pageType);
  const bits = groundTruthBits({
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

  const dosDates = [bits.dosFrom, bits.dosTo].filter(labeled);
  const dosGrades = dosDates.map(grade);
  const dos: Verdict | null =
    dosGrades.length === 0
      ? null
      : dosGrades.every((item) => item === "yes")
        ? "yes"
        : dosGrades.some((item) => item === "no")
          ? "no"
          : "maybe";

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
      pageType: blankOrJunk ? null : single(bits.pageType),
      codeable: single(bits.codeable),
      credentials,
    },
    blankJunk: bits.blankJunk,
    applicable: blankOrJunk ? METRICS.filter((metric) => metric.id !== "pageType") : METRICS,
    member: [
      [page.groundTruth?.memberName, bits.memberName],
      [page.groundTruth?.memberDob, bits.memberDob],
      [page.groundTruth?.memberId, bits.memberId],
    ] as Array<[string | null | undefined, GtBit[]]>,
  };
}

/** Every applicable metric matching is a page match (six, or five on a blank /
 * junk page, where page type is not scored). Three or more are partial. Two or
 * fewer miss. */
function overallVerdict(
  scores: Record<MetricId, Verdict | null>,
  applicable: typeof METRICS = METRICS,
): Verdict | null {
  if (applicable.every((metric) => scores[metric.id] == null)) return null;
  const matches = applicable.filter((metric) => scores[metric.id] === "yes").length;
  if (matches === applicable.length) return "yes";
  if (matches >= 3) return "maybe";
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
  const memberRates = emptyRates();
  const overall = emptyTally();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections, doc.manifest?.memberId);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    add(overall, overallVerdict(scored.scores, scored.applicable));
    addBlankJunk(blankJunkRates, scored.blankJunk);
    addBlankJunkYes(blankJunkYes, scored.blankJunk);
    addMember(memberRates, scored.member);
  }
  return { chartId, chartName, metrics, overall, blankJunkRates, blankJunkYes, memberRates };
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
