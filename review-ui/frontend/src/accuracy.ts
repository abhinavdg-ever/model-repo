import type {
  ImagingDocumentResponse,
  ImagingPageResult,
  ImagingSectionsProcessed,
} from "./api";
import { groundTruthBits, type GtBit } from "./groundTruth";
import { splitPageType } from "./ImagingPanel";

export type Verdict = "correct" | "wrong";

export type MetricId = "member" | "dos" | "blankJunk" | "pageType" | "codeable";

export const METRICS: { id: MetricId; label: string; rule: string }[] = [
  {
    id: "member",
    label: "Member Verification",
    rule: "Two or more of Name, DOB, and ID match",
  },
  {
    id: "dos",
    label: "DOS",
    rule: "From and To both match",
  },
  {
    id: "blankJunk",
    label: "Blank / Junk",
    rule: "Blank or junk label matches",
  },
  {
    id: "pageType",
    label: "Page Type",
    rule: "A match if the family or the subtype agrees",
  },
  {
    id: "codeable",
    label: "Codeable / Non Codeable",
    rule: "Codeable label matches",
  },
];

export type Tally = { correct: number; wrong: number; scored: number };

export type ChartAccuracy = {
  chartId: string;
  chartName: string;
  metrics: Record<MetricId, Tally>;
  overall: Tally;
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
  return { correct: 0, wrong: 0, scored: 0 };
}

function add(tally: Tally, verdict: Verdict | null) {
  if (verdict === "correct") {
    tally.correct += 1;
    tally.scored += 1;
  } else if (verdict === "wrong") {
    tally.wrong += 1;
    tally.scored += 1;
  }
}

export function accuracyPercent(tally: Tally): number | null {
  if (tally.scored === 0) return null;
  return Math.round((100 * tally.correct) / tally.scored);
}

function matched(bits: GtBit[]): boolean {
  return bits.some((bit) => bit.mark === "match");
}

function labeled(bits: GtBit[]): boolean {
  return bits.length > 0;
}

function idKey(value: string | null | undefined): string {
  return (value ?? "").toLowerCase().replace(/[^a-z0-9]/g, "");
}

function pageScores(
  page: ImagingPageResult,
  sections: ImagingSectionsProcessed,
  manifestMemberId: string | null | undefined,
): Record<MetricId, Verdict | null> {
  const parts = splitPageType(page.pageType);
  const bits = groundTruthBits({
    gt: page.groundTruth,
    memberName: page.memberName,
    memberDob: page.memberDob,
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

  const nameLabeled = labeled(bits.memberName);
  const dobLabeled = labeled(bits.memberDob);
  const extractedId = idKey(page.memberId);
  const expectedId = idKey(manifestMemberId);
  const idComparable = extractedId.length > 0 && expectedId.length > 0;
  const memberChecks = Number(nameLabeled) + Number(dobLabeled) + Number(idComparable);
  let member: Verdict | null = null;
  if (memberChecks >= 2) {
    let hits = 0;
    if (matched(bits.memberName)) hits += 1;
    if (matched(bits.memberDob)) hits += 1;
    if (idComparable && extractedId === expectedId) hits += 1;
    member = hits >= 2 ? "correct" : "wrong";
  }

  const dos =
    labeled(bits.dosFrom) && labeled(bits.dosTo)
      ? matched(bits.dosFrom) && matched(bits.dosTo)
        ? "correct"
        : "wrong"
      : null;

  const single = (field: GtBit[]): Verdict | null =>
    labeled(field) ? (matched(field) ? "correct" : "wrong") : null;

  return {
    member,
    dos,
    blankJunk: single(bits.blankJunk),
    pageType: single(bits.pageType),
    codeable: single(bits.codeable),
  };
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
  for (const page of doc.pages) {
    const scores = pageScores(page, sections, doc.manifest?.memberId);
    for (const metric of METRICS) add(metrics[metric.id], scores[metric.id]);
  }
  const overall = emptyTally();
  for (const metric of METRICS) {
    overall.correct += metrics[metric.id].correct;
    overall.wrong += metrics[metric.id].wrong;
    overall.scored += metrics[metric.id].scored;
  }
  return { chartId, chartName, metrics, overall };
}

export function sumTallies(rows: Tally[]): Tally {
  const total = emptyTally();
  for (const row of rows) {
    total.correct += row.correct;
    total.wrong += row.wrong;
    total.scored += row.scored;
  }
  return total;
}

export function formatRatio(tally: Tally): string {
  if (tally.scored === 0) return "—";
  return `${tally.correct}/${tally.scored}`;
}
