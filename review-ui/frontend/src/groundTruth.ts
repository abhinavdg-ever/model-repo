/** Compare a pipeline value with the client ground-truth cell.

    tick = the values agree
    !    = a partial date match (shared year, month, or day)
    X    = both sides have a value and nothing lines up
*/

import gtPageTypeCanon from "./gtPageTypeCanon.json";
import pageTaxonomy from "./pageTaxonomy.json";
import pageTypeAccept from "./pageTypeAccept.json";

export type MatchMark = "match" | "partial" | "mismatch" | "unknown";

/** A semantic similarity above this between the ground-truth page type and the
 * predicted page type / sub-type counts as a match (accuracy report). */
export const PAGE_TYPE_SEMANTIC_MIN = 0.8;

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
  /** Yes / No. Older rows hold credentials here (see gtCredentials). */
  providerSignature?: string | null;
  providerCredentials?: string | null;
  /** Free text (2026-10-27, user): page_subtype and provider_specialty. */
  pageSubtype?: string | null;
  specialty?: string | null;
  /** Encounter type (2026-10-27, user): page_ground_truth.encounter_label, since
   * encounter_type holds the page-type label of the client sheet. */
  encounterLabel?: string | null;
  tilt?: string | null;
  /** High / Medium / Low. */
  quality?: string | null;
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

/** Codable / Non-Codable, however a sheet or an older run spelled it. */
export function spellCodeable(value: string): string {
  return value.replace(CODEABLE, (_match, non) => (non ? "Non-Codable" : "Codable"));
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
export function blankJunkTruth(gt: PageGroundTruth): string | null {
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
  // Blank against Junk (or one junk subtype against another) is still a match: both say
  // Yes, and the page is out of the record either way.
  if (left === right || (left.startsWith("yes") && right.startsWith("yes"))) return bit(expected, "match");
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

function acceptKey(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]+/g, "");
}

type AcceptCanon = {
  accept: Record<string, string[]>;
  broader: Record<string, string[]>;
  close?: string[][];
  close_labels?: Record<string, string[]>;
};

function labelMap(table: Record<string, string[]>): Map<string, Set<string>> {
  return new Map(
    Object.entries(table).map(([modelType, labels]) => [
      acceptKey(modelType),
      new Set(labels.map(acceptKey)),
    ]),
  );
}

/** model type -> accepted / broader ground-truth labels (``pageTypeAccept.json``), folded. */
const ACCEPTED = labelMap((pageTypeAccept as AcceptCanon).accept);
const BROADER = labelMap((pageTypeAccept as AcceptCanon).broader);
const CLOSE_LABELS = labelMap((pageTypeAccept as AcceptCanon).close_labels ?? {});
/** model type -> the other model types in its ``close`` group (folded). */
const CLOSE: Map<string, string[]> = new Map(
  ((pageTypeAccept as AcceptCanon).close ?? []).flatMap((group) =>
    group.map((member) => [
      acceptKey(member),
      group.filter((other) => other !== member).map((other) => other.toLowerCase()),
    ]),
  ),
);

/** The model type of a prediction: the sub-type inside Progress Note, else the page type. */
function modelTypeOf(pageType: string | null | undefined, pageSubtype: string | null | undefined): string {
  const family = text(pageType);
  const sub = text(pageSubtype);
  return family.toLowerCase() === "progress note" && sub ? sub : family;
}

type GtCanonEntry = { label: string; status: "mapped" | "ambiguous" | "exclude"; full: string[]; partial: string[] };

/** The client's GT page-type labels mapped to our v29.2 paths (``gtPageTypeCanon.json``,
 * built by training/taxonomy/build_gt_mapping.py), keyed by ``acceptKey``. */
const GT_CANON = (gtPageTypeCanon as { labels: Record<string, GtCanonEntry> }).labels;

/** Every client GT page-type label the canon scores, A to Z: the Page Type ground-truth
 * options (2026-10-26, user). */
export const GT_PAGE_TYPE_LABELS: readonly string[] = Object.values(GT_CANON)
  .map((entry) => entry.label)
  .sort((a, b) => a.localeCompare(b));

/** Our L1s, the Page Type ground-truth options, and the L2s under each, which Page
 * Subtype offers once an L1 is picked (pageTaxonomy.json; 2026-10-26, user). */
export const PAGE_L1_CHOICES: readonly string[] = pageTaxonomy.L1;
const PAGE_L2 = pageTaxonomy.L2 as Record<string, string[]>;
const OUR_L1 = new Set(PAGE_L1_CHOICES.map((name) => acceptKey(name)).filter((name) => name !== "others"));

