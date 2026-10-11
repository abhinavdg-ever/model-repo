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
  memberId?: string | null;
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
  renderingProvider?: string | null;
  providerSignature?: string | null;
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

/** Yes matches a value on the page. No matches an empty page. */
function yesFoundMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  const side = yesNo(gt);
  if (!side) return missing(gt) ? [] : bit(gt, "unknown");
  if (!known) return bit(gt, "unknown");
  const found = nameFound(pipeline);
  if (side === "yes" && found) return bit(gt, "match");
  if (side === "no" && !found) return bit(gt, "match");
  return bit(gt, "mismatch");
}

function dobMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (!yesNo(gt)) return [];
  return yesFoundMark(gt, pipeline, known);
}

function visibilityMark(value: string | null | undefined): GtBit[] {
  if (yesNo(value) === "yes") return bit("Good Visibility", "unknown");
  return [];
}

const MONTHS: Record<string, string> = {
  jan: "01", january: "01", feb: "02", february: "02", mar: "03", march: "03",
  apr: "04", april: "04", may: "05", jun: "06", june: "06", jul: "07", july: "07",
  aug: "08", august: "08", sep: "09", sept: "09", september: "09", oct: "10", october: "10",
  nov: "11", november: "11", dec: "12", december: "12",
};

function yearOf(raw: string): string {
  const year = Number(raw);
  if (raw.length === 4) return raw;
  const pivot = (new Date().getFullYear() % 100) + 1;
  return String(year <= pivot ? 2000 + year : 1900 + year);
}

function parseDate(value: string | null | undefined): [string, string, string] | null {
  const raw = text(value).replace(/(\d)(?:st|nd|rd|th)\b/gi, "$1").replace(/\bsept\b/gi, "Sep");
  if (!raw || missing(raw)) return null;
  const iso = /^(\d{4})-(\d{2})-(\d{2})/.exec(raw);
  if (iso) return [iso[1], iso[2], iso[3]];
  const us = /^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})/.exec(raw);
  if (us) return [yearOf(us[3]), us[1].padStart(2, "0"), us[2].padStart(2, "0")];
  const monthFirst = /^([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{2,4})/.exec(raw);
  if (monthFirst && MONTHS[monthFirst[1].toLowerCase()]) {
    return [yearOf(monthFirst[3]), MONTHS[monthFirst[1].toLowerCase()], monthFirst[2].padStart(2, "0")];
  }
  const dayFirst = /^(\d{1,2})(?:[./\s-]+)([A-Za-z]+)\.?,?\s+(\d{2,4})/.exec(raw);
  if (dayFirst && MONTHS[dayFirst[2].toLowerCase()]) {
    return [yearOf(dayFirst[3]), MONTHS[dayFirst[2].toLowerCase()], dayFirst[1].padStart(2, "0")];
  }
  return null;
}

const DATE_IN_TEXT =
  /\b(\d{4}-\d{2}-\d{2}|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\d{1,2}[./-][A-Za-z]+[./-]?\d{2,4}|[A-Za-z]+\.?\s+\d{1,2}(?:st|nd|rd|th)?,?\s+\d{2,4}|\d{1,2}(?:st|nd|rd|th)?\s+[A-Za-z]+\.?,?\s+\d{2,4})\b/gi;

/** Every date in the text, shown as YYYY-MM-DD. Other words stay. */
export function displayIsoDates(value: string | null | undefined): string {
  return text(value).replace(DATE_IN_TEXT, (raw) => {
    const parts = parseDate(raw);
    return parts ? parts.join("-") : raw;
  });
}

function dateMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const expected = parseDate(gt);
  const actual = parseDate(pipeline);
  // Shown as YYYY-MM-DD, the same format as the Value column.
  const shown = expected ? expected.join("-") : text(gt);
  if (!known || !expected || !actual) return bit(shown, "unknown");
  if (expected.join("-") === actual.join("-")) return bit(shown, "match");
  const shared = expected.filter((part, index) => part === actual[index]).length;
  return bit(shown, shared > 0 ? "partial" : "mismatch");
}

/**
 * Camel case has no space, only a capital at each word: "ProgressNotes"
 * shows as "Progress Notes".
 */
