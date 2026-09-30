/** Compare a pipeline value with the client ground-truth cell.

    tick = the values agree
    !    = a partial date match (shared year, month, or day)
    X    = both sides have a value and nothing lines up
*/

export type MatchMark = "match" | "partial" | "mismatch" | "unknown";

export type PageGroundTruth = {
  pageNumber: number;
  sourcePageId?: string | null;
  memberName?: string | null;
  memberDob?: string | null;
  dosFrom?: string | null;
  dosTo?: string | null;
  encounterType?: string | null;
  pageType?: string | null;
  codeable?: string | null;
  blankPage?: string | null;
  junkPage?: string | null;
  isInvoice?: string | null;
  pageSequence?: string | null;
  rotation?: string | null;
  isVisible?: string | null;
};

export type GtBit = { label: string; mark: MatchMark };

const PENDING = new Set([
  "",
  "na",
  "n/a",
  "not applicable",
  "not available",
  "not found",
  "not sure",
  "yet to process",
  "skipped",
]);

function text(value: string | null | undefined): string {
  return (value ?? "").trim();
}

function fold(value: string | null | undefined): string {
  return text(value).toLowerCase().replace(/\s+/g, " ");
}

function missing(value: string | null | undefined): boolean {
  return PENDING.has(fold(value));
}

function yesNo(value: string | null | undefined): "yes" | "no" | null {
  const folded = fold(value);
  if (folded === "yes" || folded === "y" || folded === "true") return "yes";
  if (folded === "no" || folded === "n" || folded === "false") return "no";
  return null;
}

function bit(label: string | null | undefined, mark: MatchMark): GtBit[] {
  const shown = text(label);
  if (!shown) return [];
  return [{ label: shown, mark }];
}

function nameFound(value: string | null | undefined): boolean {
  const raw = text(value);
  return raw.length > 0 && !missing(raw);
}

function nameMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  const side = yesNo(gt);
  if (!side) return missing(gt) ? [] : bit(gt, "unknown");
  if (!known) return bit(gt, "unknown");
  const found = nameFound(pipeline);
  const agree = side === "yes" ? found : !found;
  return bit(gt, agree ? "match" : "mismatch");
}

function parseDate(value: string | null | undefined): [string, string, string] | null {
  const raw = text(value);
  if (!raw || missing(raw)) return null;
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (iso) return [iso[1], iso[2], iso[3]];
  const us = /^(\d{1,2})\/(\d{1,2})\/(\d{4})/.exec(raw);
  if (us) return [us[3], us[1].padStart(2, "0"), us[2].padStart(2, "0")];
  return null;
}

function dateMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const expected = parseDate(gt);
  const actual = parseDate(pipeline);
  if (!known || !expected || !actual) return bit(gt, "unknown");
  if (expected.join("-") === actual.join("-")) return bit(gt, "match");
  const shared = expected.filter((part, index) => part === actual[index]).length;
  return bit(gt, shared > 0 ? "partial" : "mismatch");
}

/** The client sheet spells it "Codable"; show "Codeable" like the pipeline. */
function codeableSpelling(value: string | null | undefined): string {
  return text(value).replace(/codable/gi, (m) => (m[0] === "C" ? "Codeable" : "codeable"));
}

function codeableMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const shown = codeableSpelling(gt);
  if (!known || missing(pipeline)) return bit(shown, "unknown");
  const leftNon = fold(shown).startsWith("non");
  const rightNon = fold(codeableSpelling(pipeline)).startsWith("non");
  return bit(shown, leftNon === rightNon ? "match" : "mismatch");
}

/** Blank Page / Junk Page / Is Invoice collapsed into the pipeline's own format. */
function blankJunkTruth(gt: PageGroundTruth): string | null {
  const blank = yesNo(gt.blankPage);
  const junk = yesNo(gt.junkPage);
  const invoice = yesNo(gt.isInvoice);
  if (blank === "yes") return "Yes (Blank)";
  if (junk === "yes" || invoice === "yes") return "Yes (Junk)";
  if (blank === "no" || junk === "no" || invoice === "no") return "No";
  return null;
}