const ALL_L2: readonly string[] = [...new Set(Object.values(PAGE_L2).flat())].sort((a, b) => a.localeCompare(b));

/** The Page Subtype options: the L2s of the L1 named ``l1`` (any spelling), or every L2
 * while no L1 of ours is picked. */
export function pageL2Choices(l1: string | null | undefined): readonly string[] {
  const key = acceptKey(text(l1));
  const name = PAGE_L1_CHOICES.find((candidate) => acceptKey(candidate) === key);
  return name ? PAGE_L2[name] ?? [] : ALL_L2;
}

/** Every L2, for checking a typed Page Subtype. */
export const PAGE_L2_ALL: readonly string[] = ALL_L2;

/** The Specialty ground-truth options. */
export const SPECIALTY_CHOICES: readonly string[] = pageTaxonomy.specialty;

/** Our path, L1 first, from a composed page type split by ``splitPageType``:
 * ("Lab Report", "Diagnostic Study) (Echocardiogram Report") -> the three levels,
 * folded with ``acceptKey``; "Others" is dropped. */
function ourPath(pageType: string | null | undefined, pageSubtype: string | null | undefined): string[] {
  const family = text(pageType);
  const sub = text(pageSubtype);
  const levels = [family, ...(sub && sub !== family ? sub.split(/\)\s+\(/) : [])];
  return levels.map((level) => acceptKey(level)).filter((level) => level && level !== "others");
}

function startsWith(path: string[], target: string): boolean {
  const levels = target.split(">").map((level) => acceptKey(level));
  return levels.length <= path.length && levels.every((level, index) => level === path[index]);
}

/** One GT label against our path with the canon: full when a full target is a prefix of
 * our path; partial when a partial target is, or our L1 is a full target's L1 (we stop
 * above the GT level, a sibling L3, another L2); else a miss. null: not scored. */
function canonMark(entry: GtCanonEntry, path: string[]): MatchMark | null {
  if (entry.status === "exclude") return null;
  if (path.length === 0) return "unknown";
  if (entry.full.some((target) => startsWith(path, target))) return "match";
  const sameL1 = entry.full.some((target) => acceptKey(target.split(">")[0]) === path[0]);
  if (sameL1 || entry.partial.some((target) => startsWith(path, target))) return "partial";
  return "mismatch";
}

const MARK_RANK: Record<MatchMark, number> = { match: 3, partial: 2, mismatch: 1, unknown: 0 };

/**
 * GT labels in the canon (gtPageTypeCanon.json) are scored with it (``canonMark``);
 * excluded ones (cover / index / fax / blank, NA ...) are not scored. A label not in
 * the canon falls back to the checks below.
 *
 * Checked against the predicted model type (the sub-type inside Progress Note,
 * else the page type), in order, the first that applies:
 *   match    a ground-truth name agrees with the model type (same name, or one
 *            contains the other) or is in its ``accept`` list (pageTypeAccept.json)
 *   partial  it is in the model type's ``broader`` list, agrees only with the
 *            page type (a generic "Progress Note" for Progress Note / Summary),
 *            or names another model type of the same ``close`` group (Cardiac
 *            Report / Echocardiogram Report / EKG / ECG Tracing): yellow,
 *            "May be" in the accuracy report
 *   match    semantic similarity > PAGE_TYPE_SEMANTIC_MIN (backend score; fallback)
 *   mismatch otherwise
 */
