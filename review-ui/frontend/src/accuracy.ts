import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import { groundTruthBits, hasGroundTruth, type GtBit } from "./groundTruth";
import { splitPageType } from "./ImagingPanel";

export type Verdict = "yes" | "maybe" | "no";

export type MetricId = "member" | "dos" | "blankJunk" | "pageType" | "codeable" | "signature";

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
    rule: "Encounter type matches the page type or the page subtype",
  },
  {
    id: "codeable",
    label: "Codeable / Non-Codeable",
    rule: "Codeable label matches",
  },
  {
    id: "signature",
    label: "Provider Signature",
    rule: "Yes or No signature label matches",
  },
];

export type Tally = { yes: number; no: number; maybe: number; scored: number };

/** Blank or junk is the positive class. A subtype disagreement still counts as found. */
export type ClassRates = { tp: number; fp: number; fn: number };

export type ChartAccuracy = {
  chartId: string;
  chartName: string;
  lastUpdatedAt?: string | null;
  lastVerifiedAt?: string | null;
  metrics: Record<MetricId, Tally>;
  overall: Tally;
  blankJunkRates: ClassRates;
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
  return { tp: 0, fp: 0, fn: 0 };
}

function blankOrJunkPositive(label: string): boolean {
  return label.trim().toLowerCase().startsWith("yes");
}

function addBlankJunk(rates: ClassRates, bits: GtBit[]): void {
  if (!labeled(bits)) return;
  const bit = bits[0];
  const expected = blankOrJunkPositive(bit.label);
  if (bit.mark === "match" || bit.mark === "partial") {
    if (expected) rates.tp += 1;
    return;
  }
  if (expected) rates.fn += 1;
  else if (bit.mark === "mismatch") rates.fp += 1;
}

function add(tally: Tally, verdict: Verdict | null) {
  if (!verdict) return;
  tally[verdict] += 1;
  tally.scored += 1;
}

/** Yes is a full match, May be is half, No is none. */
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
): { scores: Record<MetricId, Verdict | null>; blankJunk: GtBit[] } {
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

  const expectedSignature = yesNo(page.groundTruth?.providerSignature);
  const actualSignature = yesNo(page.providerSignature);
  const signature: Verdict | null =
    expectedSignature == null || actualSignature == null
      ? null
      : expectedSignature === actualSignature
        ? "yes"
        : "no";

  return {
    scores: {
      member,
      dos,
      blankJunk: single(bits.blankJunk),
      pageType: single(bits.pageType),
      codeable: single(bits.codeable),
      signature,
    },
    blankJunk: bits.blankJunk,
  };
}

/** Six full matches are a page match. Three to five are partial. Two or fewer miss. */
function overallVerdict(scores: Record<MetricId, Verdict | null>): Verdict | null {
  if (METRICS.every((metric) => scores[metric.id] == null)) return null;
  const matches = METRICS.filter((metric) => scores[metric.id] === "yes").length;
  if (matches === METRICS.length) return "yes";
  if (matches >= 3) return "maybe";
  return "no";
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
  const overall = emptyTally();
  for (const page of doc.pages) {
    if (!hasGroundTruth(page.groundTruth)) continue;
    const scored = pageScores(page, sections, doc.manifest?.memberId);
    for (const metric of METRICS) add(metrics[metric.id], scored.scores[metric.id]);
    add(overall, overallVerdict(scored.scores));
    addBlankJunk(blankJunkRates, scored.blankJunk);
  }
  return { chartId, chartName, metrics, overall, blankJunkRates };
}

export function sumRates(rows: ClassRates[]): ClassRates {
  const total = emptyRates();
  for (const row of rows) {
    total.tp += row.tp;
    total.fp += row.fp;
    total.fn += row.fn;
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
  return `Yes ${tally.yes} · No ${tally.no} · May be ${tally.maybe}`;
}
