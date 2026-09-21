import { useMemo, useState } from "react";
import type {
  ImagingDocumentResponse,
  ImagingManifestDetails,
  ImagingPageResult,
  ImagingSectionsProcessed,
  ImagingVerificationDetails,
  OcrSectionHeader,
} from "./api";

const DEFAULT_SECTIONS: ImagingSectionsProcessed = {
  member: false,
  dos: false,
  hw: false,
  quality: false,
  rotation: false,
  junk: false,
  verification: false,
};

/** Doc Summary fallback when a page has no DOS and nothing to inherit. */
const DEFAULT_DOS = "2/2/2022";
/** Hardcoded default DOS confidence */
const DEFAULT_DOS_CONFIDENCE = 0.8;

function hasDos(value: string | null | undefined): value is string {
  return value != null && String(value).trim() !== "";
}

/**
 * Doc Summary only: missing DOS inherits the previous page's DOS;
 * if nothing precedes, use 2/2/2022 at 80% confidence.
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
      let dosFrom = hasDos(page.dosFrom) ? page.dosFrom.trim() : null;
      let dosTo = hasDos(page.dosTo) ? page.dosTo.trim() : null;
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

type ImagingTab = "page" | "doc" | "additional";
type AdditionalSubTab = "sections" | "sequencing";
type DocView = "values" | "confidence" | "rejection";

type Props = {
  tab: ImagingTab;
  loading: boolean;
  error: string | null;
  document: ImagingDocumentResponse | null;
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
};

const YET_TO_PROCESS = "Yet to Process";
const NOT_FOUND = "Not Found";

function fmt(
  value: string | number | boolean | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "number") {
    return Number.isInteger(value) ? String(value) : value.toFixed(2);
  }
  return String(value);
}

function fmtDegrees(value: number | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return NOT_FOUND;
  const n = Number.isInteger(value) ? String(value) : value.toFixed(2);
  return `${n}°`;
}

function fmtConfidence(
  value: number | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return "NA";
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

function fmtYesNo(
  value: boolean | null | undefined,
  processed = true,
): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined) return NOT_FOUND;
  return value ? "Yes" : "No";
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

function fmtQualityTag(value: string | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  const text = String(value).trim();
  return text.charAt(0).toUpperCase() + text.slice(1).toLowerCase();
}

function fmtPageType(value: string | null | undefined, processed = true): string {
  if (!processed) return YET_TO_PROCESS;
  if (value === null || value === undefined || value === "") return NOT_FOUND;
  return String(value);
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

function DetailSection({
  title,
  rows,
  showConfidence = false,
}: {
  title: string;
  rows: { label: string; value: string; confidence?: string }[];
  showConfidence?: boolean;
}) {
  return (
    <section className="imaging-section">
      <h3 className="imaging-section-title">{title}</h3>
      <table className="imaging-detail-table">
        <thead>
          <tr>
            <th scope="col">Field</th>
            <th scope="col">Value</th>
            {showConfidence ? <th scope="col">Confidence</th> : null}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.label}>
              <th scope="row">{row.label}</th>
              <td>{row.value}</td>
              {showConfidence ? (
                <td>{row.confidence ?? "Not Found"}</td>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
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

function PageDetails({
  page,
  sections,
}: {
  page: ImagingPageResult;
  sections: ImagingSectionsProcessed;
}) {
  const memberConf = fmtConfidence(page.memberConfidence, sections.member);

  return (
    <div className="imaging-page-details">
      <section className="imaging-section">
        <h3 className="imaging-section-title">Member Extraction</h3>
        <table className="imaging-detail-table">
          <thead>
            <tr>
              <th scope="col">Field</th>
              <th scope="col">Value</th>
              <th scope="col">Confidence</th>
            </tr>
          </thead>
          <tbody>
            <tr>
              <th scope="row">Extracted Name</th>
              <td>{fmt(page.memberName, sections.member)}</td>
              <td>{memberConf}</td>
            </tr>
            <tr>
              <th scope="row">Extracted DOB</th>
              <td>{fmt(page.memberDob, sections.member)}</td>
              <td>{memberConf}</td>
            </tr>
            <tr>
              <th scope="row">Member ID</th>
              <td>{fmt(page.memberId, sections.member)}</td>
              <td>{memberConf}</td>
            </tr>
          </tbody>
        </table>
      </section>
      <DetailSection
        title="Page Quality & Orientation"
        showConfidence
        rows={[
          {
            label: "Printed / Handwritten",
            value: fmtHandwriting(page.handwrittenOrPrinted, sections.hw),
            confidence: fmtConfidence(
              page.handwrittenOrPrintedConfidence ?? null,
              sections.hw,
            ),
          },
          {
            label: "Quality",
            value: fmtQualityTag(
              page.pageQualityTag,
              sections.quality ?? sections.hw,
            ),
            confidence: fmtConfidence(
              page.pageQualityConfidence,
              sections.quality ?? sections.hw,
            ),
          },
          {
            label: "Orientation Angle (Page)",
            value: fmtDegrees(page.orientationAngle, sections.rotation),
            confidence: fmtConfidence(null, sections.rotation),
          },
          {
            label: "Tilt Angle (Text)",
            value: fmtDegrees(page.tiltAngle, sections.rotation),
            confidence: fmtConfidence(null, sections.rotation),
          },
          {
            label: "Mirrored (Text)",
            value: fmt(page.mirrored, sections.rotation),
            confidence: fmtConfidence(null, sections.rotation),
          },
        ]}
      />
      <DetailSection
        title="Encounter Details"
        showConfidence
        rows={[
          {
            label: "Encounter Type",
            value: fmt(
              page.encounterType,
              page.encounterType != null && String(page.encounterType).trim() !== "",
            ),
            confidence: fmtConfidence(null, false),
          },
          {
            label: "DOS From",
            value: fmt(page.dosFrom, sections.dos),
            confidence: fmtConfidence(page.dosConfidence, sections.dos),
          },
          {
            label: "DOS To",
            value: fmt(page.dosTo, sections.dos),
            confidence: fmtConfidence(page.dosConfidence, sections.dos),
          },
        ]}
      />
      <DetailSection
        title="Page Classification"
        showConfidence
        rows={[
          {
            label: "Is Blank or Junk?",
            value: fmtBlankOrJunk(page.blankOrJunk, sections.junk),
            confidence: fmtConfidence(page.pageTypeConfidence, sections.junk),
          },
          {
            label: "Is Duplicate",
            value: fmtYesNo(page.isDuplicate, sections.junk),
            confidence: fmtConfidence(page.pageTypeConfidence, sections.junk),
          },
          {
            label: "Page Type",
            value: fmtPageType(page.pageType, sections.junk),
            confidence: fmtConfidence(page.pageTypeConfidence, sections.junk),
          },
          {
            label: "Is Codeable or Non Codeable",
            value: fmt(
              page.isCodeable,
              page.isCodeable != null && String(page.isCodeable).trim() !== "",
            ),
            confidence: fmtConfidence(null, false),
          },
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
              <th scope="col">HW/Printed</th>
              <th scope="col">Quality</th>
              <th scope="col">Orient.</th>
              <th scope="col">Tilt</th>
              <th scope="col">Mirrored</th>
              <th scope="col">DOS From</th>
              <th scope="col">DOS To</th>
              <th scope="col">Is Blank or Junk?</th>
              <th scope="col">Duplicate</th>
              <th scope="col">Page Type</th>
              <th scope="col">Codeable</th>
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
            {rows.map((p) => (
              <tr key={`${p.pageNumber}-${p.fileName}`}>
                <td>{p.pageNumber}</td>
                <td className="imaging-mono">{p.fileName}</td>
                <td>{fmt(p.memberName, sections.member)}</td>
                <td>{fmt(p.memberDob, sections.member)}</td>
                <td>{fmt(p.memberId, sections.member)}</td>
                <td>{fmtHandwriting(p.handwrittenOrPrinted, sections.hw)}</td>
                <td>
                  {fmtQualityTag(
                    p.pageQualityTag,
                    sections.quality ?? sections.hw,
                  )}
                </td>
                <td>{fmtDegrees(p.orientationAngle, sections.rotation)}</td>
                <td>{fmtDegrees(p.tiltAngle, sections.rotation)}</td>
                <td>{fmt(p.mirrored, sections.rotation)}</td>
                <td>{fmt(p.dosFrom, sections.dos)}</td>
                <td>{fmt(p.dosTo, sections.dos)}</td>
                <td>{fmtBlankOrJunk(p.blankOrJunk, sections.junk)}</td>
                <td>{fmtYesNo(p.isDuplicate, sections.junk)}</td>
                <td>{fmtPageType(p.pageType, sections.junk)}</td>
                <td>{fmt(p.isCodeable, p.isCodeable != null && String(p.isCodeable).trim() !== "")}</td>
                <td>{fmt(p.currentSequence ?? p.pageNumber, true)}</td>
                <td>
                  {fmt(
                    p.actualSequence,
                    p.actualSequence != null,
                  )}
                </td>
                {showConfidence ? (
                  <>
                    <td>{fmtConfidence(p.memberConfidence, sections.member)}</td>
                    <td>
                      {fmtConfidence(
                        p.pageQualityConfidence,
                        sections.hw || sections.rotation,
                      )}
                    </td>
                    <td>{fmtConfidence(p.dosConfidence, sections.dos)}</td>
                    <td>{fmtConfidence(p.pageTypeConfidence, sections.junk)}</td>
                  </>
                ) : null}
              </tr>
            ))}
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
  currentPage,
  currentFileName,
  sectionHeaders = [],
  sectionHeadersSource = null,
  sectionHeadersSkipped = false,
  sectionHeadersLoading = false,
  imageNaturalSize = null,
}: Props) {
  const [additionalSubTab, setAdditionalSubTab] =
    useState<AdditionalSubTab>("sections");

  if (tab === "additional") {
    return (
      <div className="imaging-panel-stack">
        <div
          className="output-tabs imaging-additional-tabs"
          role="tablist"
          aria-label="Additional views"
        >
          <button
            type="button"
            role="tab"
            aria-selected={additionalSubTab === "sections"}
            className={additionalSubTab === "sections" ? "active" : ""}
            onClick={() => setAdditionalSubTab("sections")}
          >
            Section Coordinates
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={additionalSubTab === "sequencing"}
            className={additionalSubTab === "sequencing" ? "active" : ""}
            onClick={() => setAdditionalSubTab("sequencing")}
          >
            Sequencing
          </button>
        </div>
        {additionalSubTab === "sections" ? (
          <SectionCoordinates
            fileName={currentFileName}
            headers={sectionHeaders}
            source={sectionHeadersSource}
            skipped={sectionHeadersSkipped}
            loading={sectionHeadersLoading}
            imageNaturalSize={imageNaturalSize}
          />
        ) : (
          <SequencingPanel
            page={currentPage}
            fileName={currentFileName}
          />
        )}
      </div>
    );
  }

  if (loading) {
    return <div className="ocr-loading">Loading imaging results…</div>;
  }
  if (error) {
    return <div className="ocr-empty">{error}</div>;
  }
  if (!document || document.pages.length === 0) {
    return <div className="ocr-empty">No imaging results for this folder.</div>;
  }

  const manifest = document.manifest ?? {
    member: null,
    dob: null,
    memberId: null,
  };
  const sections = document.sectionsProcessed ?? DEFAULT_SECTIONS;
  const verifications =
    document.verifications && document.verifications.length > 0
      ? document.verifications
      : document.verification
        ? [document.verification]
        : [];

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
      <PageDetails page={currentPage} sections={sections} />
    </div>
  );
}

const SECTION_SKIP_MESSAGE = "Skipped for Junk/Blank";

function SequencingPanel({
  page,
  fileName,
}: {
  page: ImagingPageResult | null;
  fileName: string | null;
}) {
  const current = page?.currentSequence ?? page?.pageNumber ?? null;
  return (
    <div className="section-coords-panel">
      <div className="section-coords-title">Sequencing</div>
      {fileName ? (
        <p className="section-coords-file">{fileName}</p>
      ) : null}
      <table className="imaging-detail-table">
        <tbody>
          <tr>
            <th scope="row">Current Sequence</th>
            <td>{fmt(current, true)}</td>
          </tr>
          <tr>
            <th scope="row">Actual Sequence</th>
            <td>{fmt(page?.actualSequence, false)}</td>
          </tr>
        </tbody>
      </table>
      <p className="section-coords-empty">
        Sequencing logic will be wired next — Actual Sequence stays Yet to Process until then.
      </p>
    </div>
  );
}

function SectionCoordinates({
  fileName,
  headers,
  source,
  skipped,
  loading,
  imageNaturalSize,
}: {
  fileName: string | null;
  headers: OcrSectionHeader[];
  source: "final1" | "final2" | null;
  skipped: boolean;
  loading: boolean;
  imageNaturalSize: { w: number; h: number } | null;
}) {
  if (loading) {
    return <div className="ocr-loading">Loading section coordinates…</div>;
  }
  if (skipped) {
    return (
      <div className="section-coords-panel">
        <div className="section-coords-title">Section Coordinates</div>
        <p className="section-coords-skip">{SECTION_SKIP_MESSAGE}</p>
      </div>
    );
  }
  const withBox = headers.filter((h) => h.width > 0 && h.height > 0);
  const sourceLabel =
    source === "final2"
      ? "Final (AzDocInt)"
      : source === "final1"
        ? "Final (OSS)"
        : null;
  const natW = imageNaturalSize?.w ?? 0;
  const natH = imageNaturalSize?.h ?? 0;

  return (
    <div className="section-coords-panel">
      <div className="section-coords-title">
        Section Coordinates
        {sourceLabel ? (
          <span className="section-coords-source"> · {sourceLabel}</span>
        ) : null}
      </div>
      {fileName ? (
        <p className="section-coords-file">{fileName}</p>
      ) : null}
      {!withBox.length ? (
        <p className="section-coords-empty">
          No section headers with coordinates for this page.
        </p>
      ) : (
        <div className="section-coords-list">
          {withBox.map((h, i) => {
            const bboxLabel =
              natW > 0 && natH > 0
                ? `[${[
                    Math.round(h.left * natW),
                    Math.round(h.top * natH),
                    Math.round((h.left + h.width) * natW),
                    Math.round((h.top + h.height) * natH),
                  ].join(", ")}]`
                : `[${[h.left, h.top, h.width, h.height]
                    .map((n) => n.toFixed(3))
                    .join(", ")}]`;
            return (
              <div key={`${h.text}-${i}`} className="section-coords-row">
                <span className="section-coords-text" title={h.text}>
                  {h.text}
                </span>
                <span
                  className="section-coords-bbox"
                  title="left, top, right, bottom (image pixels)"
                >
                  {bboxLabel}
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export type { ImagingTab };
