import { useContext, useEffect, useLayoutEffect, useMemo, useState, type KeyboardEvent, type MutableRefObject, type ReactNode } from "react";
import { CalendarDays, Check, ChevronLeft, ChevronRight, CircleMinus, Pencil, Save, X } from "lucide-react";
import {
  savePageGroundTruth,
  type ExtractionReviewResponse,
  type ImagingDocumentResponse,
  type ImagingManifestDetails,
  type ImagingPageResult,
  type ImagingSectionsProcessed,
  type ImagingVerificationDetails,
  type OcrSectionHeader,
  type PageGroundTruth,
  type StagedEncounter,
  savePageSequences,
} from "./api";
import { credentialsVerdict, noValue } from "./accuracy";
import {
  duplicateDisplayConfidence,
  formatDuplicateLabel,
  isCompleteDuplicate,
} from "./duplicateLabel";
import {
  displayCodeable,
  displayIsoDates,
  groundTruthBits,
  gtCredentials,
  blankJunkTruth,
  gtSignature,
  hasGroundTruth,
  pageTypeVerdict,
  spellCodeable,
  type GtBit,
  GT_PAGE_TYPE_LABELS,
  PAGE_L1_CHOICES,
  PAGE_L2_ALL,
  pageL2Choices,
  SPECIALTY_CHOICES,
} from "./groundTruth";
import { PAGE_SUBTYPE_CHOICES, PAGE_TYPE_CHOICES } from "./reviewChoices";
import {
  ENCOUNTER_TYPE_CHOICES,
  EncounterReviewEditor,
  type EncounterSeed,
} from "./ReviewTables";
import { properName } from "./properName";
import { ClientViewContext } from "./clientView";

const DEFAULT_SECTIONS: ImagingSectionsProcessed = {
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

/** The DOS of a page with no date (DOS_DEFAULT_DATE in dos_canon.json). A business rule,
 * not a reading: it carries no confidence. */
export const DEFAULT_DOS = "2222-02-02";

function text(value: string | null | undefined): string | null {
  return (value ?? "").trim() || null;
}

/** A page's DOS: its own, else the default. Never the DOS of the page before it. */
function withDefaultDos(
  from: string | null,
  to: string | null,
  confidence: number | null,
): { dosFrom: string; dosTo: string; dosConfidence: number | null } {
  if (!from && !to) {
    return { dosFrom: DEFAULT_DOS, dosTo: DEFAULT_DOS, dosConfidence: null };
  }
  const dosFrom = displayIsoDates((from ?? to) as string);
  const dosTo = displayIsoDates((to ?? from) as string);
  return { dosFrom, dosTo, dosConfidence: confidence };
}

/** Doc Summary rows, in page order, each with its own DOS or the default. */
function withPageDos(pages: ImagingPageResult[], dosProcessed: boolean): ImagingPageResult[] {
  const ordered = [...pages].sort((a, b) => a.pageNumber - b.pageNumber);
  if (!dosProcessed) return ordered;
  return ordered.map((page) => ({
    ...page,
    ...withDefaultDos(text(page.dosFrom), text(page.dosTo), page.dosConfidence ?? null),
  }));
}

/** Document-level DOS for download: the document date, else the page's own, else the default. */
export function fillDocDosForDownload(pages: ImagingPageResult[]): ImagingPageResult[] {
  return [...pages]
    .sort((a, b) => a.pageNumber - b.pageNumber)
    .map((page) => {
      const { dosFrom, dosTo, dosConfidence } = withDefaultDos(
        text(page.docDosFrom) ?? text(page.dosFrom),
        text(page.docDosTo) ?? text(page.dosTo),
        page.dosConfidence ?? null,
      );
      return { ...page, docDosFrom: dosFrom, docDosTo: dosTo, dosConfidence };
    });
}

type ImagingTab = "page" | "sequencing" | "encounter" | "doc" | "additional";
type DocView = "values" | "rejection";

type Props = {
  tab: ImagingTab;
  loading: boolean;
  error: string | null;
  document: ImagingDocumentResponse | null;
  /** Manifest from folder shell (SQL) — shown while imaging payload loads. */
  shellManifest?: ImagingManifestDetails | null;
  currentPage: ImagingPageResult | null;
  currentFileName: string | null;
  /** Section headers for the current page (Final2 preferred, else Final1). */
  sectionHeaders?: OcrSectionHeader[];
  /** Which OCR kind supplied ``sectionHeaders``. */
  sectionHeadersSource?: "final1" | "final2" | null;
  /** True when the page is blank/junk — show skip message instead of coords. */
  sectionHeadersSkipped?: boolean;
  sectionHeadersLoading?: boolean;
  /** Natural image size — used to show pixel bboxes like document-processing. */
  imageNaturalSize?: { w: number; h: number } | null;
  /** Staged key/value extraction for the current page. Processed copies extracted. */
  extraction?: ExtractionReviewResponse | null;
  /** "all" draws every Heron header box. A number draws that header only. */
  headerHighlight?: "all" | number | null;
  onHeaderHighlight?: (next: "all" | number | null) => void;
  onGroundTruthSaved?: (
    pageNumber: number,
    fileName: string,
    groundTruth: PageGroundTruth,
  ) => void;
  groundTruthLeave?: MutableRefObject<GroundTruthLeave | null>;
  /** The fullscreen drawer: the page table only (no manifest): Field / Extracted /
   * Final / Ground Truth, no Confidence, read-only. */
  compact?: boolean;
};

export type GroundTruthLeave = {
  /** This page has ground truth that has not been saved. */
  dirty: boolean;
  accept: () => Promise<boolean>;
  discard: () => void;
};

const YET_TO_PROCESS = "Yet to Process";
const NOT_FOUND = "Not Found";
const SKIPPED = "Skipped";

function isContinuation(page: ImagingPageResult): boolean {
  return (
    page.dosMatch === "span" ||
    (page.finalDos || "").trim().toLowerCase() === "continuation"
  );
}

/** The Extracted DOS: the DOS stage's own date for this page (``<chart>_dos.csv``), never
 * the key/value extractor's pick in staging. A legacy span row copied the encounter date in. */
function extractedDos(
  page: ImagingPageResult,
  stored: string | null | undefined,
  processed: boolean,
  skipped: boolean,
): string {
  const trustStored = !isContinuation(page) || Boolean((page.finalDos || "").trim());
  if (!trustStored) return fmt("", processed, { skipped });
  return displayIsoDates(fmt(stored, processed, { skipped }));
}

function spanDate(
  page: ImagingPageResult,
  side: "from" | "to",
  processed: boolean,
  skipped: boolean,
): string {
  const marked = (page.finalDos || "").trim();
  if (marked && marked.toLowerCase() !== "continuation") return displayIsoDates(marked);
  const stored = side === "from" ? page.docDosFrom : page.docDosTo;
  return displayIsoDates(fmt(stored, processed, { skipped }));
}

function finalDosValue(
  page: ImagingPageResult,
  extracted: string,
  side: "from" | "to",
  processed: boolean,
  skipped: boolean,
): string {
  // imaging_final's Final DOS: the document's first dated page's date.
  const carried = side === "from" ? page.finalDosFrom : page.finalDosTo ?? page.finalDosFrom;
  if (carried) return displayIsoDates(carried);
  if (page.documentSeq != null) return extracted;
  if (!isContinuation(page)) return extracted;
  const date = spanDate(page, side, processed, skipped);
  if (date === NOT_FOUND || date === SKIPPED || date === YET_TO_PROCESS) return extracted;
  return date;
}

/** Blank / junk — downstream member/DOS/encounter metrics were skipped. A duplicate is
 * not: it shows its original's values (withDuplicateValues, 2026-10-26, user). */
function isBlankJunkPage(page: ImagingPageResult): boolean {
  if (page.isDuplicate === true) return false;
  const bj = (page.blankOrJunk || "").trim().toLowerCase();
  if (bj.startsWith("yes")) return true;
  const pt = (page.pageType || "").trim().toLowerCase();
  return pt === "blank" || pt === "duplicate";
}

function fmt(
  value: string | number | boolean | null | undefined,
  processed = true,
  opts?: { skipped?: boolean },
): string {
  if (opts?.skipped) return SKIPPED;
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  return String(value);
}

function fmtDegrees(
  value: number | null | undefined,
  processed = true,
  opts?: { skipped?: boolean },
): string {
  if (opts?.skipped) return SKIPPED;
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return NOT_FOUND;
  const n = Number.isInteger(value) ? String(value) : value.toFixed(2);
  return `${n}°`;
}

function fmtConfidence(
  value: number | null | undefined,
  processed = true,
  opts?: { skipped?: boolean },
): string {
  if (opts?.skipped || !processed || value === null || value === undefined) return "NA";
  const pct = value <= 1 ? value * 100 : value;
  return `${pct.toFixed(1)}%`;
}

/** One scale for every confidence (core-pipeline/stages/lib/confidence.py). */
const GOOD_AT = 80;
const REVIEW_BELOW = 50;

/** A Final value's confidence when the run stored one, else the Extracted value's. */
function finalOr(page: ImagingPageResult, key: string, extracted: number | null | undefined): number | null {
  const stored = page.finalConfidences?.[key];
  return typeof stored === "number" ? stored : extracted ?? null;
}

/** A confidence cell coloured by its band: good (>= 80%), passable, review (< 50%). */
function ConfidenceCell({ text }: { text: string }) {
  const match = /^(\d+(?:\.\d+)?)%$/.exec(text.trim());
  if (!match) return <>{text}</>;
  const pct = Number(match[1]);
  const band = pct >= GOOD_AT ? "good" : pct >= REVIEW_BELOW ? "passable" : "review";
  return (
    <span className={`conf-band conf-${band}`} title={band === "review" ? "Below 50%: review this value" : band}>
      {text}
    </span>
  );
}

/** ``duplicate`` (the page itself) shows a duplicate (≥ 98%) that is not already blank or
 * junk as ``Yes (Junk)`` -- for a run stored before the backend sent it itself.
 * Display only: scoring reads the stored verdict. */
function fmtBlankOrJunk(
  value: string | boolean | null | undefined,
  processed = true,
  duplicate?: Pick<ImagingPageResult, "isDuplicate" | "pageTypeConfidence">,
): string {
  if (!processed) return YET_TO_PROCESS;
  const blankOrJunk = typeof value === "boolean" ? value : String(value ?? "").trim().toLowerCase().startsWith("yes");
  if (
    duplicate &&
    !blankOrJunk &&
    isCompleteDuplicate(duplicate.isDuplicate, duplicate.pageTypeConfidence)
  ) {
    return "Yes (Junk)";
  }
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  if (typeof value === "boolean") return value ? "Yes (Junk)" : "No";
  return String(value);
}

function fmtDuplicate(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  processed = true,
  duplicateOf?: string | null,
  isPartial?: boolean | null,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, processed, {
    yetToProcess: YET_TO_PROCESS,
    notFound: NOT_FOUND,
    duplicateOf,
    isPartial,
  });
}

// The classifier writes lowercase labels ("printed", "handwritten", "mixed").
// They are shown next to title-case values like "Yes"/"No"/"Not Found", so
// rendering them raw made the column look like leaked internals. Display only
// — the stored value and the CSV export stay exactly as the pipeline wrote
// them, because those are a contract with the V1 reference.
function fmtHandwriting(
  value: string | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  const text = String(value).trim();
  // Capitalise whatever comes back rather than mapping known labels, so a new
  // label from a retrained classifier still displays sensibly instead of
  // falling through to "Not Found".
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

// Visibility and handwritten area come only from the page-tag model. With the
// two-class model the page was classified but these were never measured.
const NOT_MEASURED = "Not Available";

function fmtVisibility(value: boolean | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return NOT_MEASURED;
  return value ? "Visible" : "Not visible";
}

/** Handwritten share of the page as a band; the model's exact figure is
 * not precise enough to show to a decimal. */
const HW_AREA_BANDS = [
  { below: 5, label: "Negligible (~0%)" },
  { below: 20, label: "Low (<10%)" },
  { below: 50, label: "Medium (~25%)" },
  { below: Infinity, label: "High (50% or higher)" },
] as const;
const HW_AREA_CHOICES = HW_AREA_BANDS.map((band) => band.label);

function fmtHandwrittenArea(value: number | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || Number.isNaN(value)) return NOT_MEASURED;
  return HW_AREA_BANDS.find((band) => value < band.below)!.label;
}