export function displayWords(value: string | null | undefined): string {
  return text(value)
    .replace(/[_-]+/g, " ")
    .replace(/([a-z0-9])([A-Z])/g, "$1 $2")
    .split(/\s+/)
    .filter(Boolean)
    .map((word) => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
    .join(" ");
}

// Codeable, Non Codeable, Non-Codeable, NonCodeable, non_codeable, Codable …
const CODEABLE = /\b(?:(non)[\s_-]*)?code?able\b/gi;

/** Codeable / Non-Codeable, however a sheet or an older run spelled it. */
export function spellCodeable(value: string): string {
  return value.replace(CODEABLE, (_match, non) => (non ? "Non-Codeable" : "Codeable"));
}

export function displayCodeable(value: string | null | undefined): string {
  return spellCodeable(displayWords(value));
}

function codeableMark(
  gt: string | null | undefined,
  pipeline: string | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  const shown = displayCodeable(gt);
  if (!known || missing(pipeline)) return bit(shown, "unknown");
  const leftNon = fold(shown).startsWith("non");
  const rightNon = fold(displayCodeable(pipeline)).startsWith("non");
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

/** One ground-truth cell can name several page types, separated by `;` or `|`. */
function pageTypeNames(value: string | null | undefined): string[] {
  if (missing(value)) return [];
  return displayWords(value)
    .split(/\s*[;|]\s*/)
    .map((part) => part.trim())
    .filter((part) => part.length > 0 && !missing(part));
}

/**
 * A match when any ground-truth name agrees with the family or the subtype.
 * Both sides are checked; one agreement is enough.
 */
function pageTypeMark(
  gt: string | null | undefined,
  pipeline: Array<string | null | undefined>,
  known: boolean,
): GtBit[] {
  const names = pageTypeNames(gt);
  if (names.length === 0) return [];
  const shown = names.join(" | ");
  if (!known) return bit(shown, "unknown");
  const actuals = pipeline
    .flatMap((value) => pageTypeNames(value))
    .map((value) => fold(value))
    .filter(
      (value) =>
        value && value !== "yet to process" && value !== "skipped" && value !== "not found",
    );
  if (actuals.length === 0) return bit(shown, "unknown");
  const expected = names.map((name) => fold(name));
  if (expected.some((name) => name === "accept")) {
    const junk = actuals.some((actual) => JUNK_PAGE.some((word) => actual.includes(word)));
    return bit(shown, junk ? "mismatch" : "match");
  }
  const same = expected.some((name) =>
    actuals.some(
      (actual) => name === actual || actual.includes(name) || name.includes(actual),
    ),
  );
  return bit(shown, same ? "match" : "mismatch");
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

const LABEL_FIELDS: Array<keyof PageGroundTruth> = [
  "memberName",
  "memberDob",
  "memberId",
  "dosFrom",
  "dosTo",
  "encounterType",
  "pageType",
  "codeable",
  "blankPage",
  "junkPage",
  "isInvoice",
  "pageSequence",
  "rotation",
  "isVisible",
  "renderingProvider",
  "providerSignature",
];

/** True when at least one ground-truth cell on the page holds a value. */
export function hasGroundTruth(gt: PageGroundTruth | null | undefined): boolean {
  if (!gt) return false;
  return LABEL_FIELDS.some((field) => !missing(gt[field] as string | null | undefined));
}

export type GroundTruthBits = {
  memberName: GtBit[];
  memberDob: GtBit[];
  memberId: GtBit[];
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
  memberId?: string | null;
  memberKnown: boolean;
  orientationAngle: number | null | undefined;
  rotationKnown: boolean;
  dosFrom: string | null | undefined;
  dosTo: string | null | undefined;
  dosKnown: boolean;
  blankOrJunk: string | null | undefined;
  junkKnown: boolean;
  pageType: string | null | undefined;
  /** The type inside the family, "Discharge Note" of "Discharge (Discharge Note)". */
  pageSubtype?: string | null;
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
    memberId: [],
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
    memberName: yesFoundMark(gt.memberName, input.memberName, input.memberKnown),
    memberDob: dobMark(gt.memberDob, input.memberDob, input.memberKnown),
    memberId: dobMark(gt.memberId, input.memberId, input.memberKnown),
    quality: visibilityMark(gt.isVisible),
    rotation: rotationMark(gt.rotation, input.orientationAngle, input.rotationKnown),
    dosFrom: dateMark(gt.dosFrom, input.dosFrom, input.dosKnown),
    dosTo: dateMark(gt.dosTo, input.dosTo, input.dosKnown),
    blankJunk: blankJunkMark(gt, input.blankOrJunk, input.junkKnown),
    pageType: pageTypeMark(
      gt.encounterType,
      [input.pageType, input.pageSubtype],
      input.pageTypeKnown,
    ),
    codeable: codeableMark(gt.codeable, input.isCodeable, input.codeableKnown),
    pageSequence: sequenceMark(gt.pageSequence, input.actualSequence, input.sequenceKnown),
  };
}