function pageTypeMark(
  gt: string | null | undefined,
  pipeline: Array<string | null | undefined>,
  known: boolean,
  semantic?: number | null,
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
  // One of our L1s as the label: a match when our L1 is it, else as below (the canon, where
  // an L1 name may also be a client label), the better of the two.
  const ownPath = ourPath(pipeline[0], pipeline[1]);
  if (ownPath.length > 0 && names.some((name) => OUR_L1.has(acceptKey(name)) && acceptKey(name) === ownPath[0])) {
    return bit(shown, "match");
  }
  const mapped = names.filter((name) => GT_CANON[acceptKey(name)]);
  if (mapped.length === names.length) {
    const marks = mapped
      .map((name) => canonMark(GT_CANON[acceptKey(name)], ownPath))
      .filter((mark): mark is MatchMark => mark !== null);
    if (marks.length === 0) return [];
    return bit(shown, marks.reduce((best, mark) => (MARK_RANK[mark] > MARK_RANK[best] ? mark : best)));
  }
  const expected = names.map((name) => fold(name));
  if (expected.some((name) => name === "accept")) {
    const junk = actuals.some((actual) => JUNK_PAGE.some((word) => actual.includes(word)));
    return bit(shown, junk ? "mismatch" : "match");
  }
  const agrees = (actual: string) =>
    expected.some((name) => name === actual || actual.includes(name) || name.includes(actual));
  const modelType = fold(modelTypeOf(pipeline[0], pipeline[1]));
  const listed = (table: Map<string, Set<string>>) => {
    const labels = table.get(acceptKey(modelType)) ?? new Set<string>();
    return names.some((name) => labels.has(acceptKey(name)));
  };
  if ((modelType && agrees(modelType)) || listed(ACCEPTED)) return bit(shown, "match");
  const close = (CLOSE.get(acceptKey(modelType)) ?? []).some((other) => {
    const labels = new Set([
      ...(ACCEPTED.get(acceptKey(other)) ?? []),
      ...(CLOSE_LABELS.get(acceptKey(other)) ?? []),
    ]);
    return agrees(other) || names.some((name) => labels.has(acceptKey(name)));
  });
  if (listed(BROADER) || actuals.some(agrees) || close) return bit(shown, "partial");
  if (semantic != null && semantic > PAGE_TYPE_SEMANTIC_MIN) return bit(shown, "match");
  return bit(shown, "mismatch");
}

/** One ground-truth page-type label against a predicted page type / sub-type:
 * the same rules as the accuracy report (``pageTypeMark``). */
export function pageTypeVerdict(
  gt: string | null | undefined,
  pageType: string | null | undefined,
  pageSubtype: string | null | undefined,
  semantic?: number | null,
): MatchMark | null {
  return pageTypeMark(gt, [pageType, pageSubtype], true, semantic)[0]?.mark ?? null;
}

function rotationMark(
  gt: string | null | undefined,
  angle: number | null | undefined,
  known: boolean,
): GtBit[] {
  if (missing(gt)) return [];
  // "270", "270°" and "-90" are the same orientation.
  const expected = Number(text(gt).replace(/°$/, ""));
  // An orientation is a quarter turn; "12°" is a typo or a tilt, so it is not scored.
  if (!Number.isFinite(expected) || Math.round(expected) % 90 !== 0) return bit(gt, "unknown");
  if (!known || angle === null || angle === undefined || Number.isNaN(angle)) {
    return bit(gt, "unknown");
  }
  const turn = (value: number) => ((Math.round(value) % 360) + 360) % 360;
  return bit(gt, turn(expected) === turn(angle) ? "match" : "mismatch");
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
  "providerCredentials",
];

/** Provider Credentials ground truth. Rows saved before it had its own column
 * keep the credentials in providerSignature. */
export function gtCredentials(gt: PageGroundTruth | null | undefined): string {
  const own = (gt?.providerCredentials ?? "").trim();
  if (own) return own;
  const legacy = (gt?.providerSignature ?? "").trim();
  return legacy && !missing(legacy) && yesNo(legacy) === null ? legacy : "";
}

/** Provider Signature ground truth as Yes / No ("" when unlabelled). Credentials
 * in an older row mean the page is signed; NA / None there means it is not. */
export function gtSignature(gt: PageGroundTruth | null | undefined): "Yes" | "No" | "" {
  const side = yesNo(gt?.providerSignature);
  if (side) return side === "yes" ? "Yes" : "No";
  if (gtCredentials(gt)) return "Yes";
  const folded = (gt?.providerSignature ?? "").trim().toLowerCase();
  return ["na", "n/a", "not found", "none"].includes(folded) ? "No" : "";
}

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
  /** Backend semantic similarity of the ground-truth page type (accuracy report). */
  pageTypeSemantic?: number | null;
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
    // The ground-truth page type lives in the Encounter Type column; the Page
    // Type cell is the fallback when Encounter Type is empty.
    pageType: pageTypeMark(
      missing(gt.encounterType) ? gt.pageType : gt.encounterType,
      [input.pageType, input.pageSubtype],
      input.pageTypeKnown,
      input.pageTypeSemantic,
    ),
    codeable: codeableMark(gt.codeable, input.isCodeable, input.codeableKnown),
    pageSequence: sequenceMark(gt.pageSequence, input.actualSequence, input.sequenceKnown),
  };
}