function blankJunkMark(
  gt: PageGroundTruth,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  const expected = blankJunkTruth(gt);
  if (!expected) return [];
  if (!known || missing(pipeline)) return bit(expected, "unknown");
  const left = fold(expected);
  const right = fold(pipeline);
  if (left === right) return bit(expected, "match");
  if (left.startsWith("yes") && right.startsWith("yes")) return bit(expected, "partial");
  return bit(expected, "mismatch");
}

const JUNK_PAGE = [
  "invoice",
  "cover",
  "junk",
  "blank",
  "letter",
  "fax",
  "instruction",
  "record request",
];

function pageTypeMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  if (!known) return bit(gt, "unknown");
  const actual = fold(pipeline);
  if (
    !actual ||
    actual === "yet to process" ||
    actual === "skipped" ||
    actual === "not found"
  ) {
    return bit(gt, "unknown");
  }
  const expected = fold(gt);
  if (expected === "accept") {
    const junk = JUNK_PAGE.some((word) => actual.includes(word));
    return bit(gt, junk ? "mismatch" : "match");
  }
  const same =
    expected === actual || actual.includes(expected) || expected.includes(actual);
  return bit(gt, same ? "match" : "mismatch");
}

function rotationMark(
  gt: string | null | undefined,
  angle: number | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const expected = Number(text(gt));
  if (!Number.isFinite(expected)) return bit(gt, "unknown");
  if (!known || angle === null || angle === undefined || Number.isNaN(angle)) {
    return bit(gt, "unknown");
  }
  return bit(gt, Math.abs(expected - angle) < 0.5 ? "match" : "mismatch");
}

function sequenceMark(
  gt: string | null | undefined,
  actual: number | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const expected = Number(text(gt));
  if (!Number.isFinite(expected)) return bit(gt, "unknown");
  if (!known || actual === null || actual === undefined) return bit(gt, "unknown");
  return bit(gt, expected === actual ? "match" : "mismatch");
}

export type GroundTruthBits = {
  memberName: GtBit[];
  memberDob: GtBit[];
  /** Client "Is Visible" — shown beside Quality, never scored (quality is a placeholder). */
  quality: GtBit[];
  rotation: GtBit[];
  dosFrom: GtBit[];
  dosTo: GtBit[];
  blankJunk: GtBit[];
  /** Compared with the client's Encounter Type column, which holds page types. */
  pageType: GtBit[];
  codeable: GtBit[];
  pageSequence: GtBit[];
};

export function groundTruthBits(input: {
  gt: PageGroundTruth | null | undefined;
  memberName: string | null | undefined;
  memberDob: string | null | undefined;
  memberKnown: boolean;
  orientationAngle: number | null | undefined;
  rotationKnown: boolean;
  dosFrom: string | null | undefined;
  dosTo: string | null | undefined;
  dosKnown: boolean;
  blankOrJunk: string | null | undefined;
  junkKnown: boolean;
  pageType: string | null | undefined;
  pageTypeKnown: boolean;
  isCodeable: string | null | undefined;
  codeableKnown: boolean;
  actualSequence: number | null | undefined;
  sequenceKnown: boolean;
}): GroundTruthBits {
  const gt = input.gt;
  const empty: GroundTruthBits = {
    memberName: [],
    memberDob: [],
    quality: [],
    rotation: [],
    dosFrom: [],
    dosTo: [],
    blankJunk: [],
    pageType: [],
    codeable: [],
    pageSequence: [],
  };
  if (!gt) return empty;
  return {
    memberName: nameMark(gt.memberName, input.memberName, input.memberKnown),
    memberDob: dateMark(gt.memberDob, input.memberDob, input.memberKnown),
    quality: missing(gt.isVisible) ? [] : bit(gt.isVisible, "unknown"),
    rotation: rotationMark(gt.rotation, input.orientationAngle, input.rotationKnown),
    dosFrom: dateMark(gt.dosFrom, input.dosFrom, input.dosKnown),
    dosTo: dateMark(gt.dosTo, input.dosTo, input.dosKnown),
    blankJunk: blankJunkMark(gt, input.blankOrJunk, input.junkKnown),
    pageType: pageTypeMark(gt.encounterType, input.pageType, input.pageTypeKnown),
    codeable: codeableMark(gt.codeable, input.isCodeable, input.codeableKnown),
    pageSequence: sequenceMark(gt.pageSequence, input.actualSequence, input.sequenceKnown),
  };
}
