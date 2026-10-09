import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Ban, Save } from "lucide-react";
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
} from "./api";
import {
  duplicateDisplayConfidence,
  formatDuplicateLabel,
} from "./duplicateLabel";
import { displayCodeable, displayIsoDates, groundTruthBits, type GtBit } from "./groundTruth";
import { PAGE_SUBTYPE_CHOICES, PAGE_TYPE_CHOICES } from "./reviewChoices";

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

/** Doc Summary fallback when a page has no DOS and nothing to inherit. */
const DEFAULT_DOS = "2022-02-02";
/** Hardcoded default DOS confidence */
const DEFAULT_DOS_CONFIDENCE = 0.8;

function hasDos(value: string | null | undefined): value is string {
  return value != null && String(value).trim() !== "";
}

/**
 * Doc Summary only: missing DOS inherits the previous page's DOS;
 * if nothing precedes, use 2022-02-02 at 80% confidence.
 * Skipped when DOS section was never run for this folder.
 */
function fillDosForward(
  pages: ImagingPageResult[],
  dosProcessed: boolean,
): ImagingPageResult[] {
  if (!dosProcessed) return pages;

  let prevFrom: string | null = null;
  let prevTo: string | null = null;
  let prevConf: number | null = null;

  return [...pages]
    .sort((a, b) => a.pageNumber - b.pageNumber)
    .map((page) => {
      let dosFrom = hasDos(page.dosFrom) ? displayIsoDates(page.dosFrom.trim()) : null;
      let dosTo = hasDos(page.dosTo) ? displayIsoDates(page.dosTo.trim()) : null;
      let dosConfidence = page.dosConfidence ?? null;
      let usedDefault = false;

      if (!dosFrom && !dosTo) {
        if (prevFrom) {
          dosFrom = prevFrom;
          dosTo = prevTo ?? prevFrom;
          dosConfidence = prevConf;
        } else {
          dosFrom = DEFAULT_DOS;
          dosTo = DEFAULT_DOS;
          dosConfidence = DEFAULT_DOS_CONFIDENCE;
          usedDefault = true;
        }
      } else {
        if (!dosFrom) dosFrom = dosTo ?? prevFrom ?? DEFAULT_DOS;
        if (!dosTo) dosTo = dosFrom ?? prevTo ?? DEFAULT_DOS;
        if (
          (dosFrom === DEFAULT_DOS || dosTo === DEFAULT_DOS) &&
          dosConfidence == null
        ) {
          dosConfidence = DEFAULT_DOS_CONFIDENCE;
          usedDefault = true;
        }
      }

      prevFrom = dosFrom;
      prevTo = dosTo;
      prevConf = usedDefault
        ? DEFAULT_DOS_CONFIDENCE
        : (dosConfidence ?? prevConf);
      return { ...page, dosFrom, dosTo, dosConfidence };
    });
}

type ImagingTab = "page" | "sequencing" | "doc" | "additional";
type DocView = "values" | "confidence" | "rejection";

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

/** The date found on this page. A legacy span row copied the encounter date in. */
function extractedDos(
  page: ImagingPageResult,
  staged: string | null | undefined,
  stored: string | null | undefined,
  processed: boolean,
  skipped: boolean,
): string {
  const stagedDate = (staged || "").trim();
  if (stagedDate) return displayIsoDates(stagedDate);
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
  if (!isContinuation(page)) return extracted;
  const date = spanDate(page, side, processed, skipped);
  if (date === NOT_FOUND || date === SKIPPED || date === YET_TO_PROCESS) return extracted;
  return date;
}