function fmtQualityTag(value: string | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  const text = String(value).trim();
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

/** ``Family (Page Type)`` from the classifier. No parenthesis means the two names match. */
export function splitPageType(value: string | null | undefined): {
  family: string | null;
  subtype: string | null;
} {
  const raw = value == null ? "" : String(value).trim();
  if (!raw) return { family: null, subtype: null };
  const match = /^(.+?)\s+\((.+)\)\s*$/.exec(raw);
  if (!match) return { family: raw, subtype: raw };
  const family = match[1].trim();
  const subtype = match[2].trim();
  if (!family || !subtype) return { family: raw, subtype: raw };
  return { family, subtype };
}

/** "New (Doc 3)", "Last (Junk J1)" — the label only, no reason; Not Found outside a
 * document. A junk page has a label but no document number. */
function fmtDocument(page: ImagingPageResult): string {
  if (page.documentLabel) return page.documentLabel;
  if (page.documentSeq == null) return NOT_FOUND;
  const position = page.documentPosition ? page.documentPosition[0].toUpperCase() + page.documentPosition.slice(1) : "";
  return page.documentLabel || `${position} (Doc ${page.documentSeq})`;
}

function fmtPageType(value: string | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  return spellCodeable(String(value));
}

function fmtCodeable(
  value: string | null | undefined,
  pageType: string | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  const label = value != null && String(value).trim() !== "" ? String(value).trim() : "";
  if (label) return displayCodeable(label);
  const pt = pageType != null ? String(pageType).trim() : "";
  if (!pt || pt === "Not Available") return "Not Sure";
  return NOT_FOUND;
}

function fmtPagesMatched(
  v: ImagingVerificationDetails,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (v.pagesMatched != null && v.pagesChecked != null) {
    return `${v.pagesMatched}/${v.pagesChecked}`;
  }
  if (v.pagesMatched != null) return String(v.pagesMatched);
  return NOT_FOUND;
}

type CompareRow = {
  id: string;
  label: string;
  extracted: string;
  processed: string;
  confidence: string;
  /** Ground-truth comparison; omit for fields the client sheet has no column for. */
  truth?: GtBit[];
  /** Closed set. A wrong review picks one of these; other fields take typed text. */
  choices?: readonly string[];
  /** Ground truth is only Yes or No: whether Final has a value. */
  presence?: boolean;
  /** Shown as Not Found. The stored value is kept and used elsewhere. */
  fixedNa?: boolean;
  /** A ground-truth cell that only reads this (no entry yet), e.g. "Not Available". */
  fixedText?: string;
  /** Page Subtype: its options are the L2s of the L1 in this row's ground truth (every L2
   * until one of ours is there). */
  l2Of?: string;
  /** This row has no ground-truth cell. */
  noGroundTruth?: boolean;
  /** Show the value, but not a tick or cross. */
  noMark?: boolean;
  /** A required label. Shown solid. Other empty labels stay faint. */
  key?: boolean;
  /** Shown on the right of Extracted and Final. Not part of the compared value. */
  note?: string;
  /** The ground-truth cell spans this many rows (Page Type + Page Subtype share one). */
  gtRowSpan?: number;
  /** The row above's ground-truth cell covers this row. */
  gtHidden?: boolean;
  /** Custom scoring of the ground truth (default: ``enteredMark`` on Extracted / Final). */
  compare?: (truth: string) => "match" | "partial" | "mismatch" | null;
  /** A blank (or NA) label expects no value: no Extracted value is a tick, one is a cross,
   * as in the accuracy report. Set on DOS rows of a page whose ground truth has no date. */
  blankIsNone?: boolean;
};

const BLANK_JUNK_CHOICES = ["Yes (Blank)", "Yes (Junk)", "No"] as const;

/** Blank / Junk on the page view, as the accuracy report scores it: any Yes against any
 * Yes is a tick (Yes (Junk) against Yes (Blank) included), No against No a tick; Final
 * wrong but Extracted right is "!"; otherwise a cross. */
function blankJunkCompare(extracted: string, final: string) {
  const side = (value: string): "yes" | "no" | null => {
    const text = value.trim().toLowerCase();
    if (text.startsWith("yes")) return "yes";
    if (text.startsWith("no")) return "no";
    return null;
  };
  return (truth: string): "match" | "partial" | "mismatch" | null => {
    const expected = side(truth);
    if (!expected) return null;
    const got = side(final);
    if (got === expected) return "match";
    return side(extracted) === expected ? "partial" : "mismatch";
  };
}
/** The merged Page Type / Page Subtype ground truth takes either kind of name. */
const PAGE_TYPE_AND_SUBTYPE_CHOICES = [...new Set([...PAGE_TYPE_CHOICES, ...PAGE_SUBTYPE_CHOICES])];
const DUPLICATE_CHOICES = ["Yes", "Partial", "No"] as const;
const CODEABLE_CHOICES = ["Codable", "Non-Codable", "Discharge", "Not Sure"] as const;
const TYPE_CHOICES = ["Printed", "Handwritten", "Form", "Visual", "Blank"] as const;
/** The page quality grades (ocr_quality_results.quality_tag). */
const QUALITY_CHOICES = ["High", "Medium", "Low"] as const;

/** Page type; a page classified before document type existed shows its old label. */
function pageTypeOf(page: ImagingPageResult): string | null {
  return page.documentType || page.handwrittenOrPrinted || null;
}
const YES_NO_CHOICES = ["Yes", "No"] as const;
const VISIBILITY_CHOICES = ["Visible", "Not visible"] as const;
// An empty ground-truth cell reads "Not Found"; typing it (or NA) clears the cell.
const CLEARED_ENTRY = new Set(["", "na", "n/a", "not found", "not available"]);
const PLACEHOLDER_ENTRY = new Set(["yet to process"]);
const NOT_AVAILABLE_ENTRY = "Not Available";
const ORIENTATION_CHOICES = ["0°", "90°", "180°", "270°"] as const;
/** Lists shorter than this show every option under the input; longer ones drop down
 * and narrow as you type (2026-10-26, user). */
const INLINE_OPTIONS_BELOW = 10;
/** A ground truth every Page Details cell takes (2026-10-26, user): the pipeline rightly
 * skipped the field on this page (a blank page has no DOS, a junk page no provider). */
const SKIPPED_ENTRY = "Skipped";
/** Labels, not values: an empty one reads Not Available, every other Not Found
 * (2026-10-27, user). */
const NOT_AVAILABLE_FIELDS = new Set(["page_type", "page_subtype", "specialty", "encounter", "quality"]);

function emptyLabel(fieldId: string): string {
  return NOT_AVAILABLE_FIELDS.has(fieldId) ? NOT_AVAILABLE_ENTRY : NOT_FOUND;
}

/** "New Type" / "New Subtype" / "New Specialty" for a typed value none of the listed
 * ones match; it is still saved (2026-10-27, user). */
function newEntryWarning(fieldId: string, value: string, options: readonly string[]): string | null {
  const open = ENTRY_RULES[fieldId]?.open;
  const text = value.trim();
  if (!open || !text || CLEARED_ENTRY.has(text.toLowerCase())) return null;
  if (exactOption(text, options) || exactOption(text, ENTRY_RULES[fieldId]?.choices ?? [])) return null;
  return open;
}

function exactOption(value: string, options: readonly string[]): string | null {
  const folded = value.trim().toLowerCase();
  return options.find((option) => option.toLowerCase() === folded) ?? null;
}

function canonicalDate(value: string): string | null {
  const shown = displayIsoDates(value.trim());
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(shown);
  if (!match) return null;
  const year = Number(match[1]);
  const month = Number(match[2]);
  const day = Number(match[3]);
  const date = new Date(Date.UTC(year, month - 1, day));
  if (date.getUTCFullYear() !== year || date.getUTCMonth() !== month - 1 || date.getUTCDate() !== day) {
    return null;
  }
  return shown;
}

function canonicalDegrees(value: string): string | null {
  const text = value.trim().replace(/°$/, "").trim();
  if (!/^-?\d+(\.\d+)?$/.test(text)) return null;
  const angle = Number(text);
  if (!Number.isFinite(angle) || angle < -360 || angle > 360) return null;
  return `${text}°`;
}

function canonicalLabel(value: string): string | null {
  const text = value.trim();
  if (PLACEHOLDER_ENTRY.has(text.toLowerCase())) return null;
  return text;
}

type EntryRule = {
  presence?: boolean;
  choices?: readonly string[];
  /** Any text is taken; one not in ``choices`` shows this warning (2026-10-27, user). */
  open?: string;
  kind?: "date" | "degrees" | "text";
};

const ENTRY_RULES: Record<string, EntryRule> = {
  name: { presence: true },
  dob: { presence: true },
  member_id: { presence: true },
  electronic_signature: { presence: true },
  handwriting: { choices: TYPE_CHOICES },
  mirrored: { choices: YES_NO_CHOICES },
  blank_junk: { choices: BLANK_JUNK_CHOICES },
  duplicate: { choices: DUPLICATE_CHOICES },
  // The menu offers our L1s; a client label or older name saved before still validates.
  page_type: { choices: [...PAGE_L1_CHOICES, ...GT_PAGE_TYPE_LABELS, ...PAGE_TYPE_AND_SUBTYPE_CHOICES], open: "New Type" },
  page_subtype: { choices: PAGE_L2_ALL, open: "New Subtype" },
  specialty: { choices: SPECIALTY_CHOICES, open: "New Specialty" },
  codeable: { choices: CODEABLE_CHOICES },
  encounter: { choices: ENCOUNTER_TYPE_CHOICES },
  quality: { choices: QUALITY_CHOICES },
  visibility: { choices: VISIBILITY_CHOICES },
  dos_from: { kind: "date" },
  dos_to: { kind: "date" },
  orientation: { kind: "degrees" },
  // Any text (2026-10-27, user).
  tilt: { kind: "text" },
  handwritten_area: { choices: HW_AREA_CHOICES },
  provider_name: { kind: "text" },
  provider_credentials: { kind: "text" },
};

/** A listed choice, a real date, angle, or percent. Blank clears the cell. */
function normalizeEntry(id: string, value: string): string | null {
  if (CLEARED_ENTRY.has(value.trim().toLowerCase())) return "";
  if (value.trim().toLowerCase() === "skipped") return SKIPPED_ENTRY;
  const rule = ENTRY_RULES[id];
  if (!rule) return value.trim();
  if (rule.presence) return exactOption(value, YES_NO_CHOICES);
  if (rule.choices) return exactOption(value, rule.choices) ?? (rule.open ? canonicalLabel(value) : null);
  if (rule.kind === "date") return canonicalDate(value);
  if (rule.kind === "degrees") return canonicalDegrees(value);
  return canonicalLabel(value);
}

function entryHint(id: string): string {
  const rule = ENTRY_RULES[id];
  if (!rule || rule.presence || rule.choices) return "Invalid Value";
  if (rule.kind === "date") return "Enter a date";
  if (rule.kind === "degrees") return "Enter an angle";
  return "That value can't be saved";
}

function saveErrorMessage(err: unknown): string {
  const raw = err instanceof Error ? err.message : "";
  try {
    const parsed = JSON.parse(raw) as { detail?: string };
    if (parsed.detail) return parsed.detail;
  } catch {
    /* response was plain text */
  }
  return raw || "Could not save ground truth";
}

/** Ground-truth text that means "nothing here" (as the backend's clean_label). */
const GT_EMPTY = new Set(["", "na", "n/a", "not found", "not available"]);

function cleanGt(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  if (GT_EMPTY.has(text.toLowerCase())) return null;
  return text;
}

function yesNo(value: string | null | undefined): "yes" | "no" | null {
  const folded = (value ?? "").trim().toLowerCase();
  if (folded === "yes" || folded === "y" || folded === "true") return "yes";
  if (folded === "no" || folded === "n" || folded === "false") return "no";
  return null;
}

/** page_ground_truth.is_visible holds Yes / No; the cell offers Visible / Not visible. */
function visibilityLabel(value: string | null | undefined): string {
  const side = yesNo(value);
  if (side === "yes") return "Visible";
  if (side === "no") return "Not visible";
  return (value ?? "").trim();
}

function visibilityStored(shown: string): string | null {
  const text = cleanGt(shown);
  if (!text) return null;
  const folded = text.toLowerCase();
  if (folded === "visible" || folded === "good visibility") return "Yes";
  if (folded === "not visible" || folded === "bad visibility") return "No";
  return text;
}

function rotationLabel(value: string | null | undefined): string {
  const text = (value ?? "").trim().replace(/°$/, "");
  if (!text) return "";
  return /^-?\d+(\.\d+)?$/.test(text) ? `${text}°` : text;
}

function blankJunkLabel(gt: PageGroundTruth | null | undefined): string {
  if (!gt) return "";
  if (yesNo(gt.blankPage) === "yes") return "Yes (Blank)";
  if (yesNo(gt.junkPage) === "yes" || yesNo(gt.isInvoice) === "yes") return "Yes (Junk)";
  if (yesNo(gt.blankPage) === "no" || yesNo(gt.junkPage) === "no" || yesNo(gt.isInvoice) === "no") {
    return "No";
  }
  return "";
}

function blankJunkStored(shown: string): Pick<PageGroundTruth, "blankPage" | "junkPage" | "isInvoice"> {
  const text = cleanGt(shown);
  if (!text) return { blankPage: null, junkPage: null, isInvoice: null };
  const folded = text.toLowerCase();
  if (folded.includes("blank")) return { blankPage: "Yes", junkPage: "No", isInvoice: "No" };
  if (folded.includes("junk")) return { blankPage: "No", junkPage: "Yes", isInvoice: "No" };
  if (folded === "no") return { blankPage: "No", junkPage: "No", isInvoice: "No" };
  return { blankPage: text, junkPage: null, isInvoice: null };
}

/** Values already stored for this page. Empty string displays as Not Found. */
function storedGroundTruth(gt: PageGroundTruth | null | undefined): Record<string, string> {
  // The ground-truth page type lives in the Encounter Type column (Page Type: fallback).
  const pageType = cleanGt(gt?.encounterType) ?? cleanGt(gt?.pageType) ?? "";
  const stored: Record<string, string> = {
    name: presenceStored(gt?.memberName),
    dob: presenceStored(gt?.memberDob),
    member_id: presenceStored(gt?.memberId),
    provider_name: (gt?.renderingProvider ?? "").trim(),
    provider_credentials: gtCredentials(gt),
    electronic_signature: gtSignature(gt),
    visibility: visibilityLabel(gt?.isVisible),
    handwritten_area: (gt?.handwrittenPct ?? "").trim(),
    quality: (gt?.quality ?? "").trim(),
    orientation: rotationLabel(gt?.rotation),
    tilt: (gt?.tilt ?? "").trim(),
    encounter: (gt?.encounterLabel ?? "").trim(),
    mirrored: (gt?.mirrored ?? "").trim(),
    handwriting: (gt?.printedOrHandwritten ?? "").trim(),
    duplicate: (gt?.isDuplicate ?? "").trim(),
    dos_from: (gt?.dosFrom ?? "").trim(),
    dos_to: (gt?.dosTo ?? "").trim(),
    blank_junk: blankJunkLabel(gt),
    page_type: spellCodeable(pageType),
    page_subtype: (gt?.pageSubtype ?? "").trim(),
    specialty: (gt?.specialty ?? "").trim(),
    codeable: spellCodeable((gt?.codeable ?? "").trim()),
  };
  // An uploaded NA is an empty cell: it shows as Not Found and is not scored.
  return Object.fromEntries(Object.entries(stored).map(([id, value]) => [id, cleanGt(value) ?? ""]));
}

function groundTruthBody(
  page: ImagingPageResult,
  values: Record<string, string>,
): PageGroundTruth {
  const pageType = cleanGt(values.page_type);
  return {
    pageNumber: page.pageNumber,
    sourcePageId: page.fileName,
    memberName: presenceStored(values.name) || null,
    memberDob: presenceStored(values.dob) || null,
    memberId: presenceStored(values.member_id) || null,
    renderingProvider: cleanGt(values.provider_name),
    providerSignature: presenceStored(values.electronic_signature) || null,
    providerCredentials: cleanGt(values.provider_credentials),
    isVisible: visibilityStored(values.visibility ?? ""),
    handwrittenPct: cleanGt(values.handwritten_area),
    quality: cleanGt(values.quality),
    tilt: cleanGt(values.tilt),
    encounterLabel: cleanGt(values.encounter),
    mirrored: cleanGt(values.mirrored),
    printedOrHandwritten: cleanGt(values.handwriting),
    isDuplicate: cleanGt(values.duplicate),
    rotation: cleanGt((values.orientation ?? "").replace(/°$/, "")),
    // Page type ground truth is stored in Encounter Type (what accuracy reads).
    encounterType: pageType,
    dosFrom: cleanGt(values.dos_from),
    dosTo: cleanGt(values.dos_to),
    ...blankJunkStored(values.blank_junk ?? ""),
    pageType,
    codeable: cleanGt(values.codeable),
    pageSubtype: cleanGt(values.page_subtype),
    specialty: cleanGt(values.specialty),
    pageSequence: page.groundTruth?.pageSequence ?? null,
  };
}

/** Fields the save request actually writes. */
const PERSISTED_GT = new Set([
  "name",
  "dob",
  "member_id",
  "orientation",
  "dos_from",
  "dos_to",
  "blank_junk",
  "page_type",
  "codeable",
  "provider_name",
  "provider_credentials",
  "electronic_signature",
  "page_subtype",
  "specialty",
  "quality",
  "tilt",
  "encounter",
  "mirrored",
  "handwriting",
  "duplicate",
  "visibility",
  "handwritten_area",
]);

const UNCOMPARED = new Set(["", "na", "n/a", "not found", "not available", "yet to process", "skipped"]);

/** The pipeline's value offered as the ground truth on a page nobody has labelled. */
function confirmedValue(processed: string): string {
  const text = processed.trim();
  if (!text || text === YET_TO_PROCESS || text === SKIPPED || text === NOT_FOUND) return "";
  return text;
}

function presenceStored(value: string | null | undefined): string {
  const side = yesNo(value);
  if (side === "yes") return "Yes";
  if (side === "no") return "No";
  return "";
}

function valueFound(processed: string): boolean {
  const fold = (raw: string) => displayIsoDates(raw).trim().toLowerCase().replace(/\s+/g, " ");
  return !UNCOMPARED.has(fold(processed));
}

/** Tick when the entered ground truth matches Final.
    ! when Final misses and Extracted matches. X when neither matches.
    Yes/No fields tick when Yes agrees with a found value, or No agrees with none. */
function groundTruthMark(
  processed: string,
  truth: string,
  presence = false,
): "match" | "mismatch" | null {
  const skipped = (raw: string) => raw.trim().toLowerCase() === "skipped";
  if (skipped(truth)) return skipped(processed) ? "match" : "mismatch";
  if (presence) {
    const side = yesNo(truth);
    if (!side) return null;
    const processedSide = yesNo(processed);
    const found = processedSide ? processedSide === "yes" : valueFound(processed);
    return (side === "yes") === found ? "match" : "mismatch";
  }
  const fold = (raw: string) => displayIsoDates(raw).trim().toLowerCase().replace(/\s+/g, " ");
  const entered = fold(truth);
  if (UNCOMPARED.has(entered)) return null;
  const final = fold(processed);
  if (UNCOMPARED.has(final)) return "mismatch";
  return entered === final ? "match" : "mismatch";
}

function enteredMark(
  extracted: string,
  final: string,
  truth: string,
  presence: boolean,
): "match" | "partial" | "mismatch" | null {
  // Either the Final or the Extracted value matching is a match (2026-10-26, user).
  const againstFinal = groundTruthMark(final, truth, presence);
  if (againstFinal !== "mismatch") return againstFinal;
  return groundTruthMark(extracted, truth, presence) === "match" ? "match" : "mismatch";
}

function CompareSection({
  title,
  rows,
  editingId,
  values,
  onCommit,
  onAccept,
  onCancel,
  onDraft,
  onStartEdit,
  saving,
  verdicts,
  toolbar,
  compact = false,
  withTruth = true,
}: {
  title: string;
  rows: CompareRow[];
  editingId: string | null;
  values: Record<string, string>;
  onCommit: (id: string, value: string) => void;
  onAccept: (id: string, value: string) => void;
  onCancel: () => void;
  onDraft: (value: string) => void;
  onStartEdit: (id: string) => void;
  saving: boolean;
  verdicts: Record<string, "accept" | "enter">;
  toolbar?: ReactNode;
  /** The fullscreen pane: Field / Extracted / Final / Ground Truth side by side,
   * no gap, read-only. */
  compact?: boolean;
  /** Compact only: false drops the Ground Truth column (the page has none). */
  withTruth?: boolean;
}) {
  const truthColumn = !compact || withTruth;
  // The client's copy: no Extracted column, and Final is headed Algorithm.
  const finalOnly = useContext(ClientViewContext);
  const finalHeading = finalOnly ? "Algorithm" : "Final";
  return (
    <section className="imaging-section">
      <div className="imaging-section-head">
        <h3 className="imaging-section-title">{title}</h3>
        {toolbar}
      </div>
      <table
        className={`imaging-detail-table imaging-value-table${compact ? " is-compact" : ""}${finalOnly ? " is-final-only" : ""}`}
      >
        {compact ? (
          <colgroup>
            <col className="col-field" />
            {finalOnly ? null : <col className="col-extracted" />}
            <col className="col-processed" />
            {truthColumn ? <col className="col-gt" /> : null}
          </colgroup>
        ) : (
          <colgroup>
            <col className="col-field" />
            {finalOnly ? null : <col className="col-extracted" />}
            <col className="col-processed" />
            <col className="col-confidence" />
            <col className="col-gap" />
            <col className="col-gt" />
          </colgroup>
        )}
        <thead>
          {compact ? (
            <tr>
              <th scope="col">Field</th>
              {finalOnly ? null : <th scope="col">Extracted</th>}
              <th scope="col">{finalHeading}</th>
              {truthColumn ? <th scope="col">Ground Truth</th> : null}
            </tr>
          ) : (
            <tr>
              <th scope="col">Field</th>
              {finalOnly ? null : <th scope="col">Extracted</th>}
              <th scope="col">{finalHeading}</th>
              <th scope="col">Confidence</th>
              <th className="gt-gap" aria-hidden="true" />
              <th scope="col" className="gt-shade">
                Ground Truth
              </th>
            </tr>
          )}
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row">{row.label}</th>
              {finalOnly ? null : (
                <td>
                  <ValueWithNote value={row.extracted} note={row.note} />
                </td>
              )}
              <td>
                <ValueWithNote value={row.processed} note={row.note} />
              </td>
              {compact ? null : <td><ConfidenceCell text={row.confidence} /></td>}
              {row.gtHidden || !truthColumn ? null : (
              <>
              {compact ? null : <td className="gt-gap" aria-hidden="true" rowSpan={row.gtRowSpan} />}
              <td
                className={compact ? (row.gtRowSpan ? "gt-merged" : undefined) : `gt-shade${row.gtRowSpan ? " gt-merged" : ""}`}
                rowSpan={row.gtRowSpan}
              >
                <GroundTruthCell
                  row={row.l2Of ? { ...row, choices: pageL2Choices(values[row.l2Of]) } : row}
                  readOnly={compact}
                  editing={editingId === row.id}
                  value={values[row.id] ?? ""}
                  saving={saving}
                  verdict={verdicts[row.id]}
                  suggest={!withTruth}
                  onCommit={(next) => onCommit(row.id, next)}
                  onAccept={(next) => onAccept(row.id, next)}
                  onCancel={onCancel}
                  onDraft={onDraft}
                  onStartEdit={() => onStartEdit(row.id)}
                />
              </td>
              </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

function GroundTruthCell({
  row,
  editing,
  value,
  saving,
  verdict,
  suggest = false,
  onCommit,
  onAccept,
  onCancel,
  onDraft,
  onStartEdit,
  readOnly = false,
}: {
  row: CompareRow;
  readOnly?: boolean;
  editing: boolean;
  value: string;
  saving: boolean;
  verdict?: "accept" | "enter";
  /** The page has no ground truth yet: offer the pipeline's value with ✓ Accept / ✕ Change. */
  suggest?: boolean;
  onCommit: (value: string) => void;
  onAccept: (value: string) => void;
  onCancel: () => void;
  onDraft: (value: string) => void;
  onStartEdit: () => void;
}) {
  if (row.noGroundTruth) return <span className="imaging-gt-slot" />;
  if (row.fixedNa || row.fixedText) {
    return (
      <span className="imaging-gt-slot">
        <span className="imaging-gt-copied">{row.fixedText ?? NOT_FOUND}</span>
      </span>
    );
  }

  // The column shows only the ground truth: an empty cell (blank, NA, Not Found) reads
  // Not Found and is not compared; nothing is copied in from the pipeline's value.
  const stored = row.presence ? presenceStored(value) : value.trim();
  // A page nobody has labelled offers the pipeline's value; a Yes / No Final ("Provider
  // Signature: No") is offered as itself, not as "a value was found".
  const processedSide = yesNo(row.processed);
  const copied = !suggest
    ? ""
    : row.presence
      ? (processedSide ? processedSide === "yes" : valueFound(row.processed)) ? "Yes" : "No"
      : confirmedValue(row.processed);
  const offer = suggest && !stored && !verdict;
  const fromStore = !verdict && Boolean(stored);
  const effective = verdict ?? (fromStore ? "enter" : undefined);
  const compared =
    stored && effective === "enter"
      ? row.compare
        ? row.compare(stored)
        : enteredMark(row.extracted, row.processed, stored, Boolean(row.presence))
      : null;
  // A blank label on a row that expects none (DOS) is judged on the Extracted date: none is
  // a match, one is wrong. The Final date a continuation page carries from its document
  // does not count against it. The 2222-02-02 placeholder is not a date.
  const truthBlank = noValue(stored);
  const blankNone = truthBlank && Boolean(row.blankIsNone);
  const blankCross = blankNone && valueFound(row.extracted) && !row.extracted.includes("2222");
  const mark = blankCross ? (
    <span className="imaging-gt-mark gt-mismatch" aria-label="No">✕</span>
  ) : blankNone ? (
    <span className="imaging-gt-mark" aria-label="Yes">✓</span>
  ) : !stored && effective !== "accept" ? null : effective === "accept" ||
    (effective === "enter" && (compared === "match" || (compared == null && !fromStore))) ? (
      <span className="imaging-gt-mark" aria-label="Yes">✓</span>
    ) : compared === "partial" ? (
      <span className="imaging-gt-mark gt-partial" aria-label="May be">!</span>
    ) : compared === "mismatch" ? (
      <span className="imaging-gt-mark gt-mismatch" aria-label="No">✕</span>
    ) : null;
  if (readOnly) {
    // The fullscreen pane: the saved ground truth and its mark, nothing to edit.
    return (
      <span className="imaging-gt-slot">
        <span className={stored ? "imaging-gt-text" : "imaging-gt-copied"} title={stored || undefined}>
          {stored || emptyLabel(row.id)}
        </span>
        {(stored || blankCross) && !row.noMark ? mark : null}
      </span>
    );
  }
  const actions = offer ? (
    <span className="imaging-gt-actions">
      <button
        type="button"
        className="imaging-gt-icon"
        aria-label={`Accept ${row.label} ground truth`}
        title="Accept"
        disabled={saving || !copied}
        onClick={() => onAccept(copied)}
      >
        <Check size={12} strokeWidth={2.6} aria-hidden="true" />
      </button>
      <button
        type="button"
        className="imaging-gt-icon imaging-gt-cancel"
        aria-label={`Change ${row.label} ground truth`}
        title="Change"
        disabled={saving}
        onClick={onStartEdit}
      >
        <X size={12} strokeWidth={2.6} aria-hidden="true" />
      </button>
    </span>
  ) : (
    <button
      type="button"
      className="imaging-gt-pen"
      aria-label={`Edit ${row.label} ground truth`}
      title="Edit"
      disabled={saving}
      onClick={onStartEdit}
    >
      <Pencil size={11} strokeWidth={2.4} aria-hidden="true" />
    </button>
  );
  if (!editing) {
    const shown = stored || (offer ? copied : "") || emptyLabel(row.id);
    const dim = !stored && !row.key;
    return (
      <span className="imaging-gt-slot">
        <span className={dim ? "imaging-gt-copied" : "imaging-gt-text"} title={shown}>{shown}</span>
        {row.noMark ? null : mark}
        {actions}
      </span>
    );
  }

  // Every cell: its listed values, then the "no value" entry that fits it -- Not Found
  // for a date, a name or a Yes / No, Not Available for a label -- and Skipped
  // (2026-10-26, 2026-10-27, user).
  const kind = ENTRY_RULES[row.id]?.kind;
  const listed = row.presence
    ? ["Yes", "No"]
    : row.choices ?? (kind === "degrees" && row.id === "orientation" ? ORIENTATION_CHOICES : []);
  const yesNoRow = row.presence || listed.every((option) => yesNo(option) != null);
  const none = NOT_AVAILABLE_FIELDS.has(row.id)
    ? [NOT_AVAILABLE_ENTRY]
    : kind === "date" || kind === "text" || (yesNoRow && listed.length > 0)
      ? [NOT_FOUND]
      : [];
  const values = listed.filter((option) => option !== SKIPPED_ENTRY && !none.includes(option));
  const extras = [...none, SKIPPED_ENTRY];
  // A long list puts them first, so they are not lost under its values.
  const options =
    values.length + extras.length < INLINE_OPTIONS_BELOW ? [...values, ...extras] : [...extras, ...values];
  const known = stored && !options.includes(stored) ? [stored, ...options] : options;
  const draft = row.presence || row.choices ? spellCodeable(stored) : stored || (offer ? copied : "");
  return (
    <span className="imaging-gt-slot is-editing">
      <ChoiceMenu
        fieldId={row.id}
        label={row.label}
        value={draft}
        options={known}
        onCommit={onCommit}
        onCancel={onCancel}
        onDraft={onDraft}
      />
    </span>
  );
}

function ChoiceMenu({
  fieldId,
  label,
  value,
  options,
  onCommit,
  onCancel,
  onDraft,
}: {
  fieldId: string;
  label: string;
  value: string;
  options: readonly string[];
  onCommit: (value: string) => void;
  onCancel: () => void;
  onDraft: (value: string) => void;
}) {
  const isDate = ENTRY_RULES[fieldId]?.kind === "date";
  // A date cell holds YYYY-MM-DD, typed or picked; a "no value" entry stays as typed.
  const initial = isDate ? (canonicalDate(value) ?? (options.includes(value) ? value : value.trim())) : value;
  const [calendar, setCalendar] = useState(false);
  const [text, setText] = useState(initial);
  const [typed, setTyped] = useState(false);
  const [invalid, setInvalid] = useState<string | null>(null);
  const folded = text.trim().toLowerCase();
  // A short list shows every option under the input; a long one is searched: its matches
  // drop down once two letters are typed (2026-10-27, user).
  const inline = isDate || options.length < INLINE_OPTIONS_BELOW;
  const recos = inline
    ? options
    : typed && folded.length >= 2
      ? options.filter((option) => option.toLowerCase().includes(folded))
      : [];
  const pick = (option: string) => {
    setText(option);
    setTyped(false);
    setInvalid(null);
    onDraft(option);
  };
  function commit() {
    const next = normalizeEntry(fieldId, text);
    if (next == null) {
      setInvalid(entryHint(fieldId));
      return;
    }
    onCommit(next);
  }
  function onKeyDown(event: KeyboardEvent) {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      onCancel();
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      commit();
    }
  }
  return (
    <div className="imaging-gt-picker" onKeyDown={onKeyDown}>
      <span className="imaging-gt-entry">
        <input
          aria-label={`${label} ground truth`}
          aria-invalid={invalid ? true : undefined}
          type="text"
          inputMode={isDate ? "numeric" : undefined}
          value={text}
          placeholder={isDate ? "YYYY-MM-DD" : emptyLabel(fieldId)}
          autoFocus
          onChange={(event) => {
            setText(event.target.value);
            setTyped(true);
            setInvalid(null);
            onDraft(event.target.value);
          }}
        />
        {isDate ? (
          <button
            type="button"
            className="imaging-gt-icon"
            aria-label={`Choose ${label} on a calendar`}
            aria-expanded={calendar}
            title="Calendar"
            onMouseDown={(event) => event.preventDefault()}
            onClick={() => setCalendar((open) => !open)}
          >
            <CalendarDays size={12} strokeWidth={2.4} aria-hidden="true" />
          </button>
        ) : null}
        {text !== initial ? <CellSaveButton label={label} onSave={commit} /> : null}
      </span>
      {isDate && calendar ? (
        <DateCalendar
          value={canonicalDate(text)}
          onPick={(date) => {
            pick(date);
            setCalendar(false);
          }}
          onClose={() => setCalendar(false)}
        />
      ) : null}
      {recos.length > 0 && inline ? (
        <div className="imaging-gt-options" role="listbox" aria-label={label}>
          {recos.map((option) => (
            <button
              key={option}
              type="button"
              role="option"
              aria-selected={option === text}
              className={option === text ? "is-active" : undefined}
              onClick={() => pick(option)}
            >
              {option}
            </button>
          ))}
        </div>
      ) : recos.length > 0 ? (
        <ul className="imaging-gt-menu" role="listbox" aria-label={label}>
          {recos.map((option) => (
            <li key={option}>
              <button type="button" role="option" onClick={() => pick(option)}>
                {option}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {invalid ? <span className="imaging-gt-hint">{invalid}</span> : null}
      {!invalid && newEntryWarning(fieldId, text, options) ? (
        <span className="imaging-gt-hint is-warning">{newEntryWarning(fieldId, text, options)}: not in the list</span>
      ) : null}
    </div>
  );
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function isoDay(year: number, month: number, day: number): string {
  return `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

/** A month calendar for a DOS cell, fixed to the window and kept inside it: below the
 * cell, or above it when there is no room (2026-10-27, user). Picks YYYY-MM-DD. */
function DateCalendar({
  value,
  onPick,
  onClose,
}: {
  value: string | null;
  onPick: (date: string) => void;
  onClose: () => void;
}) {
  const start = value ? new Date(`${value}T00:00:00`) : new Date();
  const [month, setMonth] = useState({ year: start.getFullYear(), month: start.getMonth() });
  const [anchor, setAnchor] = useState<HTMLDivElement | null>(null);
  const [place, setPlace] = useState<{ top: number; left: number } | null>(null);
  useLayoutEffect(() => {
    const cell = anchor?.parentElement;
    if (!anchor || !cell) return;
    const margin = 8;
    const box = cell.getBoundingClientRect();
    const width = anchor.offsetWidth;
    const height = anchor.offsetHeight;
    const below = box.bottom + 4;
    const top = below + height + margin <= window.innerHeight ? below : Math.max(margin, box.top - height - 4);
    const left = Math.min(Math.max(margin, box.left), window.innerWidth - width - margin);
    setPlace({ top, left });
  }, [anchor, month]);
  useEffect(() => {
    const close = (event: Event) => {
      if (anchor && event.target instanceof Node && anchor.parentElement?.contains(event.target)) return;
      onClose();
    };
    // Fixed to the window, so a scroll or resize would leave it behind its cell: close it.
    document.addEventListener("mousedown", close);
    window.addEventListener("resize", onClose);
    window.addEventListener("scroll", onClose, true);
    return () => {
      document.removeEventListener("mousedown", close);
      window.removeEventListener("resize", onClose);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [anchor, onClose]);
  const step = (months: number) =>
    setMonth(({ year, month: m }) => {
      const next = new Date(year, m + months, 1);
      return { year: next.getFullYear(), month: next.getMonth() };
    });
  const firstWeekday = new Date(month.year, month.month, 1).getDay();
  const days = new Date(month.year, month.month + 1, 0).getDate();
  const today = new Date();
  const todayIso = isoDay(today.getFullYear(), today.getMonth(), today.getDate());
  return (
    <div
      ref={setAnchor}
      className="imaging-gt-calendar"
      role="dialog"
      aria-label="Choose a date"
      style={place ? { top: place.top, left: place.left } : { visibility: "hidden" }}
      onMouseDown={(event) => event.preventDefault()}
    >
      <div className="imaging-gt-calendar-head">
        <button type="button" aria-label="Previous year" onClick={() => step(-12)}>«</button>
        <button type="button" aria-label="Previous month" onClick={() => step(-1)}>
          <ChevronLeft size={12} aria-hidden="true" />
        </button>
        <span>
          {MONTHS[month.month]} {month.year}
        </span>
        <button type="button" aria-label="Next month" onClick={() => step(1)}>
          <ChevronRight size={12} aria-hidden="true" />
        </button>
        <button type="button" aria-label="Next year" onClick={() => step(12)}>»</button>
      </div>
      <div className="imaging-gt-calendar-grid">
        {["S", "M", "T", "W", "T", "F", "S"].map((day, index) => (
          <span key={`h${index}`} className="imaging-gt-calendar-weekday">{day}</span>
        ))}
        {Array.from({ length: firstWeekday }, (_, index) => (
          <span key={`e${index}`} />
        ))}
        {Array.from({ length: days }, (_, index) => {
          const iso = isoDay(month.year, month.month, index + 1);
          const classes = [iso === value ? "is-active" : "", iso === todayIso ? "is-today" : ""].filter(Boolean).join(" ");
          return (
            <button key={iso} type="button" className={classes || undefined} onClick={() => onPick(iso)}>
              {index + 1}
            </button>
          );
        })}
      </div>
    </div>
  );
}

/** Saves this one cell, as Enter does; shown once the value differs from the stored one.
 * ``onMouseDown`` keeps the focus (and the typed text) in the input. */
function CellSaveButton({ label, onSave }: { label: string; onSave: () => void }) {
  return (
    <button
      type="button"
      className="imaging-gt-icon imaging-gt-cell-save"
      aria-label={`Save ${label} ground truth`}
      title="Save (Enter)"
      onMouseDown={(event) => event.preventDefault()}
      onClick={onSave}
    >
      <Save size={12} strokeWidth={2.4} aria-hidden="true" />
    </button>
  );
}

function ManifestDetails({ manifest }: { manifest: ImagingManifestDetails }) {
  // The client's copy: the bar is as wide as its narrower Page Details table.
  const finalOnly = useContext(ClientViewContext);
  return (
    <div className={`imaging-manifest-stack${finalOnly ? " is-final-only" : ""}`}>
      <div className="imaging-manifest-row" role="group" aria-label="Manifest details">
        <span>
          <strong>Member Name:</strong> {fmt(manifest.member)}
        </span>
        <span>
          <strong>DOB:</strong> {fmt(manifest.dob)}
        </span>
        <span>
          <strong>Member ID:</strong> {fmt(manifest.memberId)}
        </span>
      </div>
    </div>
  );
}

function markGlyph(mark: GtBit["mark"]): string {
  if (mark === "match") return "✓";
  if (mark === "partial") return "!";
  if (mark === "mismatch") return "✕";
  return "";
}

function markTitle(mark: GtBit["mark"]): string {
  if (mark === "match") return "Matches ground truth";
  if (mark === "partial") return "Partial match";
  if (mark === "mismatch") return "Does not match ground truth";
  return "Ground truth not compared yet";
}

function GtMarks({ bits }: { bits: GtBit[] }) {
  if (bits.length === 0) return null;
  return (
    <span className="gt-marks">
      {bits.map((bit) => {
        const glyph = markGlyph(bit.mark);
        return (
          <span
            key={`${bit.label}-${bit.mark}`}
            className={`gt-mark gt-${bit.mark}`}
            title={markTitle(bit.mark)}
          >
            {glyph ? `${bit.label} ${glyph}` : bit.label}
          </span>
        );
      })}
    </span>
  );
}

/** A score under its value in Doc Summary; nothing when there is none. */
function ConfLine({ value }: { value: string }) {
  if (value === "NA") return null;
  return <span className="imaging-conf-line">Conf: {value}</span>;
}

function GtCell({ text, bits }: { text: string; bits: GtBit[] }) {
  return (
    <span className="gt-cell">
      <span>{text}</span>
      <GtMarks bits={bits} />
    </span>
  );
}

const MARK_RANK: Record<string, number> = { match: 3, partial: 2, mismatch: 1, unknown: 0 };

/** The better-marked of two scorings of one ground-truth field (Final, Extracted). */
function better(a: GtBit[], b: GtBit[]): GtBit[] {
  if (a.length === 0) return b;
  if (b.length === 0) return a;
  return (MARK_RANK[b[0].mark] ?? 0) > (MARK_RANK[a[0].mark] ?? 0) ? b : a;
}

/** Ground-truth marks for a page: DOS and codability match when either the Final or the
 * Extracted value does (as in the accuracy report). */
function pageBits(page: ImagingPageResult, sections: ImagingSectionsProcessed) {
  const bits = extractedBits(page, sections);
  if (!page.finalDosFrom && !page.finalDosTo && !page.finalCodeable) return bits;
  const finalBits = groundTruthBits({
    ...extractedInput(page, sections),
    dosFrom: page.finalDosFrom || page.dosFrom,
    dosTo: page.finalDosTo || page.finalDosFrom || page.dosTo,
    isCodeable: page.finalCodeable || page.isCodeable,
  });
  return {
    ...bits,
    dosFrom: better(bits.dosFrom, finalBits.dosFrom),
    dosTo: better(bits.dosTo, finalBits.dosTo),
    codeable: better(bits.codeable, finalBits.codeable),
  };
}

function extractedBits(page: ImagingPageResult, sections: ImagingSectionsProcessed) {
  return groundTruthBits(extractedInput(page, sections));
}

function extractedInput(page: ImagingPageResult, sections: ImagingSectionsProcessed) {
  return {
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
    pageType: splitPageType(page.pageType).family,
    pageSubtype: splitPageType(page.pageType).subtype,
    pageTypeKnown: Boolean(sections.junk || sections.codeable),
    isCodeable: page.isCodeable,
    codeableKnown:
      Boolean(sections.codeable) ||
      (page.isCodeable != null && String(page.isCodeable).trim() !== ""),
    actualSequence: page.actualSequence,
    sequenceKnown: Boolean(sections.sequencing),
  };
}

function ValueWithNote({ value, note }: { value: string; note?: string }) {
  if (!note) return value;
  return (
    <span className="value-with-note">
      <span>{value}</span>
      <span className="value-note">{note}</span>
    </span>
  );
}

function sameValue(
  value: string,
  confidence: string,
  extra: Pick<CompareRow, "id" | "label"> & Partial<CompareRow>,
): CompareRow {
  return {
    truth: extra.truth,
    choices: extra.choices,
    presence: extra.presence,
    fixedNa: extra.fixedNa,
    fixedText: extra.fixedText,
    noGroundTruth: extra.noGroundTruth,
    noMark: extra.noMark,
    key: extra.key,
    note: extra.note,
    id: extra.id,
    label: extra.label,
    extracted: value,
    processed: value,
    confidence,
  };
}

function dosCompareRow(
  page: ImagingPageResult,
  side: "from" | "to",
  processed: boolean,
  skipped: boolean,
  truth: GtBit[] | undefined,
): CompareRow {
  const stored = side === "from" ? page.dosFrom : page.dosTo;
  const extracted = extractedDos(page, stored, processed, skipped);
  const confidence = fmtConfidence(
    finalOr(page, "dos", page.dosConfidence),
    processed,
    skipped ? { skipped: true } : undefined,
  );
  return {
    id: side === "from" ? "dos_from" : "dos_to",
    label: side === "from" ? "DOS From" : "DOS To",
    extracted,
    processed: extracted === SKIPPED || extracted === YET_TO_PROCESS
      ? extracted
      : finalDosValue(page, extracted, side, processed, skipped),
    confidence,
    truth,
    key: true,
    // Only when the ground truth gives the page no date at all (From and To blank or NA):
    // a label with only From does not make a To a cross.
    blankIsNone:
      hasGroundTruth(page.groundTruth) &&
      noValue(page.groundTruth?.dosFrom) &&
      noValue(page.groundTruth?.dosTo),
    // A blank / junk page (by the ground truth or the pipeline) has no DOS to score, as in
    // the accuracy report: no tick or cross, even when the ground truth gives a date.
    // A duplicate is scored on its original's DOS.
    noMark:
      (page.isDuplicate !== true && (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes")) ||
      (page.groundTruth ? (blankJunkTruth(page.groundTruth) ?? "").toLowerCase().startsWith("yes") : false),
  };
}

function stagedField(
  extraction: ExtractionReviewResponse | null | undefined,
  id: string,
): { value: string; confidence: number | null } | null {
  if (!extraction?.available) return null;
  const row = extraction.fields.find((field) => field.id === id);
  if (!row?.extracted.trim()) return null;
  return { value: row.extracted, confidence: row.confidence };
}

function PageDetails({
  folderId,
  page,
  sections,
  onSaved,
  groundTruthLeave,
  compact = false,
}: {
  folderId: string;
  page: ImagingPageResult;
  sections: ImagingSectionsProcessed;
  onSaved: (groundTruth: PageGroundTruth) => void;
  compact?: boolean;
  groundTruthLeave?: MutableRefObject<GroundTruthLeave | null>;
}) {
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [verdicts, setVerdicts] = useState<Record<string, "accept" | "enter">>({});
  useEffect(() => {
    setEditingId(null);
    setDraft(null);
    setSaving(false);
    setSaveError(null);
    setEdits({});
    setVerdicts({});
  }, [page.pageNumber, page.fileName]);

  const skipped = isBlankJunkPage(page);
  const skip = skipped ? { skipped: true } : undefined;
  const memberConf = fmtConfidence(page.memberConfidence, sections.member, skip);
  const bits = pageBits(page, sections);
  const encounterKnown =
    Boolean(sections.encounter) ||
    (page.encounterType != null && String(page.encounterType).trim() !== "");
  const pageTypeKnown = Boolean(sections.junk || sections.codeable);
  const pageParts = splitPageType(page.pageType);
  // Final: the document's first-page type; a page outside any document keeps its own.
  const finalParts = splitPageType(page.finalPageType || page.pageType);
  const codeableKnown =
    Boolean(sections.codeable) ||
    (page.isCodeable != null && String(page.isCodeable).trim() !== "");
  const qualityKnown = sections.quality ?? sections.hw;
  // Member and provider rows show the stages' own results (member_extraction_results /
  // the member CSV; imaging_final / provider_signature.csv), never the key/value
  // extractor's raw pick in staging — that stays in the extraction pane.
  const providerCredentials = text(page.providerCredentials) || "";
  // Several signers: one per line, "1. Name" / "2. Name", credentials in the same order;
  // one signer: as before.
  const signers = (page.providerSigners ?? []).filter((signer) => text(signer.name));
  const numbered = (values: string[]) => values.map((value, index) => `${index + 1}. ${value}`).join("\n");
  const providerNameShown =
    signers.length > 1
      ? numbered(signers.map((signer) => properName(signer.name || "")))
      : properName(page.providerName || "") || NOT_FOUND;
  const credentialsShown =
    signers.length > 1
      ? numbered(signers.map((signer) => text(signer.credentials) || NOT_FOUND))
      : providerCredentials || NOT_FOUND;
  const stored = storedGroundTruth(page.groundTruth);
  const values = { ...stored, ...edits };
  const dirty =
    Object.entries(edits).some(([id, value]) => value !== (stored[id] ?? "")) ||
    (editingId != null && draft != null && draft !== (values[editingId] ?? ""));

  function finishReview() {
    setEditingId(null);
    setDraft(null);
  }

  function discardReview() {
    setEdits({});
    setVerdicts({});
    setSaveError(null);
    finishReview();
  }

  async function acceptAll(): Promise<boolean> {
    const merged = { ...stored, ...edits };
    if (editingId && draft != null) {
      const next = normalizeEntry(editingId, draft);
      if (next == null) {
        setSaveError(entryHint(editingId));
        return false;
      }
      merged[editingId] = next;
    }
    const persistedChanged = [...PERSISTED_GT].some(
      (id) => (merged[id] ?? "") !== (stored[id] ?? ""),
    );
    if (!persistedChanged) {
      if (editingId && draft != null && !PERSISTED_GT.has(editingId)) {
        setEdits((current) => ({ ...current, [editingId]: draft }));
      }
      finishReview();
      return true;
    }
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await savePageGroundTruth(folderId, groundTruthBody(page, merged));
      onSaved(saved);
      setEdits((current) => {
        const next = { ...current };
        if (editingId && draft != null) next[editingId] = draft;
        for (const id of PERSISTED_GT) delete next[id];
        return next;
      });
      finishReview();
      return true;
    } catch (err) {
      setSaveError(saveErrorMessage(err));
      return false;
    } finally {
      setSaving(false);
    }
  }

  if (groundTruthLeave) {
    groundTruthLeave.current = {
      dirty,
      accept: acceptAll,
      discard: discardReview,
    };
  }
  useEffect(() => {
    return () => {
      if (groundTruthLeave) groundTruthLeave.current = null;
    };
  }, [groundTruthLeave]);

  // Compact pane: a page nobody has labelled shows Field / Final only.
  const withTruth = Object.entries(page.groundTruth ?? {}).some(
    ([key, value]) =>
      key !== "pageNumber" && key !== "sourcePageId" && value != null && String(value).trim() !== "",
  );
  const sectionProps = {
    compact,
    withTruth,
    values,
    editingId,
    saving,
    verdicts,
    onAccept: (id: string, next: string) => {
      setEditingId(null);
      setDraft(null);
      setSaveError(null);
      setVerdicts((current) => ({ ...current, [id]: "accept" }));
      setEdits((current) => ({ ...current, [id]: next }));
    },
    onCommit: (id: string, next: string) => {
      setEditingId(null);
      setDraft(null);
      setSaveError(null);
      setVerdicts((current) => ({ ...current, [id]: "enter" }));
      setEdits((current) => ({ ...current, [id]: next }));
    },
    onCancel: () => {
      setEditingId(null);
      setDraft(null);
      setSaveError(null);
    },
    onDraft: (value: string) => {
      setDraft(value);
      setSaveError(null);
    },
    onStartEdit: (id: string) => {
      setEditingId(id);
      setDraft(values[id] ?? "");
    },
  };
  const saveBar = dirty ? (
    <span className="imaging-gt-savebar">
      <button
        type="button"
        className="imaging-gt-save"
        aria-label="Save"
        title="Save"
        disabled={saving}
        onClick={() => void acceptAll()}
      >
        <Save size={15} aria-hidden="true" />
        Save
      </button>
      <button
        type="button"
        className="imaging-gt-discard"
        aria-label="Discard"
        title="Discard"
        disabled={saving}
        onClick={discardReview}
      >
        <CircleMinus size={15} aria-hidden="true" />
        Discard
      </button>
    </span>
  ) : null;

  return (
    <div className="imaging-page-details">
      {saveError ? <p className="imaging-gt-error">{saveError}</p> : null}
      <CompareSection
        {...sectionProps}
        title="Member Extraction"
        toolbar={saveBar}
        rows={[
          sameValue(fmt(page.memberName && properName(page.memberName), sections.member, skip), memberConf, {
            id: "name",
            label: "Member Name",
            truth: bits.memberName,
            presence: true,
            key: true,
          }),
          sameValue(displayIsoDates(fmt(page.memberDob, sections.member, skip)), memberConf, {
            id: "dob",
            label: "Member DOB",
            truth: bits.memberDob,
            presence: true,
            key: true,
          }),
          sameValue(fmt(page.memberId, sections.member, skip), memberConf, {
            id: "member_id",
            label: "Member ID",
            presence: true,
          }),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Page Quality & Orientation"
        rows={[
          sameValue(fmtHandwriting(pageTypeOf(page), sections.hw), fmtConfidence(page.handwrittenOrPrintedConfidence ?? null, sections.hw), {
            id: "handwriting",
            label: "Type (Printed/HW)",
            choices: TYPE_CHOICES,
          }),
          sameValue(fmtHandwrittenArea(page.handwrittenAreaPct, sections.hw), fmtConfidence(page.measuredConfidences?.handwritten_area, sections.hw), {
            id: "handwritten_area",
            label: "Handwritten %",
            choices: HW_AREA_CHOICES,
          }),
          sameValue(fmtVisibility(page.isVisible, sections.hw), fmtConfidence(page.measuredConfidences?.visibility, sections.hw), {
            id: "visibility",
            label: "Visibility",
            choices: VISIBILITY_CHOICES,
          }),
          sameValue(fmtQualityTag(page.pageQualityTag, qualityKnown), fmtConfidence(page.pageQualityConfidence, qualityKnown), {
            id: "quality",
            label: "Quality",
            choices: QUALITY_CHOICES,
            // A placeholder grade today (CLAUDE.md section 3): labelled, not scored.
            noMark: true,
          }),
          sameValue(fmtDegrees(page.orientationAngle, sections.rotation), fmtConfidence(page.measuredConfidences?.orientation, sections.rotation), {
            id: "orientation",
            label: "Text Orientation",
            truth: bits.rotation,
          }),
          sameValue(fmtDegrees(page.tiltAngle, sections.rotation), fmtConfidence(page.measuredConfidences?.tilt, sections.rotation), {
            id: "tilt",
            label: "Tilt (Text)",
            noMark: true,
          }),
          sameValue(fmt(page.mirrored, sections.rotation), fmtConfidence(page.measuredConfidences?.mirrored, sections.rotation), {
            id: "mirrored",
            label: "Mirrored (Text)",
            choices: YES_NO_CHOICES,
          }),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Encounter Info"
        rows={[
          sameValue(fmt(page.encounterType, encounterKnown, skip), fmtConfidence(finalOr(page, "encounter_type", page.encounterConfidence), Boolean(sections.encounter), skip), {
            id: "encounter",
            label: "Encounter Type",
            choices: ENCOUNTER_TYPE_CHOICES,
          }),
          dosCompareRow(page, "from", sections.dos, skipped, bits.dosFrom),
          dosCompareRow(page, "to", sections.dos, skipped, bits.dosTo),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Page Classification"
        rows={[
          (() => {
            const stored = fmtBlankOrJunk(page.blankOrJunk, sections.junk);
            const row = sameValue(fmtBlankOrJunk(page.blankOrJunk, sections.junk, page), fmtConfidence(finalOr(page, "blank_junk", page.blankJunkConfidence), sections.junk), {
              id: "blank_junk",
              label: "Is Blank or Junk?",
              truth: bits.blankJunk,
              choices: BLANK_JUNK_CHOICES,
              key: true,
            });
            // Scored on the stored verdict, as the accuracy report does.
            return { ...row, compare: blankJunkCompare(stored, stored) };
          })(),
          sameValue(fmtDuplicate(page.isDuplicate, page.pageTypeConfidence, sections.junk, page.duplicateOf, page.isPartialDuplicate), fmtConfidence(duplicateDisplayConfidence(page.isDuplicate, page.pageTypeConfidence, page.isPartialDuplicate, page.duplicateConfidence), sections.junk), {
            id: "duplicate",
            label: "Is Duplicate",
            choices: DUPLICATE_CHOICES,
          }),
          {
            ...sameValue(fmtPageType(pageParts.family, pageTypeKnown), fmtConfidence(finalOr(page, "page_type", page.pageTypeConfidence), pageTypeKnown), {
              id: "page_type",
              label: "Page Type",
              truth: bits.pageType,
              choices: PAGE_L1_CHOICES,
              key: true,
            }),
            processed: fmtPageType(finalParts.family, pageTypeKnown),
            // Our L1 (an older client label still scores, gtPageTypeCanon.json), with
            // the accuracy report's rules: the better of Final and Extracted. Page
            // Subtype lists the L2s of the L1 picked, not saved yet; Specialty has no
            // entry yet (2026-10-26, user).
            compare: (truth: string) => {
              if (!truth.trim() || !pageTypeKnown) return null;
              const final = pageTypeVerdict(truth, finalParts.family, finalParts.subtype, page.pageTypeSemantic);
              const own = pageTypeVerdict(truth, pageParts.family, pageParts.subtype, page.pageTypeSemantic);
              if (final === "match" || own === "match") return "match";
              if (final === "partial" || own === "partial") return "partial";
              return final === "mismatch" || own === "mismatch" ? "mismatch" : null;
            },
          },
          {
            ...sameValue(fmtPageType(pageParts.subtype, pageTypeKnown), fmtConfidence(page.subtypeConfidence, pageTypeKnown), {
              id: "page_subtype",
              label: "Page Subtype",
              choices: PAGE_L2_ALL,
              noMark: true,
            }),
            processed: fmtPageType(finalParts.subtype, pageTypeKnown),
            l2Of: "page_type",
          },
          {
            ...sameValue(fmt(page.specialty, pageTypeKnown), fmtConfidence(finalOr(page, "specialty", page.specialtyConfidence), pageTypeKnown), {
              id: "specialty",
              label: "Specialty",
              choices: SPECIALTY_CHOICES,
              noMark: true,
            }),
            processed: fmt(page.finalSpecialty ?? page.specialty, pageTypeKnown),
          },
          {
            ...sameValue(fmtCodeable(page.isCodeable, page.pageType, codeableKnown), fmtConfidence(finalOr(page, "codability", null), Boolean(sections.codeable)), {
              id: "codeable",
              label: "Is Codable Or Non-Codable",
              truth: bits.codeable,
              choices: CODEABLE_CHOICES,
            }),
            ...(page.finalCodeable ? { processed: displayCodeable(page.finalCodeable) } : {}),
          },
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Provider Extraction"
        rows={[
          sameValue(
            providerNameShown,
            fmtConfidence(page.providerSignatureConfidence, Boolean(page.providerName)),
            { id: "provider_name", label: "Provider Name" },
          ),
          {
            // The credentials on the provider the signature rows name.
            ...sameValue(
              credentialsShown,
              fmtConfidence(page.providerSignatureConfidence, Boolean(providerCredentials)),
              { id: "provider_credentials", label: "Provider Credentials", key: true },
            ),
            // Scored as the accuracy report scores it: any one credential shared
            // ('MD | NP' against 'MD, PhD') is a match.
            compare: (truth: string) => {
              const verdict = credentialsVerdict(truth, providerCredentials || undefined);
              return verdict == null ? null : verdict === "no" ? "mismatch" : "match";
            },
          },
          sameValue(
            yesNo(page.providerSignature) === "yes" ? "Yes" : yesNo(page.providerSignature) === "no" ? "No" : NOT_FOUND,
            fmtConfidence(page.providerSignatureConfidence, yesNo(page.providerSignature) != null),
            {
              id: "electronic_signature",
              label: "Provider Signature",
              presence: true,
            },
          ),
        ]}
      />
    </div>
  );
}

function RejectionRulesTable({
  rows,
  verificationProcessed,
}: {
  rows: ImagingVerificationDetails[];
  verificationProcessed: boolean;
}) {
  if (rows.length === 0) {
    return (
      <div className="ocr-empty">
        {verificationProcessed
          ? "No member verification summary for this chart."
          : YET_TO_PROCESS}
      </div>
    );
  }

  return (
    <table className="imaging-summary-table imaging-rejection-table">
      <thead>
        <tr>
          <th scope="col">Component</th>
          <th scope="col">Status</th>
          <th scope="col">Matched Name</th>
          <th scope="col">Pages</th>
          <th scope="col">Conf.</th>
          <th scope="col">Decision Reason</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((v, idx) => (
          <tr key={`${v.finalStatus ?? "row"}-${idx}`}>
            <td>Member Verification</td>
            <td>{fmt(v.finalStatus, verificationProcessed)}</td>
            <td>{fmt(v.matchedName, verificationProcessed)}</td>
            <td>{fmtPagesMatched(v, verificationProcessed)}</td>
            <td>{fmtConfidence(v.matchedConfidence, verificationProcessed)}</td>
            <td className="imaging-decision-reason">
              {fmt(v.decisionReason, verificationProcessed)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

type DocumentRow = {
  seq: number;
  startPage: number;
  endPage: number;
  pageType: string | null;
  encounterType: string | null;
  dosFrom: string | null;
  dosTo: string | null;
  /** Every page is junk: a junk group (shares the document numbering), no visit. */
  junk: boolean;
};

/** One row per document (continuity stage), in page order. Its type and DOS are the
 * document's Final values. Its encounter type is the staged encounter's
 * (continuity.json → encounters) when the chart has them, else the first one its
 * pages carry. */
function documentRows(pages: ImagingPageResult[], encounters: StagedEncounter[] = []): DocumentRow[] {
  const staged = new Map<number, StagedEncounter>();
  for (const encounter of encounters) {
    if (encounter.document != null && !staged.has(encounter.document)) staged.set(encounter.document, encounter);
  }
  const byDocument = new Map<number, ImagingPageResult[]>();
  for (const page of pages) {
    if (page.documentSeq == null) continue;
    const members = byDocument.get(page.documentSeq) ?? [];
    members.push(page);
    byDocument.set(page.documentSeq, members);
  }
  const rows: DocumentRow[] = [];
  for (const [seq, members] of byDocument) {
    members.sort((a, b) => a.pageNumber - b.pageNumber);
    const first = members[0];
    const text = (value: string | null | undefined) => (value ?? "").trim() || null;
    rows.push({
      seq,
      startPage: first.pageNumber,
      endPage: members[members.length - 1].pageNumber,
      pageType: text(first.finalPageType) ?? text(first.pageType),
      encounterType:
        text(staged.get(seq)?.encounter_label)
        ?? members.map((page) => text(page.encounterType)).find(Boolean)
        ?? null,
      dosFrom: text(first.finalDosFrom),
      dosTo: text(first.finalDosTo),
      junk: members.every((page) => (page.blankOrJunk ?? "").trim().toLowerCase().startsWith("yes (junk)")),
    });
  }
  return rows.sort((a, b) => a.startPage - b.startPage);
}

/** An encounter's DOS From / DOS To: a one-day visit has To = From. */
function fmtDocumentDos(from: string | null, to: string | null, side: "from" | "to"): string {
  const value = side === "from" ? from ?? to : to ?? from;
  return value ? displayIsoDates(value) : NOT_FOUND;
}

function DocumentsTable({
  pages,
  sections,
  encounters,
}: {
  pages: ImagingPageResult[];
  sections: ImagingSectionsProcessed;
  encounters: StagedEncounter[];
}) {
  const rows = useMemo(() => documentRows(pages, encounters), [pages, encounters]);
  if (rows.length === 0) {
    return (
      <div className="ocr-empty">
        {pages.some((page) => page.documentPosition != null)
          ? "No encounters in this chart: every page is blank, junk or a duplicate."
          : "Encounters appear once the continuity stage has run on this chart."}
      </div>
    );
  }
  const typeKnown = Boolean(sections.junk || sections.codeable);
  const encounterKnown = Boolean(sections.encounter) || rows.some((row) => row.encounterType);
  return (
    <table className="imaging-summary-table imaging-documents-table">
      <thead>
        <tr>
          <th scope="col">Encounter #</th>
          <th scope="col">Start Page</th>
          <th scope="col">End Page</th>
          <th scope="col">Page Type</th>
          <th scope="col">Encounter Type</th>
          <th scope="col">DOS From</th>
          <th scope="col">DOS To</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.seq}>
            <td>{row.junk ? `Junk ${row.seq}` : row.seq}</td>
            <td>{row.startPage}</td>
            <td>{row.endPage}</td>
            <td>{row.junk ? "Junk" : fmtPageType(row.pageType, typeKnown)}</td>
            {/* A junk group is no visit: no encounter type and no DOS to find. */}
            <td>{row.junk ? SKIPPED : fmt(row.encounterType, encounterKnown)}</td>
            {(["from", "to"] as const).map((side) => (
              <td key={side}>
                {row.junk ? SKIPPED : sections.dos ? fmtDocumentDos(row.dosFrom, row.dosTo, side) : YET_TO_PROCESS}
              </td>
            ))}
          </tr>
        ))}
      </tbody>
    </table>
  );
}

/** Encounter tab: the Algorithm encounters and the ground truth, one subtab each. The ground
 * truth stays mounted while hidden so an unsaved edit survives a switch. */
function EncounterIdentification({
  folderId,
  pages,
  sections,
  encounters,
}: {
  folderId: string;
  pages: ImagingPageResult[];
  sections: ImagingSectionsProcessed;
  encounters: StagedEncounter[];
}) {
  const [view, setView] = useState<"algo" | "truth">("algo");
  const seed = useMemo(() => encounterSeed(pages, encounters), [pages, encounters]);
  const pageCount = Math.max(0, ...pages.map((page) => page.pageNumber));
  return (
    <div className="section-coords-panel">
      <div className="section-coords-title">Encounter Identification</div>
      <div className="output-tabs imaging-doc-tabs" role="tablist" aria-label="Encounter view">
        <button
          type="button"
          role="tab"
          aria-selected={view === "algo"}
          className={view === "algo" ? "active" : ""}
          onClick={() => setView("algo")}
        >
          Algorithm
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "truth"}
          className={view === "truth" ? "active" : ""}
          onClick={() => setView("truth")}
        >
          Ground Truth
        </button>
      </div>
      <div hidden={view !== "algo"}>
        <DocumentsTable pages={pages} sections={sections} encounters={encounters} />
      </div>
      <div hidden={view !== "truth"}>
        <EncounterReviewEditor folderId={folderId} seed={seed} pageCount={pageCount} />
      </div>
    </div>
  );
}

/** The pipeline's encounters as the reviewer's starting rows. */
function encounterSeed(pages: ImagingPageResult[], encounters: StagedEncounter[]): EncounterSeed[] {
  return documentRows(pages, encounters).map((row) => ({
    startPage: row.startPage,
    endPage: row.endPage,
    pageType: row.junk ? "Junk" : row.pageType,
    encounterType: row.junk ? null : row.encounterType,
    dosFrom: row.junk ? null : row.dosFrom,
    dosTo: row.junk ? null : (row.dosTo ?? row.dosFrom),
  }));
}

function DocSummary({
  pages,
  verifications,
  sections,
}: {
  pages: ImagingPageResult[];
  verifications: ImagingVerificationDetails[];
  sections: ImagingSectionsProcessed;
}) {
  const [view, setView] = useState<DocView>("values");
  // Each page's own DOS, else the default 2222-02-02; never the previous page's.
  const rows = useMemo(() => withPageDos(pages, sections.dos), [pages, sections.dos]);

  return (
    <div className="imaging-doc-summary">
      <div className="output-tabs imaging-doc-tabs" role="tablist" aria-label="Doc summary view">
        <button
          type="button"
          role="tab"
          aria-selected={view === "values"}
          className={view === "values" ? "active" : ""}
          onClick={() => setView("values")}
        >
          Values
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === "rejection"}
          className={view === "rejection" ? "active" : ""}
          onClick={() => setView("rejection")}
        >
          Rejection Rules
        </button>
      </div>

      {view === "rejection" ? (
        <RejectionRulesTable
          rows={verifications}
          verificationProcessed={sections.verification}
        />
      ) : (
        <table className="imaging-summary-table imaging-doc-table">
          <thead>
            <tr>
              <th scope="col">Page #</th>
              <th scope="col">File</th>
              <th scope="col">Extracted Name</th>
              <th scope="col">Extracted DOB</th>
              <th scope="col">Member ID</th>
              <th scope="col">Type (Printed/HW)</th>
              <th scope="col">Handwritten %</th>
              <th scope="col">Visibility</th>
              <th scope="col">Quality</th>
              <th scope="col" title="How far the text sits rotated counter-clockwise from upright, as scanned">Orient.</th>
              <th scope="col">Tilt</th>
              <th scope="col">Mirrored</th>
              <th scope="col">DOS From</th>
              <th scope="col">DOS To</th>
              <th scope="col">Is Blank or Junk?</th>
              <th scope="col">Duplicate</th>
              <th scope="col">Page Type</th>
              <th scope="col">Page Subtype</th>
              <th scope="col">Codable / Non-Codable</th>
              <th scope="col">Current Sequence</th>
              <th scope="col">Actual Sequence</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const skip = isBlankJunkPage(p) ? { skipped: true } : undefined;
              const bits = pageBits(p, sections);
              const pageParts = splitPageType(p.pageType);
              const typeKnown = Boolean(sections.junk || sections.codeable);
              return (
              <tr key={`${p.pageNumber}-${p.fileName}`}>
                <td>{p.pageNumber}</td>
                <td className="imaging-mono">{p.fileName}</td>
                <td>
                  <GtCell text={fmt(p.memberName && properName(p.memberName), sections.member, skip)} bits={bits.memberName} />
                  <ConfLine value={fmtConfidence(p.memberConfidence, sections.member, skip)} />
                </td>
                <td>
                  <GtCell text={displayIsoDates(fmt(p.memberDob, sections.member, skip))} bits={bits.memberDob} />
                </td>
                <td>{fmt(p.memberId, sections.member, skip)}</td>
                <td>{fmtHandwriting(pageTypeOf(p), sections.hw)}</td>
                <td>{fmtHandwrittenArea(p.handwrittenAreaPct, sections.hw)}</td>
                <td>{fmtVisibility(p.isVisible, sections.hw)}</td>
                <td>
                  {fmtQualityTag(
                    p.pageQualityTag,
                    sections.quality ?? sections.hw,
                  )}
                  <ConfLine value={fmtConfidence(p.pageQualityConfidence, sections.hw || sections.rotation)} />
                </td>
                <td>
                  <GtCell
                    text={fmtDegrees(p.orientationAngle, sections.rotation)}
                    bits={bits.rotation}
                  />
                </td>
                <td>{fmtDegrees(p.tiltAngle, sections.rotation)}</td>
                <td>{fmt(p.mirrored, sections.rotation)}</td>
                <td>
                  <GtCell text={displayIsoDates(fmt(p.dosFrom, sections.dos, skip))} bits={bits.dosFrom} />
                  <ConfLine value={fmtConfidence(p.dosConfidence, sections.dos, skip)} />
                </td>
                <td>
                  <GtCell text={displayIsoDates(fmt(p.dosTo, sections.dos, skip))} bits={bits.dosTo} />
                </td>
                <td>
                  <GtCell
                    text={fmtBlankOrJunk(p.blankOrJunk, sections.junk, p)}
                    bits={bits.blankJunk}
                  />
                </td>
                <td>
                  <ValueWithNote
                    value={fmtDuplicate(p.isDuplicate, p.pageTypeConfidence, sections.junk, p.duplicateOf, p.isPartialDuplicate)}
                  />
                </td>
                <td>
                  <GtCell
                    text={fmtPageType(pageParts.family, typeKnown)}
                    bits={bits.pageType}
                  />
                  <ConfLine
                    value={fmtConfidence(
                      p.isDuplicate
                        ? duplicateDisplayConfidence(p.isDuplicate, p.pageTypeConfidence, p.isPartialDuplicate, p.duplicateConfidence)
                        : p.pageTypeConfidence,
                      sections.junk,
                    )}
                  />
                </td>
                <td>
                  <GtCell
                    text={fmtPageType(pageParts.subtype, typeKnown)}
                    bits={bits.pageType}
                  />
                </td>
                <td>
                  <GtCell
                    text={fmtCodeable(
                      p.isCodeable,
                      p.pageType,
                      Boolean(sections.codeable) ||
                        (p.isCodeable != null && String(p.isCodeable).trim() !== ""),
                    )}
                    bits={bits.codeable}
                  />
                </td>
                <td>{fmt(p.currentSequence ?? p.pageNumber, true)}</td>
                <td>
                  <GtCell
                    text={fmt(p.actualSequence, Boolean(sections.sequencing), skip)}
                    bits={bits.pageSequence}
                  />
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export default function ImagingPanel({
  tab,
  loading,
  error,
  document,
  shellManifest = null,
  currentPage,
  currentFileName,
  sectionHeadersSkipped = false,
  sectionHeadersLoading = false,
  extraction = null,
  headerHighlight = null,
  onHeaderHighlight,
  onGroundTruthSaved,
  groundTruthLeave,
  compact = false,
}: Props) {
  if (tab === "additional") {
    return (
      <div className="imaging-panel-stack">
        <ExtractedList
          extraction={extraction}
          page={currentPage}
          skipped={sectionHeadersSkipped}
          loading={sectionHeadersLoading}
          headerHighlight={headerHighlight}
          onHeaderHighlight={onHeaderHighlight}
        />
      </div>
    );
  }

  const emptyManifest: ImagingManifestDetails = {
    member: null,
    dob: null,
    memberId: null,
  };
  const shell = shellManifest ?? emptyManifest;

  if (loading) {
    return (
      <div className="imaging-panel-stack" aria-busy="true">
        <ManifestDetails manifest={shell} />
        <div className="ocr-loading imaging-loading">
          <span className="imaging-loading-spinner" aria-hidden="true" />
          Loading imaging results…
        </div>
        {tab === "page" ? (
          <div className="imaging-page-details imaging-page-details-skeleton" aria-hidden="true">
            {Array.from({ length: 6 }, (_, i) => (
              <div key={i} className="imaging-skeleton-row">
                <span className="imaging-skeleton-label" />
                <span className="imaging-skeleton-value" />
              </div>
            ))}
          </div>
        ) : null}
      </div>
    );
  }
  if (error) {
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={shell} />
        <div className="ocr-empty">{error}</div>
      </div>
    );
  }
  if (!document || document.pages.length === 0) {
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={shell} />
        <div className="ocr-empty">No imaging results for this chart.</div>
      </div>
    );
  }

  const manifest = document.manifest ?? shell;
  const sections = document.sectionsProcessed ?? DEFAULT_SECTIONS;
  const verifications =
    document.verifications && document.verifications.length > 0
      ? document.verifications
      : document.verification
        ? [document.verification]
        : [];

  if (tab === "sequencing") {
    const seqRows = [...document.pages].sort((a, b) => a.pageNumber - b.pageNumber);
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={manifest} />
        <SequencingTable
          folderId={document.folder_id}
          pages={seqRows}
          sections={sections}
          onSaved={(page, groundTruth) =>
            onGroundTruthSaved?.(page.pageNumber, page.fileName, groundTruth)
          }
        />
      </div>
    );
  }

  if (tab === "encounter") {
    // One row per encounter (continuity document); staged encounters first. The
    // reviewer's encounters below, starting from a copy of these.
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={manifest} />
        <EncounterIdentification
          key={document.folder_id}
          folderId={document.folder_id}
          pages={document.pages}
          sections={sections}
          encounters={document.encounters ?? []}
        />
      </div>
    );
  }

  if (tab === "doc") {
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={manifest} />
        <DocSummary
          pages={document.pages}
          verifications={verifications}
          sections={sections}
        />
      </div>
    );
  }

  if (!currentPage) {
    return (
      <div className="imaging-panel-stack">
        <ManifestDetails manifest={manifest} />
        <div className="ocr-empty">
          {currentFileName
            ? `No imaging row for ${currentFileName}.`
            : "Select a page to view imaging details."}
        </div>
      </div>
    );
  }

  return (
    <div className="imaging-panel-stack">
      {compact ? null : <ManifestDetails manifest={manifest} />}
      <PageDetails
        folderId={document.folder_id}
        page={currentPage}
        sections={sections}
        compact={compact}
        groundTruthLeave={groundTruthLeave}
        onSaved={(groundTruth) =>
          onGroundTruthSaved?.(currentPage.pageNumber, currentPage.fileName, groundTruth)
        }
      />
    </div>
  );
}

const SECTION_SKIP_MESSAGE = "Skipped for Junk/Blank";

/** Whole-number sequence of a Main page, or null ("J3" and anything else: null). */
function sequenceNumber(value: string | number | null | undefined): number | null {
  const text = normalizeSequence(value);
  return /^[1-9]\d*$/.test(text) ? Number(text) : null;
}

/** A sequence as it is compared and saved: "3", "3.0" and "03" are "3"; "j 2" is "J2".
 * Anything else is returned trimmed, so a bad value shows up. */
function normalizeSequence(value: string | number | null | undefined): string {
  const text = String(value ?? "").trim();
  const junk = /^j\s*0*([1-9]\d*)$/i.exec(text);
  if (junk) return `J${junk[1]}`;
  if (/^\d+(\.0+)?$/.test(text) && Number(text) >= 1) return String(Math.trunc(Number(text)));
  return text;
}

/** Two series: Main pages 1, 2, 3 …; blank / junk pages J1, J2, J3 …. */
const SEQUENCE_VALUE = /^J?[1-9]\d*$/;

/** A page in the J series: blank, junk, or a complete duplicate (≥ 98%; 2026-10-25,
 * user: duplicates are junk for sequencing too). */
function isJunkSequencePage(page: ImagingPageResult): boolean {
  return isBlankJunkPage(page) || isCompleteDuplicate(page.isDuplicate, page.pageTypeConfidence);
}

/** The Algo. Sequence of every page, in page order: Main pages (partial duplicates
 * included) keep the sequencing stage's number; blank / junk pages and complete
 * duplicates are J1, J2 … in page order. */
function algoSequences(
  pages: ImagingPageResult[],
  processed: boolean,
): Map<number, string> {
  const out = new Map<number, string>();
  let junk = 0;
  for (const page of [...pages].sort((a, b) => a.pageNumber - b.pageNumber)) {
    if (isJunkSequencePage(page)) out.set(page.pageNumber, `J${(junk += 1)}`);
    else out.set(page.pageNumber, fmt(page.actualSequence, processed));
  }
  return out;
}

/** Original order as ground truth: Main pages renumbered 1, 2, 3 … and blank / junk
 * pages and complete duplicates J1, J2 … in page order. */
function originalSequences(pages: ImagingPageResult[]): Map<number, string> {
  const out = new Map<number, string>();
  let main = 0;
  let junk = 0;
  const ordered = [...pages].sort(
    (a, b) => (a.currentSequence ?? a.pageNumber) - (b.currentSequence ?? b.pageNumber),
  );
  for (const page of ordered) {
    if (isJunkSequencePage(page)) out.set(page.pageNumber, `J${(junk += 1)}`);
    else out.set(page.pageNumber, String((main += 1)));
  }
  return out;
}

/** The ground-truth sequence as shown, edited and compared. The client's sheet numbers
 * every page 1, 2, 3 …, junk included, so a page whose ground truth is blank, junk or an
 * invoice (blank_page / junk_page / is_invoice) becomes J1, J2 … and the Main pages are renumbered 1, 2, 3 …,
 * each in stored order. A chart already in that form comes back unchanged. */
function groundTruthSequences(pages: ImagingPageResult[]): Map<number, string> {
  const out = new Map<number, string>();
  const main: [number, number][] = [];
  const junk: [number, number][] = [];
  for (const page of pages) {
    const value = normalizeSequence(page.groundTruth?.pageSequence);
    const j = /^J([1-9]\d*)$/.exec(value);
    const n = j ? Number(j[1]) : sequenceNumber(value);
    if (n == null) {
      out.set(page.pageNumber, value);
      continue;
    }
    const gt = page.groundTruth;
    const isJunk =
      Boolean(j) ||
      yesNo(gt?.blankPage) === "yes" ||
      yesNo(gt?.junkPage) === "yes" ||
      yesNo(gt?.isInvoice) === "yes";
    (isJunk ? junk : main).push([n, page.pageNumber]);
  }
  for (const [list, prefix] of [[main, ""], [junk, "J"]] as const) {
    [...list]
      .sort((a, b) => a[0] - b[0] || a[1] - b[1])
      .forEach(([, pageNumber], i) => out.set(pageNumber, `${prefix}${i + 1}`));
  }
  return out;
}

/** The ground-truth sequences after one page's entry, renumbered (2026-10-26, user):
 * entering 2 puts the page 2nd in its series (Main 1, 2, 3 … or J1, J2 …) and the
 * pages from 2 on move down one; clearing a page closes the gap (the rest move up one);
 * moving a page between Main and J renumbers both. A value that is not a sequence
 * leaves every other page as it is. */
function reflowSequences(
  draft: Record<number, string>,
  pageNumber: number,
  entered: string,
): Record<number, string> {
  const value = normalizeSequence(entered);
  if (value !== "" && !SEQUENCE_VALUE.test(value)) return { ...draft, [pageNumber]: entered };
  const series: Record<"" | "J", [number, number][]> = { "": [], J: [] };
  for (const [key, raw] of Object.entries(draft)) {
    const page = Number(key);
    const current = normalizeSequence(raw);
    if (page === pageNumber || !SEQUENCE_VALUE.test(current)) continue;
    const prefix = current.startsWith("J") ? "J" : "";
    series[prefix].push([Number(current.slice(prefix.length)), page]);
  }
  const out: Record<number, string> = { ...draft, [pageNumber]: value };
  for (const prefix of ["", "J"] as const) {
    const ordered = series[prefix].sort((a, b) => a[0] - b[0] || a[1] - b[1]).map(([, page]) => page);
    if (value && (value.startsWith("J") ? "J" : "") === prefix) {
      const at = Math.min(Number(value.slice(prefix.length)), ordered.length + 1) - 1;
      ordered.splice(at, 0, pageNumber);
    }
    ordered.forEach((page, i) => {
      out[page] = `${prefix}${i + 1}`;
    });
  }
  return out;
}

/** What is wrong with one series of sequences: each must run 1, 2, 3 … with no gap or
 * repeat. ``prefix`` is "" for Main pages, "J" for blank / junk. */
function seriesProblem(values: string[], prefix: "" | "J"): string | null {
  const numbers = values
    .filter((value) => (prefix ? value.startsWith("J") : /^\d/.test(value)))
    .map((value) => Number(value.slice(prefix.length)))
    .sort((a, b) => a - b);
  const repeated = numbers.find((n, i) => i > 0 && numbers[i - 1] === n);
  if (repeated != null) return `Sequence ${prefix}${repeated} is used more than once.`;
  const gap = numbers.findIndex((n, i) => n !== i + 1);
  if (gap >= 0) {
    return `The ${prefix ? "J" : "Main"} sequence must run ${prefix}1–${prefix}${numbers.length} with no gaps: ${prefix}${gap + 1} is missing.`;
  }
  return null;
}

/** Breaks in the Algo. Sequence against the ground truth, Main pages only. Pages with
 * both numbers are put in Algo. order; a page breaks when it is not the next page in
 * ground-truth order after the one before it (the first page: not the first in
 * ground-truth order). */
function sequenceBreaks(
  pages: ImagingPageResult[],
  truth: (page: ImagingPageResult) => number | null,
): { breaks: Set<number>; compared: number } {
  const both = pages.filter(
    (p) => !isJunkSequencePage(p) && sequenceNumber(p.actualSequence) != null && truth(p) != null,
  );
  const rank = new Map(
    [...both]
      .sort((a, b) => truth(a)! - truth(b)! || a.pageNumber - b.pageNumber)
      .map((p, i) => [p.pageNumber, i]),
  );
  const algo = [...both].sort(
    (a, b) => sequenceNumber(a.actualSequence)! - sequenceNumber(b.actualSequence)! || a.pageNumber - b.pageNumber,
  );
  const breaks = new Set<number>();
  algo.forEach((p, i) => {
    const expected = i === 0 ? 0 : rank.get(algo[i - 1].pageNumber)! + 1;
    if (rank.get(p.pageNumber) !== expected) breaks.add(p.pageNumber);
  });
  return { breaks, compared: both.length };
}

/** Page sequencing with its ground truth (page_ground_truth.page_sequence, text: "3" for
 * a Main page, "J2" for a blank / junk one; see groundTruthSequences). Edit turns the
 * ground-truth column into inputs; Save writes every page whose stored value differs.
 * Outside editing, each ground truth is marked: a Main page by breaks against the Algo.
 * Sequence, a J page as correct when the Algo. also has it blank / junk. */
function SequencingTable({
  folderId,
  pages,
  sections,
  onSaved,
}: {
  folderId: string;
  pages: ImagingPageResult[];
  sections: ImagingSectionsProcessed;
  onSaved: (page: ImagingPageResult, groundTruth: PageGroundTruth) => void;
}) {
  const sequencingProcessed = Boolean(sections.sequencing);
  const stored = (p: ImagingPageResult) => normalizeSequence(p.groundTruth?.pageSequence);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState<Record<number, string>>({});
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const algo = useMemo(() => algoSequences(pages, sequencingProcessed), [pages, sequencingProcessed]);
  const truth = useMemo(() => groundTruthSequences(pages), [pages]);
  const shown = (p: ImagingPageResult) => truth.get(p.pageNumber) ?? "";

  const startEdit = () => {
    setDraft(Object.fromEntries(pages.map((p) => [p.pageNumber, shown(p)])));
    setError(null);
    setEditing(true);
  };
  const drafted = (p: ImagingPageResult) => normalizeSequence(draft[p.pageNumber]);
  const changed = editing ? pages.filter((p) => drafted(p) !== stored(p)) : [];
  const invalid = changed.find((p) => drafted(p) !== "" && !SEQUENCE_VALUE.test(drafted(p)));
  // A complete duplicate copies as blank; blank / junk pages as J1, J2 ….
  const fill = (from: "algo" | "original") => {
    const original = originalSequences(pages);
    setDraft(
      Object.fromEntries(
        pages.map((p) => {
          if (from === "original") return [p.pageNumber, original.get(p.pageNumber) ?? ""];
          const value = algo.get(p.pageNumber) ?? "";
          return [p.pageNumber, SEQUENCE_VALUE.test(value) ? value : (draft[p.pageNumber] ?? "")];
        }),
      ),
    );
  };
  // Main pages must run 1, 2, 3 … and blank / junk pages J1, J2, J3 …, no gap or repeat.
  const continuity = (() => {
    if (!editing || invalid) return null;
    const values = pages.map(drafted).filter(Boolean);
    return seriesProblem(values, "") ?? seriesProblem(values, "J");
  })();
  const { breaks } = sequenceBreaks(pages, (p) => sequenceNumber(shown(p)));
  /** A J page agrees when the Algo. also has it blank / junk; a Main page by its breaks. */
  const verdict = (p: ImagingPageResult): "match" | "break" | "mismatch" | null => {
    const value = shown(p);
    if (editing || !SEQUENCE_VALUE.test(value)) return null;
    const gtJunk = value.startsWith("J");
    if (gtJunk !== isJunkSequencePage(p)) return "mismatch";
    if (gtJunk) return "match";
    if (sequenceNumber(p.actualSequence) == null) return null;
    return breaks.has(p.pageNumber) ? "break" : "match";
  };
  // Accuracy = 1 − breaks / Main pages marked: Main against Main only. A J page, or a
  // page that is J on one side and Main on the other, is marked but not counted (that is
  // a blank / junk error, scored there).
  const marks = editing
    ? []
    : pages
        .filter((p) => !shown(p).startsWith("J") && !isJunkSequencePage(p))
        .map(verdict)
        .filter((mark) => mark != null);
  const wrong = marks.filter((mark) => mark !== "match").length;
  // The chart as a whole (2026-10-27, user): its sequence matches (1) only with no break;
  // Break Rate = breaks / (pages − 1), the joins between Main pages.
  const sequenceMatch = wrong === 0 ? 1 : 0;
  const breakRate = marks.length > 1 ? Math.min(1, wrong / (marks.length - 1)) : wrong ? 1 : 0;
  const firstBreak = editing
    ? null
    : [...pages]
        .filter((p) => verdict(p) === "break")
        .sort((a, b) => (sequenceNumber(a.actualSequence) ?? 0) - (sequenceNumber(b.actualSequence) ?? 0))[0] ?? null;

  async function save() {
    if (invalid || continuity || changed.length === 0) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await savePageSequences(
        folderId,
        changed.map((p) => ({
          pageNumber: p.pageNumber,
          sourcePageId: p.fileName,
          pageSequence: drafted(p) || null,
        })),
      );
      const byNumber = new Map(saved.map((gt) => [gt.pageNumber, gt]));
      for (const page of changed) {
        const gt = byNumber.get(page.pageNumber);
        if (gt) onSaved(page, gt);
      }
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not save the page sequence");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="section-coords-panel">
      <div className="encounter-review-head">
        <div className="section-coords-title">Page Sequencing</div>
        <span className="encounter-review-bar">
          {editing ? (
            <>
              <button type="button" className="imaging-gt-discard" onClick={() => fill("original")} disabled={saving}>
                Copy Original
              </button>
              <button type="button" className="imaging-gt-discard" onClick={() => fill("algo")} disabled={saving}>
                Copy Algo.
              </button>
              <button type="button" className="imaging-gt-discard" onClick={() => setEditing(false)} disabled={saving}>
                <CircleMinus size={15} aria-hidden="true" />
                Discard
              </button>
              <button
                type="button"
                className="imaging-gt-save"
                onClick={() => void save()}
                disabled={saving || changed.length === 0 || Boolean(invalid) || Boolean(continuity)}
              >
                <Save size={15} aria-hidden="true" />
                {saving ? "Saving…" : `Save${changed.length ? ` (${changed.length})` : ""}`}
              </button>
            </>
          ) : (
            <button type="button" className="imaging-gt-discard" onClick={startEdit} title="Edit the ground truth">
              <Pencil size={14} aria-hidden="true" />
              Edit
            </button>
          )}
        </span>
      </div>
      {!sequencingProcessed ? (
        <p className="section-coords-empty">
          Sequencing has not run for this chart yet — Algo. Sequence stays Yet
          to Process until the page_sequencing stage writes results.
        </p>
      ) : null}
      {marks.length > 0 ? (
        <div className="sequence-break-summary">
          <p>
            Sequence:{" "}
            {sequenceMatch ? (
              <span className="cmp-match">In order ✓</span>
            ) : (
              <span className="cmp-mismatch">
                Broken sequence ✗{firstBreak ? ` (first break at page ${firstBreak.pageNumber})` : ""}
              </span>
            )}
            {" · "}Sequence Match = <strong>{sequenceMatch}</strong>
          </p>
          <p>
            Accuracy ={" "}
            <span className={wrong ? "cmp-mismatch" : "cmp-match"}>
              {(100 * (1 - wrong / marks.length)).toFixed(1)}%
            </span>{" "}
            ({wrong} {wrong === 1 ? "break" : "breaks"} / {marks.length} {marks.length === 1 ? "page" : "pages"})
            {" · "}Break Rate ={" "}
            <span className={wrong ? "cmp-mismatch" : "cmp-match"}>{(100 * breakRate).toFixed(1)}%</span>{" "}
            ({wrong} / ({marks.length} pages − 1))
          </p>
        </div>
      ) : null}
      {invalid ? (
        <div className="encounter-review-problem">
          Page {invalid.pageNumber}: a sequence is a whole number from 1 (Main) or J1, J2 … (blank / junk).
        </div>
      ) : null}
      {continuity ? <div className="encounter-review-problem">{continuity}</div> : null}
      {error ? <div className="error-banner">{error}</div> : null}
      <table className="imaging-summary-table imaging-sequencing-table">
        <thead>
          <tr>
            <th scope="col">S.No</th>
            <th scope="col">File</th>
            <th scope="col">Original Sequence</th>
            <th scope="col">Algo. Sequence</th>
            <th scope="col">Ground Truth</th>
          </tr>
        </thead>
        <tbody>
          {pages.map((p, index) => {
            const mark = verdict(p);
            return (
            <tr key={`seq-${p.pageNumber}-${p.fileName}`}>
              <td>{index + 1}</td>
              <td>{p.fileName}</td>
              <td>{fmt(p.currentSequence ?? p.pageNumber, true)}</td>
              <td>{algo.get(p.pageNumber)}</td>
              <td>
                {editing ? (
                  <input
                    className="encounter-review-input encounter-review-page"
                    type="text"
                    inputMode="text"
                    value={draft[p.pageNumber] ?? ""}
                    aria-label={`Ground-truth sequence of page ${p.pageNumber}`}
                    onChange={(e) =>
                      setDraft((values) => ({ ...values, [p.pageNumber]: e.target.value }))
                    }
                    // Enter renumbers the rest of the series around this entry.
                    onKeyDown={(e) => {
                      if (e.key !== "Enter") return;
                      e.preventDefault();
                      const entered = e.currentTarget.value;
                      setDraft((values) => reflowSequences(values, p.pageNumber, entered));
                    }}
                  />
                ) : (
                  <>
                    {shown(p)}
                    {mark === "match" ? <span className="cmp-match sequence-break-mark"> ✓</span> : null}
                    {mark === "break" ? <span className="cmp-mismatch sequence-break-mark"> ✗ Break</span> : null}
                    {mark === "mismatch" ? (
                      <span
                        className="cmp-mismatch sequence-break-mark"
                        title={isJunkSequencePage(p) ? "Blank / junk in the Algo., Main in the ground truth" : "Main in the Algo., blank / junk in the ground truth"}
                      >
                        {" "}✗ {isJunkSequencePage(p) ? "Junk in Algo." : "Main in Algo."}
                      </span>
                    ) : null}
                  </>
                )}
              </td>
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/** Extra classes for a header chip or box: a heading has none; a rejected candidate is
 *  heading-like (a little lighter) or plain (lightest). */
export function headerTier(header: OcrSectionHeader | undefined): string {
  if (!header?.candidate) return "";
  return header.heading_like ? " is-candidate is-heading-like" : " is-candidate";
}

/** Section headers shown under Additional: every Heron candidate, the ones not accepted
 *  marked ``candidate`` (shown lighter). OCR "Show Headers" uses this list only. */
export function additionalSectionHeaders(
  extraction: ExtractionReviewResponse | null | undefined,
): { names: string[]; boxes: OcrSectionHeader[] } {
  const boxes = (extraction?.section_headers ?? []).filter(
    (header) => header.text.trim() && header.width > 0 && header.height > 0,
  );
  if (boxes.length > 0) {
    return { names: boxes.map((header) => header.text.trim()), boxes };
  }
  const names = (stagedField(extraction, "heading_heron")?.value ?? "")
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean);
  return { names, boxes: [] };
}

function ExtractedList({
  extraction,
  page,
  skipped,
  loading,
  headerHighlight,
  onHeaderHighlight,
}: {
  extraction: ExtractionReviewResponse | null | undefined;
  page: ImagingPageResult | null;
  skipped: boolean;
  loading: boolean;
  headerHighlight: "all" | number | null;
  onHeaderHighlight?: (next: "all" | number | null) => void;
}) {
  const pageNo = stagedField(extraction, "page_no");
  const { names, boxes } = additionalSectionHeaders(extraction);
  const canDraw = boxes.length > 0 && Boolean(onHeaderHighlight);

  return (
    <div className="section-coords-panel">
      <div className="section-coords-title">Extracted</div>
      {loading ? (
        <p className="section-coords-empty">Loading extracted fields…</p>
      ) : (
        <div className="section-coords-list">
          <div className="section-coords-row">
            <span className="section-coords-text">Page Numbers</span>
            <span>{pageNo?.value || "Not Found"}</span>
          </div>
          <div className="section-coords-row">
            <span className="section-coords-text">Continuation</span>
            <span>{page ? fmtDocument(page) : "—"}</span>
          </div>
          <div className="section-coords-row section-headers-row">
            <div className="section-headers-label">
              <span className="section-coords-text">Section Headers</span>
              {canDraw ? (
                <label className="header-highlight-all">
                  <input
                    type="checkbox"
                    checked={headerHighlight === "all"}
                    onChange={() =>
                      onHeaderHighlight?.(headerHighlight === "all" ? null : "all")
                    }
                  />
                  Highlight all
                </label>
              ) : null}
            </div>
            {skipped ? (
              <span>{SECTION_SKIP_MESSAGE}</span>
            ) : names.length === 0 ? (
              <span>Not Found</span>
            ) : (
              <span className="extracted-header-list">
                {names.map((text, index) =>
                  canDraw ? (
                    <button
                      key={`${text}-${index}`}
                      type="button"
                      className={`header-highlight-one${headerTier(boxes[index])}`}
                      title={boxes[index]?.candidate ? "Candidate: not accepted as a heading" : undefined}
                      aria-pressed={
                        headerHighlight === "all" || headerHighlight === index
                      }
                      onClick={() =>
                        onHeaderHighlight?.(headerHighlight === index ? null : index)
                      }
                    >
                      {text}
                    </button>
                  ) : (
                    <span key={`${text}-${index}`} className="header-highlight-one">
                      {text}
                    </span>
                  ),
                )}
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

export type { ImagingTab };
