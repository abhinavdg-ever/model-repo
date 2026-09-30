/** Compare a pipeline value with the client ground-truth cell.

    tick = the values agree
    !    = a partial match (shared name tokens, or some date parts)
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

function nameTokens(value: string | null | undefined): string[] {
  return fold(value)
    .replace(/[^a-z0-9\s]/g, " ")
    .split(/\s+/)
    .filter((token) => token.length > 0);
}

function significantTokens(tokens: string[]): string[] {
  const long = tokens.filter((token) => token.length > 1);
  return (long.length > 0 ? long : tokens).slice().sort();
}

function nameMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  if (!known || missing(pipeline)) return bit(gt, "unknown");
  const left = significantTokens(nameTokens(gt));
  const right = significantTokens(nameTokens(pipeline));
  if (left.length === 0 || right.length === 0) return bit(gt, "unknown");
  if (left.join(" ") === right.join(" ")) return bit(gt, "match");
  const rightSet = new Set(right);
  const shared = left.filter((token) => rightSet.has(token));
  return bit(gt, shared.length > 0 ? "partial" : "mismatch");
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

function textMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  if (!known || missing(pipeline)) return bit(gt, "unknown");
  const left = fold(gt);
  const right = fold(pipeline);
  if (left === right) return bit(gt, "match");
  if (left.includes(right) || right.includes(left)) return bit(gt, "partial");
  return bit(gt, "mismatch");
}

function codeableMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  if (!known || missing(pipeline)) return bit(gt, "unknown");
  const left = fold(gt).replace("codable", "codeable");
  const right = fold(pipeline).replace("codable", "codeable");
  const leftNon = left.startsWith("non");
  const rightNon = right.startsWith("non");
  return bit(gt, leftNon === rightNon ? "match" : "mismatch");
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

function flagMark(
  gt: string | null | undefined,
  label: string,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  const side = yesNo(gt);
  if (!side && missing(gt)) return [];
  const shown = `${label} ${text(gt)}`;
  if (!side || !known || missing(pipeline)) return [{ label: shown, mark: "unknown" }];
  const hit = fold(pipeline).includes(label);
  const agree = side === "yes" ? hit : !hit;
  return [{ label: shown, mark: agree ? "match" : "mismatch" }];
}

function invoiceMark(
  gt: string | null | undefined,
  pageType: string | null | undefined,
  known: boolean,
): GtBit[] {
  const side = yesNo(gt);
  if (!side) return [];
  if (!known) return bit(`invoice ${text(gt)}`, "unknown");
  const actual = fold(pageType);
  if (
    !actual ||
    actual === "yet to process" ||
    actual === "skipped" ||
    actual === "not found"
  ) {
    return bit(`invoice ${text(gt)}`, "unknown");
  }
  const invoice = actual.includes("invoice");
  const agree = side === "yes" ? invoice : !invoice;
  return bit(`invoice ${text(gt)}`, agree ? "match" : "mismatch");
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
  rotation: GtBit[];
  encounter: GtBit[];
  dosFrom: GtBit[];
  dosTo: GtBit[];
  blankJunk: GtBit[];
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
  encounterType: string | null | undefined;
  encounterKnown: boolean;
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
    rotation: [],
    encounter: [],
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
    rotation: rotationMark(gt.rotation, input.orientationAngle, input.rotationKnown),
    encounter: textMark(gt.encounterType, input.encounterType, input.encounterKnown),
    dosFrom: dateMark(gt.dosFrom, input.dosFrom, input.dosKnown),
    dosTo: dateMark(gt.dosTo, input.dosTo, input.dosKnown),
    blankJunk: [
      ...flagMark(gt.blankPage, "blank", input.blankOrJunk, input.junkKnown),
      ...flagMark(gt.junkPage, "junk", input.blankOrJunk, input.junkKnown),
      ...invoiceMark(gt.isInvoice, input.pageType, input.pageTypeKnown),
    ],
    pageType: pageTypeMark(gt.pageType, input.pageType, input.pageTypeKnown),
    codeable: codeableMark(gt.codeable, input.isCodeable, input.codeableKnown),
    pageSequence: sequenceMark(gt.pageSequence, input.actualSequence, input.sequenceKnown),
  };
}