/** Blank / junk / duplicate — downstream member/DOS/encounter metrics were skipped. */
function isBlankJunkPage(page: ImagingPageResult): boolean {
  const bj = (page.blankOrJunk || "").trim().toLowerCase();
  if (bj.startsWith("yes")) return true;
  if (page.isDuplicate === true) return true;
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

function fmtBlankOrJunk(
  value: string | boolean | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  if (typeof value === "boolean") return value ? "Yes (Junk)" : "No";
  return String(value);
}

function fmtDuplicate(
  isDuplicate: boolean | null | undefined,
  confidence: number | null | undefined,
  processed = true,
): string {
  return formatDuplicateLabel(isDuplicate, confidence, processed, {
    yetToProcess: YET_TO_PROCESS,
    notFound: NOT_FOUND,
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

function fmtPercent(value: number | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return NOT_MEASURED;
  return `${value.toFixed(1)}%`;
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

function fmtPageType(value: string | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  return String(value);
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
};

const ENCOUNTER_CHOICES = ["Outpatient (F2F)", "Outpatient (Tele)", "Inpatient", "Home"] as const;
const BLANK_JUNK_CHOICES = ["Yes (Blank)", "Yes (Junk)", "No"] as const;
const DUPLICATE_CHOICES = ["Yes", "May Be", "No"] as const;
const CODEABLE_CHOICES = ["Codeable", "Non Codeable", "Discharge"] as const;
const TYPE_CHOICES = ["Printed", "Handwritten", "Form", "Visual", "Blank"] as const;

/** Page type; a page classified before document type existed shows its old label. */
function pageTypeOf(page: ImagingPageResult): string | null {
  return page.documentType || page.handwrittenOrPrinted || null;
}
const QUALITY_CHOICES = ["High", "Medium", "Low"] as const;
const YES_NO_CHOICES = ["Yes", "No"] as const;

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

function cleanGt(value: string | null | undefined): string | null {
  const text = (value ?? "").trim();
  if (!text || text.toLowerCase() === "na" || text.toLowerCase() === "n/a") return null;
  return text;
}

function yesNo(value: string | null | undefined): "yes" | "no" | null {
  const folded = (value ?? "").trim().toLowerCase();
  if (folded === "yes" || folded === "y" || folded === "true") return "yes";
  if (folded === "no" || folded === "n" || folded === "false") return "no";
  return null;
}

function visibilityLabel(value: string | null | undefined): string {
  const side = yesNo(value);
  if (side === "yes") return "Good Visibility";
  if (side === "no") return "Bad Visibility";
  return (value ?? "").trim();
}

function visibilityStored(shown: string): string | null {
  const text = cleanGt(shown);
  if (!text) return null;
  if (text.toLowerCase() === "good visibility") return "Yes";
  if (text.toLowerCase() === "bad visibility") return "No";
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

/** Values already stored for this page. Empty string displays as NA. */
function storedGroundTruth(gt: PageGroundTruth | null | undefined): Record<string, string> {
  const pageType = (gt?.pageType ?? "").trim();
  const encounter = (gt?.encounterType ?? "").trim();
  return {
    name: presenceStored(gt?.memberName),
    dob: presenceStored(gt?.memberDob),
    member_id: presenceStored(gt?.memberId),
    provider_name: (gt?.renderingProvider ?? "").trim(),
    electronic_signature: presenceStored(gt?.providerSignature),
    quality: visibilityLabel(gt?.isVisible),
    orientation: rotationLabel(gt?.rotation),
    encounter: pageType ? encounter : "",
    dos_from: (gt?.dosFrom ?? "").trim(),
    dos_to: (gt?.dosTo ?? "").trim(),
    blank_junk: blankJunkLabel(gt),
    page_type: pageType || encounter,
    codeable: (gt?.codeable ?? "").trim(),
  };
}

function groundTruthBody(
  page: ImagingPageResult,
  values: Record<string, string>,
): PageGroundTruth {
  const pageType = cleanGt(values.page_type);
  const encounter = cleanGt(values.encounter);
  return {
    pageNumber: page.pageNumber,
    sourcePageId: page.fileName,
    memberName: presenceStored(values.name) || null,
    memberDob: presenceStored(values.dob) || null,
    memberId: presenceStored(values.member_id) || null,
    renderingProvider: cleanGt(values.provider_name),
    providerSignature: presenceStored(values.electronic_signature) || null,
    isVisible: visibilityStored(values.quality ?? ""),
    rotation: cleanGt((values.orientation ?? "").replace(/°$/, "")),
    encounterType: encounter,
    dosFrom: cleanGt(values.dos_from),
    dosTo: cleanGt(values.dos_to),
    ...blankJunkStored(values.blank_junk ?? ""),
    pageType,
    codeable: cleanGt(values.codeable),
    pageSequence: page.groundTruth?.pageSequence ?? null,
  };
}

function confirmedValue(processed: string): string {
  const text = processed.trim();
  if (!text || text === YET_TO_PROCESS || text === SKIPPED || text === NOT_FOUND) return "";
  return text;
}

const UNCOMPARED = new Set(["", "na", "n/a", "not found", "not available", "yet to process", "skipped"]);

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

/** Tick when the entered ground truth matches Final; X when both sides differ.
    Yes/No fields tick when Yes agrees with a found value, or No agrees with none. */
function groundTruthMark(
  processed: string,
  truth: string,
  presence = false,
): "match" | "mismatch" | null {
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

function MatchMark({
  processed,
  value,
  presence = false,
}: {
  processed: string;
  value: string;
  presence?: boolean;
}) {
  const mark = groundTruthMark(processed, value, presence);
  if (!mark) return null;
  const tick = mark === "match";
  return (
    <span className={`imaging-gt-mark gt-${mark}`} aria-label={tick ? "Matches" : "Does not match"}>
      {tick ? "✓" : "✕"}
    </span>
  );
}

function CompareSection({
  title,
  rows,
  editing,
  editingSection,
  values,
  gtChoice,
  onEdit,
  onChoose,
  onCommit,
  onStartEdit,
  onSave,
  onCancel,
  saving,
  headerAction,
}: {
  title: string;
  rows: CompareRow[];
  /** True when this section's ground truth cells are open. */
  editing: boolean;
  /** "all" opens every section. A title opens only that section. */
  editingSection: string | null;
  values: Record<string, string>;
  gtChoice: Record<string, "correct" | "enter">;
  onEdit: (id: string, value: string) => void;
  onChoose: (id: string, choice: "correct" | "enter", value: string) => void;
  onCommit: (id: string) => void;
  onStartEdit: () => void;
  onSave: () => void;
  onCancel: (fieldIds: string[]) => void;
  saving: boolean;
  headerAction?: ReactNode;
}) {
  const sectionEdit = editing && editingSection === title;
  return (
    <section className="imaging-section">
      <div className="imaging-section-head">
        <h3 className="imaging-section-title">{title}</h3>
        {headerAction}
      </div>
      <table className="imaging-detail-table imaging-value-table">
        <colgroup>
          <col className="col-field" />
          <col className="col-extracted" />
          <col className="col-processed" />
          <col className="col-confidence" />
          <col className="col-gap" />
          <col className="col-gt" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Extracted</th>
            <th scope="col">Final</th>
            <th scope="col">Confidence</th>
            <th className="gt-gap" aria-hidden="true" />
            <th scope="col" className="gt-shade">
              <span className="imaging-gt-head">
                <span>Ground Truth</span>
                {sectionEdit ? (
                  <span className="imaging-gt-actions">
                    <button
                      type="button"
                      className="imaging-gt-icon"
                      aria-label="Save ground truth"
                      onClick={onSave}
                      disabled={saving}
                    >
                      <Save size={13} strokeWidth={2.25} aria-hidden="true" />
                    </button>
                    <button
                      type="button"
                      className="imaging-gt-icon imaging-gt-cancel"
                      aria-label="Cancel ground truth"
                      onClick={() => onCancel(rows.map((row) => row.id))}
                      disabled={saving}
                    >
                      <Ban size={13} strokeWidth={2} aria-hidden="true" />
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="imaging-gt-edit"
                    onClick={onStartEdit}
                    disabled={Boolean(editingSection) && editingSection !== "all"}
                  >
                    Edit
                  </button>
                )}
              </span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.id}>
              <th scope="row">{row.label}</th>
              <td>{row.extracted}</td>
              <td>{row.processed}</td>
              <td>{row.confidence}</td>
              <td className="gt-gap" aria-hidden="true" />
              <td className="gt-shade">
                <GroundTruthCell
                  row={row}
                  editing={editing}
                  choice={gtChoice[row.id]}
                  value={values[row.id] ?? ""}
                  onEdit={(value) => onEdit(row.id, value)}
                  onCommit={() => onCommit(row.id)}
                  onChoose={(choice) =>
                    onChoose(
                      row.id,
                      choice,
                      choice === "correct" ? confirmedValue(row.processed) : values[row.id] ?? "",
                    )
                  }
                />
              </td>
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
  choice,
  value,
  onEdit,
  onCommit,
  onChoose,
}: {
  row: CompareRow;
  editing: boolean;
  choice?: "correct" | "enter";
  value: string;
  onEdit: (value: string) => void;
  onCommit: () => void;
  onChoose: (choice: "correct" | "enter") => void;
}) {
  const commitOnEnter = (event: { key: string; preventDefault: () => void }) => {
    if (event.key === "Enter") {
      event.preventDefault();
      onCommit();
    }
  };
  if (row.presence) {
    const shown = presenceStored(value);
    if (!editing) {
      return (
        <span className="imaging-gt-slot">
          {shown || "NA"}
          <MatchMark processed={row.processed} value={shown} presence />
        </span>
      );
    }
    return (
      <span className="imaging-gt-slot">
        <select
          aria-label={`${row.label} ground truth`}
          value={shown}
          autoFocus={row.id === "name"}
          onChange={(event) => onEdit(event.target.value)}
          onKeyDown={commitOnEnter}
        >
          <option value="">NA</option>
          <option value="Yes">Yes</option>
          <option value="No">No</option>
        </select>
        <MatchMark processed={row.processed} value={shown} presence />
      </span>
    );
  }
  const saved = (
    <span className="imaging-gt-slot">
      {value.trim() ? value : "NA"}
      <MatchMark processed={row.processed} value={value} />
    </span>
  );
  if (!editing) return saved;
  const opened = Boolean(value.trim()) || choice === "enter" || choice === "correct";
  if (!opened) {
    return (
      <span className="imaging-gt-choice">
        <button type="button" aria-label={`${row.label} correct`} onClick={() => onChoose("correct")}>
          ✓ Correct
        </button>
        <button type="button" aria-label={`${row.label} enter value`} onClick={() => onChoose("enter")}>
          ✕ Enter Value
        </button>
      </span>
    );
  }
  const mark = <MatchMark processed={row.processed} value={value} />;
  if (row.choices && row.choices.length > 5) {
    return (
      <LongChoiceField row={row} value={value} onEdit={onEdit} onCommit={onCommit} mark={mark} />
    );
  }
  if (row.choices) {
    const options = value && !row.choices.includes(value) ? [value, ...row.choices] : row.choices;
    return (
      <span className="imaging-gt-slot">
        <select
          aria-label={`${row.label} ground truth`}
          value={value}
          autoFocus={choice === "enter" || choice === "correct"}
          onChange={(event) => onEdit(event.target.value)}
          onKeyDown={commitOnEnter}
        >
          <option value="">NA</option>
          {options.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
        {mark}
      </span>
    );
  }
  return (
    <span className="imaging-gt-slot">
      <input
        aria-label={`${row.label} ground truth`}
        value={value}
        placeholder="NA"
        autoFocus={choice === "enter" || choice === "correct"}
        onChange={(event) => onEdit(event.target.value)}
        onKeyDown={commitOnEnter}
      />
      {mark}
    </span>
  );
}

function LongChoiceField({
  row,
  value,
  onEdit,
  onCommit,
  mark,
}: {
  row: CompareRow;
  value: string;
  onEdit: (value: string) => void;
  onCommit: () => void;
  mark: ReactNode;
}) {
  const choices = row.choices ?? [];
  const known = choices.includes(value);
  const [other, setOther] = useState(Boolean(value) && !known);
  const listId = `gt-choices-${row.id}`;
  if (other) {
    return (
      <span className="imaging-gt-slot">
        <input
          aria-label={`${row.label} ground truth`}
          value={value}
          placeholder="Other"
          autoFocus
          onChange={(event) => onEdit(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") {
              event.preventDefault();
              onCommit();
            }
          }}
        />
        {mark}
      </span>
    );
  }
  return (
    <span className="imaging-gt-slot">
      <input
        aria-label={`${row.label} ground truth`}
        list={listId}
        value={value}
        placeholder="Type to find"
        onChange={(event) => {
          if (event.target.value === "Others") {
            setOther(true);
            onEdit("");
            return;
          }
          onEdit(event.target.value);
        }}
        onKeyDown={(event) => {
          if (event.key === "Enter") {
            event.preventDefault();
            onCommit();
          }
        }}
      />
      {mark}
      <datalist id={listId}>
        {choices.map((choice) => (
          <option key={choice} value={choice} />
        ))}
        <option value="Others" />
      </datalist>
    </span>
  );
}

function ManifestDetails({ manifest }: { manifest: ImagingManifestDetails }) {
  return (
    <div className="imaging-manifest-stack">
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

function GtCell({ text, bits }: { text: string; bits: GtBit[] }) {
  return (
    <span className="gt-cell">
      <span>{text}</span>
      <GtMarks bits={bits} />
    </span>
  );
}

function pageBits(page: ImagingPageResult, sections: ImagingSectionsProcessed) {
  return groundTruthBits({
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
  });
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
    id: extra.id,
    label: extra.label,
    extracted: value,
    processed: value,
    confidence,
  };
}

function dosCompareRow(
  page: ImagingPageResult,
  dos: { value: string; confidence: number | null } | null,
  side: "from" | "to",
  processed: boolean,
  skipped: boolean,
  truth: GtBit[] | undefined,
): CompareRow {
  const parts = (dos?.value || "").split(" – ");
  const staged = dos ? (side === "from" ? parts[0] : (parts[1] ?? parts[0])) : null;
  const stored = side === "from" ? page.dosFrom : page.dosTo;
  const extracted = extractedDos(page, staged, stored, processed, skipped);
  const confidence = fmtConfidence(dos?.confidence ?? page.dosConfidence, processed, skipped);
  return {
    id: side === "from" ? "dos_from" : "dos_to",
    label: side === "from" ? "DOS From" : "DOS To",
    extracted,
    processed: extracted === SKIPPED || extracted === YET_TO_PROCESS
      ? extracted
      : finalDosValue(page, extracted, side, processed, skipped),
    confidence,
    truth,
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
  extraction,
  onSaved,
}: {
  folderId: string;
  page: ImagingPageResult;
  sections: ImagingSectionsProcessed;
  extraction: ExtractionReviewResponse | null;
  onSaved: (groundTruth: PageGroundTruth) => void;
}) {
  const [editingSection, setEditingSection] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [edits, setEdits] = useState<Record<string, string>>({});
  const [gtChoice, setGtChoice] = useState<Record<string, "correct" | "enter">>({});
  useEffect(() => {
    setEditingSection(null);
    setSaving(false);
    setSaveError(null);
    setEdits({});
    setGtChoice({});
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
  const codeableKnown =
    Boolean(sections.codeable) ||
    (page.isCodeable != null && String(page.isCodeable).trim() !== "");
  const qualityKnown = sections.quality ?? sections.hw;
  const name = stagedField(extraction, "name");
  const dob = stagedField(extraction, "dob");
  const memberId = stagedField(extraction, "member_id");
  const dos = stagedField(extraction, "dos");
  const values = { ...storedGroundTruth(page.groundTruth), ...edits };
  const sectionProps = {
    values,
    editingSection,
    gtChoice,
    onEdit: (id: string, value: string) => {
      setEdits((current) => ({ ...current, [id]: value }));
    },
    onChoose: (id: string, choice: "correct" | "enter", value: string) => {
      setGtChoice((current) => ({ ...current, [id]: choice }));
      if (choice === "correct") setEdits((current) => ({ ...current, [id]: value }));
    },
    onCommit: (id: string) => {
      void saveGroundTruth(id);
    },
    saving,
    onSave: () => {
      void saveGroundTruth();
    },
    onCancel: (fieldIds: string[]) => {
      setEdits((current) => {
        const next = { ...current };
        for (const id of fieldIds) delete next[id];
        return next;
      });
      setGtChoice((current) => {
        const next = { ...current };
        for (const id of fieldIds) delete next[id];
        return next;
      });
      setEditingSection(null);
      setSaveError(null);
    },
  };
  async function saveGroundTruth(fieldId?: string) {
    setSaving(true);
    setSaveError(null);
    try {
      const saved = await savePageGroundTruth(folderId, groundTruthBody(page, values));
      onSaved(saved);
      setEdits({});
      if (!fieldId) {
        setEditingSection(null);
        setGtChoice({});
      }
    } catch (err) {
      setSaveError(saveErrorMessage(err));
    } finally {
      setSaving(false);
    }
  }
  const reviewButton =
    editingSection === "all" ? (
      <span className="imaging-review-actions">
        <button
          type="button"
          className="imaging-review-btn"
          aria-label="Save ground truth"
          disabled={saving}
          onClick={() => {
            void saveGroundTruth();
          }}
        >
          <Save size={16} strokeWidth={2.25} aria-hidden="true" />
        </button>
        <button
          type="button"
          className="imaging-review-btn"
          aria-label="Cancel ground truth"
          disabled={saving}
          onClick={() => {
            setEdits({});
            setGtChoice({});
            setEditingSection(null);
            setSaveError(null);
          }}
        >
          <Ban size={16} strokeWidth={2} aria-hidden="true" />
        </button>
      </span>
    ) : (
      <button
        type="button"
        className="imaging-review-btn"
        aria-label="Review"
        disabled={saving || editingSection != null}
        onClick={() => setEditingSection("all")}
      >
        Review
      </button>
    );

  return (
    <div className="imaging-page-details">
      {saveError ? <p className="imaging-gt-error">{saveError}</p> : null}
      <CompareSection
        {...sectionProps}
        title="Member Extraction"
        editing={editingSection === "all" || editingSection === "Member Extraction"}
        onStartEdit={() => setEditingSection("Member Extraction")}
        headerAction={reviewButton}
        rows={[
          sameValue(name?.value ?? fmt(page.memberName, sections.member, skip), name ? fmtConfidence(name.confidence, true) : memberConf, {
            id: "name",
            label: "Member Name",
            truth: bits.memberName,
            presence: true,
          }),
          sameValue(displayIsoDates(dob?.value ?? fmt(page.memberDob, sections.member, skip)), dob ? fmtConfidence(dob.confidence, true) : memberConf, {
            id: "dob",
            label: "Member DOB",
            truth: bits.memberDob,
            presence: true,
          }),
          sameValue(memberId?.value ?? fmt(page.memberId, sections.member, skip), memberId ? fmtConfidence(memberId.confidence, true) : memberConf, {
            id: "member_id",
            label: "Member ID",
            presence: true,
          }),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Page Quality & Orientation"
        editing={editingSection === "all" || editingSection === "Page Quality & Orientation"}
        onStartEdit={() => setEditingSection("Page Quality & Orientation")}
        rows={[
          sameValue(fmtHandwriting(pageTypeOf(page), sections.hw), fmtConfidence(page.handwrittenOrPrintedConfidence ?? null, sections.hw), {
            id: "handwriting",
            label: "Type (Printed/HW)",
            choices: TYPE_CHOICES,
          }),
          sameValue(fmtPercent(page.handwrittenAreaPct, sections.hw), fmtConfidence(null, sections.hw), {
            id: "handwritten_area",
            label: "Handwritten %",
          }),
          sameValue(fmtVisibility(page.isVisible, sections.hw), fmtConfidence(null, sections.hw), {
            id: "visibility",
            label: "Visibility",
          }),
          sameValue(fmtQualityTag(page.pageQualityTag, qualityKnown), fmtConfidence(page.pageQualityConfidence, qualityKnown), {
            id: "quality",
            label: "Quality",
            truth: bits.quality,
            choices: QUALITY_CHOICES,
          }),
          sameValue(fmtDegrees(page.orientationAngle, sections.rotation), fmtConfidence(null, sections.rotation), {
            id: "orientation",
            label: "Orientation Angle (Page)",
            truth: bits.rotation,
          }),
          sameValue(fmtDegrees(page.tiltAngle, sections.rotation), fmtConfidence(null, sections.rotation), {
            id: "tilt",
            label: "Tilt (Text)",
          }),
          sameValue(fmt(page.mirrored, sections.rotation), fmtConfidence(null, sections.rotation), {
            id: "mirrored",
            label: "Mirrored (Text)",
            choices: YES_NO_CHOICES,
          }),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Encounter Info"
        editing={editingSection === "all" || editingSection === "Encounter Info"}
        onStartEdit={() => setEditingSection("Encounter Info")}
        rows={[
          sameValue(fmt(page.encounterType, encounterKnown, skip), fmtConfidence(null, Boolean(sections.encounter), skip), {
            id: "encounter",
            label: "Encounter Type",
            choices: ENCOUNTER_CHOICES,
          }),
          dosCompareRow(page, dos, "from", sections.dos, skip, bits.dosFrom),
          dosCompareRow(page, dos, "to", sections.dos, skip, bits.dosTo),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Page Classification"
        editing={editingSection === "all" || editingSection === "Page Classification"}
        onStartEdit={() => setEditingSection("Page Classification")}
        rows={[
          sameValue(fmtBlankOrJunk(page.blankOrJunk, sections.junk), fmtConfidence(page.pageTypeConfidence, sections.junk), {
            id: "blank_junk",
            label: "Is Blank or Junk?",
            truth: bits.blankJunk,
            choices: BLANK_JUNK_CHOICES,
          }),
          sameValue(fmtDuplicate(page.isDuplicate, page.pageTypeConfidence, sections.junk), fmtConfidence(duplicateDisplayConfidence(page.isDuplicate, page.pageTypeConfidence), sections.junk), {
            id: "duplicate",
            label: "Is Duplicate",
            choices: DUPLICATE_CHOICES,
          }),
          sameValue(fmtPageType(pageParts.family, pageTypeKnown), fmtConfidence(page.pageTypeConfidence, pageTypeKnown), {
            id: "page_type",
            label: "Page Type",
            truth: bits.pageType,
            choices: PAGE_TYPE_CHOICES,
          }),
          sameValue(fmtPageType(pageParts.subtype, pageTypeKnown), fmtConfidence(null, pageTypeKnown), {
            id: "page_subtype",
            label: "Page Subtype",
            choices: PAGE_SUBTYPE_CHOICES,
          }),
          sameValue(fmtCodeable(page.isCodeable, page.pageType, codeableKnown), fmtConfidence(null, Boolean(sections.codeable)), {
            id: "codeable",
            label: "Is Codeable Or Non Codeable",
            truth: bits.codeable,
            choices: CODEABLE_CHOICES,
          }),
        ]}
      />
      <CompareSection
        {...sectionProps}
        title="Provider Extraction"
        editing={editingSection === "all" || editingSection === "Provider Extraction"}
        onStartEdit={() => setEditingSection("Provider Extraction")}
        rows={[
          sameValue(page.providerName || NOT_FOUND, fmtConfidence(page.providerSignatureConfidence, Boolean(page.providerName)), {
            id: "provider_name",
            label: "Provider Name",
          }),
          sameValue(page.providerCredentials || NOT_FOUND, fmtConfidence(page.providerSignatureConfidence, Boolean(page.providerCredentials)), {
            id: "provider_credentials",
            label: "Provider Credentials",
          }),
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
  const rows = useMemo(
    () => fillDosForward(pages, sections.dos),
    [pages, sections.dos],
  );
  const showConfidence = view === "confidence";

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
          aria-selected={view === "confidence"}
          className={view === "confidence" ? "active" : ""}
          onClick={() => setView("confidence")}
        >
          With Confidence
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
        <table className="imaging-summary-table">
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
              <th scope="col">Orient.</th>
              <th scope="col">Tilt</th>
              <th scope="col">Mirrored</th>
              <th scope="col">DOS From</th>
              <th scope="col">DOS To</th>
              <th scope="col">Is Blank or Junk?</th>
              <th scope="col">Duplicate</th>
              <th scope="col">Page Type</th>
              <th scope="col">Page Subtype</th>
              <th scope="col">Codeable / Non Codeable</th>
              <th scope="col">Current Sequence</th>
              <th scope="col">Actual Sequence</th>
              {showConfidence ? (
                <>
                  <th scope="col">Member Conf.</th>
                  <th scope="col">Quality Conf.</th>
                  <th scope="col">DOS Conf.</th>
                  <th scope="col">Type Conf.</th>
                </>
              ) : null}
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
                  <GtCell text={fmt(p.memberName, sections.member, skip)} bits={bits.memberName} />
                </td>
                <td>
                  <GtCell text={displayIsoDates(fmt(p.memberDob, sections.member, skip))} bits={bits.memberDob} />
                </td>
                <td>{fmt(p.memberId, sections.member, skip)}</td>
                <td>{fmtHandwriting(pageTypeOf(p), sections.hw)}</td>
                <td>{fmtPercent(p.handwrittenAreaPct, sections.hw)}</td>
                <td>{fmtVisibility(p.isVisible, sections.hw)}</td>
                <td>
                  {fmtQualityTag(
                    p.pageQualityTag,
                    sections.quality ?? sections.hw,
                  )}
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
                </td>
                <td>
                  <GtCell text={displayIsoDates(fmt(p.dosTo, sections.dos, skip))} bits={bits.dosTo} />
                </td>
                <td>
                  <GtCell
                    text={fmtBlankOrJunk(p.blankOrJunk, sections.junk)}
                    bits={bits.blankJunk}
                  />
                </td>
                <td>
                  {fmtDuplicate(
                    p.isDuplicate,
                    p.pageTypeConfidence,
                    sections.junk,
                  )}
                </td>
                <td>
                  <GtCell
                    text={fmtPageType(pageParts.family, typeKnown)}
                    bits={bits.pageType}
                  />
                </td>
                <td>{fmtPageType(pageParts.subtype, typeKnown)}</td>
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
                {showConfidence ? (
                  <>
                    <td>{fmtConfidence(p.memberConfidence, sections.member, skip)}</td>
                    <td>
                      {fmtConfidence(
                        p.pageQualityConfidence,
                        sections.hw || sections.rotation,
                      )}
                    </td>
                    <td>{fmtConfidence(p.dosConfidence, sections.dos, skip)}</td>
                    <td>
                      {fmtConfidence(
                        p.isDuplicate
                          ? duplicateDisplayConfidence(
                              p.isDuplicate,
                              p.pageTypeConfidence,
                            )
                          : p.pageTypeConfidence,
                        sections.junk,
                      )}
                    </td>
                  </>
                ) : null}
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
}: Props) {
  if (tab === "additional") {
    return (
      <div className="imaging-panel-stack">
        <ExtractedList
          extraction={extraction}
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
          pages={seqRows}
          sequencingProcessed={Boolean(sections.sequencing)}
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
      <ManifestDetails manifest={manifest} />
      <PageDetails
        folderId={document.folder_id}
        page={currentPage}
        sections={sections}
        extraction={extraction}
        onSaved={(groundTruth) =>
          onGroundTruthSaved?.(currentPage.pageNumber, currentPage.fileName, groundTruth)
        }
      />
    </div>
  );
}

const SECTION_SKIP_MESSAGE = "Skipped for Junk/Blank";

function SequencingTable({
  pages,
  sequencingProcessed,
}: {
  pages: ImagingPageResult[];
  sequencingProcessed: boolean;
}) {
  return (
    <div className="section-coords-panel">
      <div className="section-coords-title">Page Sequencing</div>
      {!sequencingProcessed ? (
        <p className="section-coords-empty">
          Sequencing has not run for this chart yet — Actual Sequence stays Yet
          to Process until the page_sequencing stage writes results.
        </p>
      ) : null}
      <table className="imaging-summary-table imaging-sequencing-table">
        <thead>
          <tr>
            <th scope="col">Page #</th>
            <th scope="col">File</th>
            <th scope="col">Current Sequence</th>
            <th scope="col">Actual Sequence</th>
          </tr>
        </thead>
        <tbody>
          {pages.map((p) => {
            const skip = isBlankJunkPage(p) ? { skipped: true } : undefined;
            return (
            <tr key={`seq-${p.pageNumber}-${p.fileName}`}>
              <td>{p.pageNumber}</td>
              <td className="imaging-mono">{p.fileName}</td>
              <td>{fmt(p.currentSequence ?? p.pageNumber, true)}</td>
              <td>{fmt(p.actualSequence, sequencingProcessed, skip)}</td>
            </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function boxedHeaders(
  extraction: ExtractionReviewResponse | null | undefined,
): OcrSectionHeader[] {
  return (extraction?.section_headers ?? []).filter(
    (header) => header.text.trim() && header.width > 0 && header.height > 0,
  );
}

function ExtractedList({
  extraction,
  skipped,
  loading,
  headerHighlight,
  onHeaderHighlight,
}: {
  extraction: ExtractionReviewResponse | null | undefined;
  skipped: boolean;
  loading: boolean;
  headerHighlight: "all" | number | null;
  onHeaderHighlight?: (next: "all" | number | null) => void;
}) {
  const pageNo = stagedField(extraction, "page_no");
  const boxes = boxedHeaders(extraction);
  const fromText = (stagedField(extraction, "heading_heron")?.value ?? "")
    .split("|")
    .map((part) => part.trim())
    .filter(Boolean);
  const names = boxes.length > 0 ? boxes.map((header) => header.text.trim()) : fromText;
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
                      className="header-highlight-one"
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
